"""Stability dampers: per-task flap hysteresis and the cluster-wide storm breaker.

Both are pure sliding-window counters over an injected clock so the governor's
decisions stay reproducible in tests and across a restart-free long run.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.attention_governor")


class FlapDetector:
    """§7.1 anti-flap: a task bouncing through failure states faster than the window
    is damped once, and its intermediate events are swallowed.

    ``max_transitions`` is the number tolerated *within* the window; the transition
    that exceeds it trips the task.
    """

    def __init__(self, window_seconds: float = 60.0, max_transitions: int = 2) -> None:
        self.window_seconds = window_seconds
        self.max_transitions = max_transitions
        self.history: Dict[str, List[float]] = {}

    def record_and_check_flap(self, task_id: str, now: Optional[float] = None) -> bool:
        """Record one failure transition; True when *task_id* has tripped the limit."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        recent = [ts for ts in self.history.get(task_id, []) if ts >= cutoff]
        recent.append(now)
        self.history[task_id] = recent
        return len(recent) > self.max_transitions

    def reset(self, task_id: str) -> None:
        """Forget a task's history — called when a human resolves its triage card."""
        self.history.pop(task_id, None)


class SwarmStormBreaker:
    """§7.2 provider-outage breaker: ``threshold`` DISTINCT tasks reporting transient
    failures inside ``window_seconds`` pauses spawns and silences the individual pings.

    Distinct tasks, not raw events: one worker retrying a flaky endpoint five times is
    that worker's problem, while three workers failing at once is the upstream's.
    Repeat trips back off exponentially to ``max_cooldown_seconds``.
    """

    def __init__(
        self,
        window_seconds: float = 30.0,
        threshold_distinct_tasks: int = 3,
        cooldown_seconds: float = 60.0,
        max_cooldown_seconds: float = 900.0,
    ) -> None:
        self.window_seconds = window_seconds
        self.threshold = threshold_distinct_tasks
        self.base_cooldown = cooldown_seconds
        self.max_cooldown = max_cooldown_seconds
        self.transient_events: List[Tuple[str, float]] = []
        self.tripped_until = 0.0
        self.trip_count = 0

    def record_transient(self, task_id: str, now: Optional[float] = None) -> bool:
        """Record one transient failure; True while the breaker is open."""
        now = time.time() if now is None else now
        if now < self.tripped_until:
            return True

        cutoff = now - self.window_seconds
        self.transient_events = [(tid, ts) for tid, ts in self.transient_events if ts >= cutoff]
        self.transient_events.append((task_id, now))

        distinct = {tid for tid, _ in self.transient_events}
        if len(distinct) < self.threshold:
            return False
        self.trip_count += 1
        cooldown = min(self.base_cooldown * (2 ** (self.trip_count - 1)), self.max_cooldown)
        self.tripped_until = now + cooldown
        logger.warning(
            "attention governor: swarm storm breaker tripped; pausing spawns for %.0fs (%d tasks: %s)",
            cooldown, len(distinct), sorted(distinct),
        )
        return True

    def is_tripped(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) < self.tripped_until
