"""Session-scoped runaway guards: spend, turn count, wall-clock, terminal polling.

``agent.tool_guardrails`` already breaks per-TURN tool loops, but it is blind to the
failure shape that dominates real spend: it resets at every turn boundary, and it only
fires on byte-identical results — a ``tmux capture-pane`` poll whose output drifts by one
line is never identical. The audit behind PLAN-REMEDIATION-MIGRATION-20260925 found four
unmonitored sessions burning >$300 across 28-175 hours in exactly that loop.

So these breakers are deliberately the other half of the pair: session-scoped (they live on
the agent, not on the turn), and they judge the COMMAND SHAPE, never the result.

Limits come from ``config.yaml`` (``agent.circuit_breakers``) — not an env var, which is for
secrets. The one env var here, ``HERMES_DISABLE_COST_BREAKER``, is the plan's documented
rollback switch: a kill-switch, not configuration.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, fields
from typing import Any, Deque, Mapping, Optional

logger = logging.getLogger("agent.circuit_breakers")

# Rollback switch (plan §Priority 0 "Rollback Approach"): reverts to an unconstrained loop.
DISABLE_ENV_VAR = "HERMES_DISABLE_COST_BREAKER"

# Plan defaults. A limit of exactly 0 disables that one tripwire (same convention as
# ``tool_guardrails.LoopCapConfig``); anything unparseable or negative falls back here.
DEFAULT_MAX_SESSION_COST_USD = 10.0
DEFAULT_MAX_TURNS = 120
DEFAULT_MAX_CONTINUOUS_HOURS = 2.0

# Sliding window of the last N shell commands; this many matching polls in it is a loop.
# 4-of-5 tolerates a double-check and a genuine interleaved build, but not a spin.
POLLING_WINDOW = 5
POLLING_TRIP_COUNT = 4

STEERING_INTERRUPT = (
    "[SYSTEM: Consecutive polling loop detected. Halt and await notify marker or event relay.]"
)

_AGENT_BREAKER_ATTR = "_session_cost_breaker"
_AGENT_DETECTOR_ATTR = "_terminal_polling_detector"

# Tools whose arguments carry a shell command. ``process_manage`` is deliberately absent:
# it IS the sanctioned poller, and routing waits through it is the behaviour we want.
_SHELL_TOOLS = frozenset({"terminal"})
_COMMAND_ARG_KEYS = ("command", "cmd")

# Command shapes that do nothing but observe. Each is a wait dressed as work, so a window
# full of them is a session spending model tokens to watch a clock.
_POLLING_PATTERNS = tuple(re.compile(p) for p in (
    r"\btmux\s+capture-pane\b",
    r"\btmux\s+(?:ls|list-\w+)\b",
    r"\bps\s+(?:aux|-ef|-e|-a)\b",
    r"\bpgrep\b",
    r"\btail\s+-f\b",
    r"\btail\s+(?:-\S+\s+)*-n\b",
    r"\bsleep\s+[0-9]",
    r"\bwatch\s+-?",
    r"\bdocker\s+logs\b",
    r"\bjournalctl\b",
    r"\bsystemctl\s+status\b",
))
# Literals that drift between consecutive polls (line counts, pane ids, pids, timestamps).
# Folding them is the whole point: "near-identical" is what the byte-exact guard misses.
_DIGIT_RUN = re.compile(r"[0-9]+")


def breakers_disabled() -> bool:
    """True when the operator pulled the rollback switch."""
    return os.environ.get(DISABLE_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BreakerLimits:
    """Hard session ceilings. ``0`` disables one tripwire; the others stay armed."""

    max_session_cost_usd: float = DEFAULT_MAX_SESSION_COST_USD
    max_turns: int = DEFAULT_MAX_TURNS
    max_continuous_hours: float = DEFAULT_MAX_CONTINUOUS_HOURS

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "BreakerLimits":
        if not isinstance(data, Mapping):
            return cls()
        kwargs = {}
        for spec in fields(cls):
            raw = data.get(spec.name)
            if raw is None or isinstance(raw, bool):
                kwargs[spec.name] = spec.default
                continue
            try:
                # Keyed off the default's type, not the annotation string: ``from __future__
                # import annotations`` makes ``spec.type`` a str, which silently breaks on rename.
                value = int(raw) if isinstance(spec.default, int) else float(raw)
            except (TypeError, ValueError):
                value = spec.default
            kwargs[spec.name] = spec.default if value < 0 else value
        return cls(**kwargs)

    @property
    def max_continuous_seconds(self) -> float:
        return self.max_continuous_hours * 3600.0


def load_breaker_limits() -> BreakerLimits:
    """Read ``agent.circuit_breakers`` from config; shipped defaults on any failure."""
    try:
        from hermes_cli.config import load_config_readonly

        section = (load_config_readonly() or {}).get("agent", {}).get("circuit_breakers")
    except Exception:
        logger.debug("circuit-breaker config read failed; using defaults", exc_info=True)
        return BreakerLimits()
    return BreakerLimits.from_mapping(section)


@dataclass(frozen=True)
class BreakerTrip:
    """A crossed ceiling. ``code`` names the tripwire; ``message`` is operator-facing."""

    code: str
    observed: Any
    limit: Any
    message: str


class SessionBreaker:
    """Spend / turn / wall-clock ceilings for one agent's whole session.

    Latching is deliberate. A breaker that re-armed itself would let the same loop resume
    on the next iteration and spend the ceiling again; crossing one requires an explicit
    human act (raise the limit in ``config.yaml``, start a new session, or pull the
    rollback switch), which is the plan's "prompt user before continuing".
    """

    def __init__(self, limits: Optional[BreakerLimits] = None, *, now: Optional[float] = None):
        self.limits = limits if limits is not None else load_breaker_limits()
        self.started_at = time.monotonic() if now is None else now
        self.turns = 0
        self.tripped: Optional[BreakerTrip] = None

    def consume_turn(self, *, cost_usd: float = 0.0, now: Optional[float] = None) -> Optional[BreakerTrip]:
        """Account for one about-to-start model call; return the trip that must stop the loop.

        Checked BEFORE the call, so a tripped session never pays for the crossing request.
        A refused turn is not counted.
        """
        if breakers_disabled():
            return None
        if self.tripped is not None:
            return self.tripped
        now = time.monotonic() if now is None else now
        trip = self._first_crossing(cost_usd, now)
        if trip is not None:
            self.tripped = trip
            logger.warning("session circuit breaker tripped: %s", trip.message)
            return trip
        self.turns += 1
        return None

    def _first_crossing(self, cost_usd: float, now: float) -> Optional[BreakerTrip]:
        limits = self.limits
        try:
            cost = float(cost_usd or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if limits.max_session_cost_usd > 0 and cost >= limits.max_session_cost_usd:
            return BreakerTrip(
                "session_cost", cost, limits.max_session_cost_usd,
                f"Session spend reached ${cost:.2f} against the ${limits.max_session_cost_usd:.2f} "
                f"ceiling; stopping before the next model call. {_RESUME_HINT}",
            )
        if limits.max_turns > 0 and self.turns >= limits.max_turns:
            return BreakerTrip(
                "session_turns", self.turns, limits.max_turns,
                f"Session reached its {limits.max_turns}-turn ceiling without finishing; "
                f"stopping before the next model call. {_RESUME_HINT}",
            )
        elapsed = max(0.0, now - self.started_at)
        if limits.max_continuous_hours > 0 and elapsed >= limits.max_continuous_seconds:
            return BreakerTrip(
                "session_wall_clock", elapsed, limits.max_continuous_seconds,
                f"Session has run continuously for {elapsed / 3600.0:.1f}h against the "
                f"{limits.max_continuous_hours:g}h ceiling; stopping before the next model "
                f"call. {_RESUME_HINT}",
            )
        return None


_RESUME_HINT = (
    "Raise the ceiling under `agent.circuit_breakers` in config.yaml, start a fresh session, "
    f"or set {DISABLE_ENV_VAR}=1 to run unconstrained."
)


def normalize_polling_command(command: Any) -> Optional[str]:
    """Shape key for a polling command, or ``None`` when the command does real work.

    Two polls share a key when they differ only in the literals that drift between
    iterations, so ``tail -n 20 x.log`` and ``tail -n 50 x.log`` are one shape while
    ``tail -n 20 y.log`` is another.
    """
    if not isinstance(command, str):
        return None
    text = " ".join(command.strip().lower().split())
    if not text or not any(pattern.search(text) for pattern in _POLLING_PATTERNS):
        return None
    return _DIGIT_RUN.sub("#", text)


class PollingLoopDetector:
    """Sliding window over the session's shell commands; trips on a polling spin."""

    def __init__(self, *, window: int = POLLING_WINDOW, trip_after: int = POLLING_TRIP_COUNT):
        self._window: Deque[Optional[str]] = deque(maxlen=max(1, window))
        self._trip_after = max(2, trip_after)

    def observe(self, command: Any) -> Optional[BreakerTrip]:
        """Record one shell command; return a trip when the window is a polling loop."""
        if breakers_disabled():
            return None
        shape = normalize_polling_command(command)
        self._window.append(shape)
        if shape is None:
            return None
        count = sum(1 for seen in self._window if seen == shape)
        if count < self._trip_after:
            return None
        return BreakerTrip(
            "terminal_polling_loop", count, self._trip_after,
            f"{STEERING_INTERRUPT} {count} of the last {len(self._window)} shell commands were "
            f"the same poll (`{shape}`). Stop polling: wait on an event, a notify marker, or a "
            "single blocking command with a generous timeout instead of re-reading state.",
        )


def get_session_breaker(agent: Any) -> SessionBreaker:
    """The agent's own breaker, created on first use so it spans every turn."""
    breaker = getattr(agent, _AGENT_BREAKER_ATTR, None)
    if not isinstance(breaker, SessionBreaker):
        breaker = SessionBreaker(load_breaker_limits())
        setattr(agent, _AGENT_BREAKER_ATTR, breaker)
    return breaker


def get_polling_detector(agent: Any) -> PollingLoopDetector:
    detector = getattr(agent, _AGENT_DETECTOR_ATTR, None)
    if not isinstance(detector, PollingLoopDetector):
        detector = PollingLoopDetector()
        setattr(agent, _AGENT_DETECTOR_ATTR, detector)
    return detector


def session_breaker_trip(agent: Any) -> Optional[BreakerTrip]:
    """Loop seam: account for the next model call, returning the trip that must stop it."""
    if breakers_disabled():
        return None
    try:
        breaker = get_session_breaker(agent)
        return breaker.consume_turn(cost_usd=getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0)
    except Exception:
        # A guard that raises would take down turns it exists to protect; fail open.
        logger.debug("session breaker check failed", exc_info=True)
        return None


def observe_tool_command(agent: Any, tool_name: str, args: Any) -> Optional[BreakerTrip]:
    """Tool-result seam: feed a shell command to the detector, returning any trip."""
    if tool_name not in _SHELL_TOOLS or breakers_disabled():
        return None
    try:
        command = next(
            (args[key] for key in _COMMAND_ARG_KEYS if isinstance(args, Mapping) and key in args),
            None,
        )
        return get_polling_detector(agent).observe(command)
    except Exception:
        logger.debug("polling detector failed", exc_info=True)
        return None


__all__ = [
    "BreakerLimits", "BreakerTrip", "DISABLE_ENV_VAR", "PollingLoopDetector", "SessionBreaker",
    "STEERING_INTERRUPT", "breakers_disabled", "get_polling_detector", "get_session_breaker",
    "load_breaker_limits", "normalize_polling_command", "observe_tool_command",
    "session_breaker_trip",
]
