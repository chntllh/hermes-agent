"""Session runaway circuit breakers (PLAN-REMEDIATION-MIGRATION-20260925, Priority 0).

The historical audit behind the plan: 26.87% of sessions failed or stalled and
consumed 76.1% of all spend; four unmonitored sessions burned >$300 over 28-175
hours polling ``tmux capture-pane``. ``agent.tool_guardrails`` cannot see that
shape — it resets every turn, and a poll whose output drifts by one line is never
a byte-identical repeat — so these breakers are session-scoped and judge the
COMMAND, not the result.
"""

from __future__ import annotations

import pytest

from agent import circuit_breakers as cb


# ---------------------------------------------------------------------------
# Limits + rollback switch
# ---------------------------------------------------------------------------


def test_default_limits_match_the_plan():
    limits = cb.BreakerLimits()
    assert limits.max_session_cost_usd == 10.0
    assert limits.max_turns == 120
    assert limits.max_continuous_hours == 2.0


def test_limits_from_mapping_coerces_and_falls_back():
    assert cb.BreakerLimits.from_mapping({"max_session_cost_usd": 25}).max_session_cost_usd == 25.0
    assert cb.BreakerLimits.from_mapping({"max_turns": "40"}).max_turns == 40
    # Garbage and negatives fall back to the shipped default rather than disabling the guard.
    assert cb.BreakerLimits.from_mapping({"max_turns": "nope"}).max_turns == 120
    assert cb.BreakerLimits.from_mapping({"max_turns": -5}).max_turns == 120
    assert cb.BreakerLimits.from_mapping(None) == cb.BreakerLimits()
    # An explicit zero disables one limit without touching the others.
    zeroed = cb.BreakerLimits.from_mapping({"max_session_cost_usd": 0})
    assert zeroed.max_session_cost_usd == 0.0
    assert zeroed.max_turns == 120


def test_rollback_env_var_disables_every_breaker(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    assert cb.breakers_disabled() is False
    monkeypatch.setenv(cb.DISABLE_ENV_VAR, "1")
    assert cb.breakers_disabled() is True
    # A disabled breaker never trips, however far past the ceiling the session is.
    breaker = cb.SessionBreaker(cb.BreakerLimits(max_session_cost_usd=1.0), now=0.0)
    assert breaker.consume_turn(cost_usd=500.0, now=0.0) is None


# ---------------------------------------------------------------------------
# Cost / turn / wall-clock tripwires
# ---------------------------------------------------------------------------


def test_cost_tripwire_halts_before_the_next_api_call(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    breaker = cb.SessionBreaker(cb.BreakerLimits(), now=0.0)
    assert breaker.consume_turn(cost_usd=9.99, now=1.0) is None
    trip = breaker.consume_turn(cost_usd=10.0, now=2.0)
    assert trip is not None and trip.code == "session_cost"
    assert trip.observed == 10.0 and trip.limit == 10.0
    assert "$10.00" in trip.message
    # The trip latches: the loop cannot be restarted into another paid call.
    assert breaker.tripped is trip
    assert breaker.consume_turn(cost_usd=0.0, now=3.0) is trip


def test_turn_tripwire_allows_exactly_max_turns(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    breaker = cb.SessionBreaker(cb.BreakerLimits(max_turns=3), now=0.0)
    assert [breaker.consume_turn(now=float(i)) for i in range(3)] == [None, None, None]
    assert breaker.turns == 3
    trip = breaker.consume_turn(now=4.0)
    assert trip is not None and trip.code == "session_turns"
    assert trip.observed == 3 and trip.limit == 3
    # The refused turn is not counted.
    assert breaker.turns == 3


def test_wall_clock_tripwire_uses_the_monotonic_start(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    breaker = cb.SessionBreaker(cb.BreakerLimits(max_continuous_hours=2.0), now=1000.0)
    assert breaker.consume_turn(now=1000.0 + 7199.0) is None
    trip = breaker.consume_turn(now=1000.0 + 7200.0)
    assert trip is not None and trip.code == "session_wall_clock"
    assert trip.limit == pytest.approx(7200.0)


def test_zero_limit_disables_only_that_tripwire(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    breaker = cb.SessionBreaker(
        cb.BreakerLimits(max_session_cost_usd=0.0, max_turns=2, max_continuous_hours=0.0), now=0.0,
    )
    assert breaker.consume_turn(cost_usd=10_000.0, now=10**9) is None
    assert breaker.consume_turn(cost_usd=10_000.0, now=10**9) is None
    assert breaker.consume_turn(cost_usd=10_000.0, now=10**9).code == "session_turns"


# ---------------------------------------------------------------------------
# Terminal polling detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "tmux capture-pane -p -t build:0",
    "ps aux | grep hermes",
    "tail -n 20 /tmp/run.log",
    "tail -f /tmp/run.log",
    "pgrep -f pytest",
    "sleep 30",
    "docker logs --tail 50 web",
    "journalctl -u hermes-gateway -n 40",
])
def test_polling_commands_are_recognized(command):
    assert cb.normalize_polling_command(command) is not None


@pytest.mark.parametrize("command", [
    "pytest tests/agent -q",
    "git commit -m 'wip'",
    "uv run --group dev python -c 'print(1)'",
    "cargo build --release",
])
def test_real_work_is_not_mistaken_for_polling(command):
    assert cb.normalize_polling_command(command) is None


def test_near_identical_polls_share_one_shape():
    # The literals that drift between polls (counts, pane ids, timestamps) are the
    # exact reason the byte-identical guardrail in agent.tool_guardrails never fires.
    assert (cb.normalize_polling_command("tail -n 20 /tmp/run.log")
            == cb.normalize_polling_command("tail -n 50 /tmp/run.log"))
    assert (cb.normalize_polling_command("tmux capture-pane -p -t build:0")
            == cb.normalize_polling_command("tmux  capture-pane  -p  -t  build:1"))
    assert (cb.normalize_polling_command("tail -n 20 /tmp/run.log")
            != cb.normalize_polling_command("tail -n 20 /tmp/other.log"))


def test_detector_trips_on_the_fourth_poll_of_a_five_call_window():
    """Plan acceptance: a synthetic loop of 10 identical polls is caught at the 4th."""
    detector = cb.PollingLoopDetector()
    trips = [detector.observe("tmux capture-pane -p -t build:0") for _ in range(10)]
    assert trips[0] is None and trips[1] is None and trips[2] is None
    assert trips[3] is not None
    assert trips[3].code == "terminal_polling_loop"
    assert cb.STEERING_INTERRUPT in trips[3].message
    # Every later poll in the loop stays flagged; the count escalates.
    assert all(t is not None for t in trips[3:])
    assert trips[-1].observed == 5  # the window only holds five


def test_detector_tolerates_polls_interleaved_with_real_work():
    """Three polls and two builds in the window is diagnosis, not a spin loop."""
    detector = cb.PollingLoopDetector()
    for command in ("tail -n 20 /tmp/run.log", "cargo build", "tail -n 20 /tmp/run.log",
                    "cargo build", "tail -n 20 /tmp/run.log"):
        assert detector.observe(command) is None


def test_detector_resets_once_the_agent_makes_progress():
    detector = cb.PollingLoopDetector()
    for _ in range(4):
        detector.observe("ps aux | grep worker")
    assert detector.observe("ps aux | grep worker") is not None
    for command in ("make build", "make test", "git add -A"):
        detector.observe(command)
    # Only two of the last five calls are polls now.
    assert detector.observe("ps aux | grep worker") is None


def test_detector_is_disabled_by_the_rollback_env_var(monkeypatch):
    monkeypatch.setenv(cb.DISABLE_ENV_VAR, "1")
    detector = cb.PollingLoopDetector()
    assert all(detector.observe("tmux capture-pane -p") is None for _ in range(10))


# ---------------------------------------------------------------------------
# Agent-facing entry points (the two wiring seams)
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, cost=0.0):
        self.session_estimated_cost_usd = cost


def test_session_breaker_trip_reads_the_agents_running_cost(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(cb, "load_breaker_limits", lambda: cb.BreakerLimits(max_session_cost_usd=2.0))
    agent = _FakeAgent(cost=0.5)
    assert cb.session_breaker_trip(agent) is None
    agent.session_estimated_cost_usd = 2.5
    trip = cb.session_breaker_trip(agent)
    assert trip is not None and trip.code == "session_cost"
    # One breaker per agent, so turns and elapsed time accumulate across turns.
    assert cb.get_session_breaker(agent) is cb.get_session_breaker(agent)


def test_observe_tool_command_only_watches_shell_tools(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    agent = _FakeAgent()
    for _ in range(9):
        assert cb.observe_tool_command(agent, "read_file", {"path": "a.py"}) is None
    trips = [cb.observe_tool_command(agent, "terminal", {"command": "tmux capture-pane -p"})
             for _ in range(4)]
    assert trips[:3] == [None, None, None]
    assert trips[3] is not None and trips[3].code == "terminal_polling_loop"


def test_observe_tool_command_survives_malformed_args(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    agent = _FakeAgent()
    assert cb.observe_tool_command(agent, "terminal", None) is None
    assert cb.observe_tool_command(agent, "terminal", {"command": 42}) is None
    assert cb.observe_tool_command(agent, "terminal", {}) is None


# ---------------------------------------------------------------------------
# Loop seam: begin_iteration must actually stop the turn
# ---------------------------------------------------------------------------


class _LoopAgent:
    """The slice of AIAgent that ``begin_iteration`` touches."""

    def __init__(self, cost):
        self.session_estimated_cost_usd = cost
        self.quiet_mode = True
        self._interrupt_requested = False
        self._budget_grace_call = False
        self._api_call_count = 0
        self.printed: list = []
        self.iteration_budget = type("_B", (), {
            "consume": lambda self: True, "used": 0, "max_total": 500,
        })()
        self._checkpoint_mgr = type("_C", (), {"new_turn": lambda self: None})()

    def _drain_pending_redirect(self):
        return None

    def _touch_activity(self, _label):
        pass

    def _safe_print(self, text, **_kw):
        self.printed.append(text)


def _begin(agent):
    from agent.turn_iteration_prep import begin_iteration

    return begin_iteration(
        agent, messages=[], conversation_history=[], original_user_message="go",
        api_call_count=0, interrupted=False, _turn_exit_reason=None,
    )


def test_begin_iteration_breaks_the_loop_once_the_cost_ceiling_is_crossed(monkeypatch):
    monkeypatch.delenv(cb.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(cb, "load_breaker_limits", lambda: cb.BreakerLimits(max_session_cost_usd=10.0))
    agent = _LoopAgent(cost=4.0)
    assert _begin(agent).action == "fallthrough"
    assert agent._api_call_count == 1

    agent.session_estimated_cost_usd = 10.5
    verdict = _begin(agent)
    assert verdict.action == "break"
    assert verdict._turn_exit_reason == "halted_budget_tripwire(session_cost)"
    # The refused iteration never became a paid API call.
    assert agent._api_call_count == 1


def test_begin_iteration_is_unconstrained_under_the_rollback_switch(monkeypatch):
    monkeypatch.setenv(cb.DISABLE_ENV_VAR, "1")
    monkeypatch.setattr(cb, "load_breaker_limits", lambda: cb.BreakerLimits(max_session_cost_usd=1.0))
    agent = _LoopAgent(cost=999.0)
    assert _begin(agent).action == "fallthrough"
    assert agent._api_call_count == 1
