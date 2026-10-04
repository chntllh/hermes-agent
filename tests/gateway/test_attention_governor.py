"""Attention Governor: tier classification, Focus Shield gating and swarm breakers.

Ports the ten acceptance scenarios of ``ARCH-KANBAN-NOTIFY-GOV-20260925`` §9 and adds
the dual-mode Totonou resolver contract. Everything here is hermetic: the Totonou
probe is a local fake and no clock, network or board DB is touched.
"""

from __future__ import annotations

import pytest

from gateway.attention_governor import (
    AttentionGovernor,
    BlockKind,
    EventKind,
    KanbanTaskEvent,
    NotificationTier,
    TotonouFocusState,
    resolve_totonou_focus,
)

PERSONAL = "personal"
WORK = "work"
OPERATOR_ID = "1409460360544653344"


@pytest.fixture
def focus_state() -> TotonouFocusState:
    return TotonouFocusState(mode=WORK, focus_shield_active=False)


@pytest.fixture
def governor(focus_state) -> AttentionGovernor:
    return AttentionGovernor(discord_user_id=OPERATOR_ID, totonou_client=lambda: focus_state)


def _review(task_id: str, **kwargs) -> KanbanTaskEvent:
    kwargs.setdefault("title", f"Review {task_id}")
    kwargs.setdefault("priority", 2)
    return KanbanTaskEvent(
        task_id=task_id, board_slug="kontrol", kind=EventKind.REVIEW_REQUESTED, status="review", **kwargs
    )


# --- §9.1 Focus Shield ---------------------------------------------------------------


def test_focus_shield_holds_every_p1_without_emitting_a_card(governor, focus_state):
    """Ten concurrent reviews during a focus session must produce zero Discord surface."""
    focus_state.focus_shield_active = True
    focus_state.active_task_title = "Debugging Kontrol USB Descriptors"

    for i in range(1, 11):
        assert governor.ingest_event(_review(f"TASK-{i}")) is NotificationTier.P1_REVIEW_QUEUE

    assert governor.outbound_discord_cards == []
    assert governor.outbound_discord_pings == []
    # Each is staged silently in the Totonou work capture inbox and held in the queue.
    assert len(governor.outbound_totonou_captures) == 10
    assert len(governor.decision_queue.queue) == 10
    assert governor.decision_queue.active_card_task_id is None


# --- §9.2 Linearization --------------------------------------------------------------


def test_intermission_window_presents_exactly_one_highest_scoring_card(governor, focus_state):
    focus_state.focus_shield_active = True
    governor.ingest_event(_review("TASK-LOW", priority=1))
    governor.ingest_event(_review("TASK-HIGH", priority=3, downstream_blocked_count=2))
    governor.ingest_event(_review("TASK-MED", priority=2))
    assert governor.outbound_discord_cards == []

    focus_state.focus_shield_active = False
    governor.on_totonou_focus_completed()

    assert len(governor.outbound_discord_cards) == 1
    card = governor.outbound_discord_cards[0]
    assert card["task_id"] == "TASK-HIGH"
    # Anti-ping rule: a review card never mentions anyone.
    assert card["allowed_mentions"] == {"parse": []}
    # The other two stay pending behind the single attention slot.
    assert sum(c.status == "pending" for c in governor.decision_queue.queue.values()) == 2


def test_operator_response_advances_the_queue_in_place(governor):
    governor.ingest_event(_review("TASK-1", priority=2))
    governor.ingest_event(_review("TASK-2", priority=1))

    # WIP=1: only the first ever reached the surface.
    assert [c["task_id"] for c in governor.outbound_discord_cards] == ["TASK-1"]

    nxt = governor.operator_respond_hitl("TASK-1", "approve")

    assert nxt is not None and nxt.task_id == "TASK-2"
    assert [c["task_id"] for c in governor.outbound_discord_cards] == ["TASK-1", "TASK-2"]
    assert governor.decision_queue.queue["TASK-1"].status == "resolved"


# --- §9.3 Provider storm -------------------------------------------------------------


def test_transient_storm_trips_the_breaker_without_a_single_ping(governor):
    base = 1000.0
    for i in range(1, 11):
        event = KanbanTaskEvent(
            task_id=f"WORKER-{i}", board_slug="kontrol", kind=EventKind.BLOCKED,
            block_kind=BlockKind.TRANSIENT, title=f"Worker {i}", status="blocked",
            payload={"error": "429 Too Many Requests"},
        )
        assert governor.ingest_event(event, now=base + i * 0.2) is NotificationTier.P2_SILENT_DIGEST

    assert governor.storm_breaker.is_tripped(now=base + 5.0)
    assert governor.outbound_discord_pings == []
    assert governor.outbound_discord_cards == []
    assert [d for d in governor.outbound_discord_digests if d.get("type") == "storm_breaker_active"]


def test_storm_breaker_pauses_dispatch_while_tripped(governor):
    """§7.2: a tripped breaker is what the dispatcher consults before spawning."""
    base = 1000.0
    assert governor.dispatch_allowed(now=base) is True
    for i in range(3):
        governor.ingest_event(
            KanbanTaskEvent(task_id=f"W-{i}", board_slug="k", kind=EventKind.BLOCKED,
                            block_kind=BlockKind.TRANSIENT, title="w", status="blocked"),
            now=base + i,
        )
    assert governor.dispatch_allowed(now=base + 3) is False
    # Cooldown expiry re-opens spawning without any operator action.
    assert governor.dispatch_allowed(now=base + 3 + 3600) is True


# --- §9.4 Flapping -------------------------------------------------------------------


def test_flap_detection_damps_a_zombie_restart_loop(governor):
    base = 2000.0
    for i in range(4):
        governor.ingest_event(
            KanbanTaskEvent(task_id="FLAPPY", board_slug="kontrol", kind=EventKind.CRASHED,
                            title="Flappy task", status="running", payload={"error": "Segfault"}),
            now=base + i * 10.0,
        )

    assert "FLAPPY" in governor.circuit_tripped_tasks
    # Exactly one P1 notice survives the loop; the earlier crashes stayed in the digest.
    cards = [c for c in governor.outbound_discord_cards if c["task_id"] == "FLAPPY"]
    assert len(cards) == 1
    assert governor.decision_queue.queue["FLAPPY"].tier is NotificationTier.P1_REVIEW_QUEUE


# --- §9.5 Mode isolation -------------------------------------------------------------


def test_personal_mode_buffers_work_reviews_instead_of_dropping_them(governor, focus_state):
    """§1.5/§5.2C: work P1s are *paused in the queue* until the operator flips back to work."""
    focus_state.mode = PERSONAL

    governor.ingest_event(_review("KREO-100", title="Kreo Work Task", payload={"mode": WORK}))

    assert governor.outbound_discord_cards == []
    assert governor.outbound_discord_pings == []
    # No work telemetry reaches a personal-mode Totonou surface.
    assert governor.outbound_totonou_captures == []
    assert [d for d in governor.outbound_discord_digests if d.get("type") == "mode_suppressed"]
    # ...but the review is buffered, not lost.
    assert governor.decision_queue.queue["KREO-100"].status == "pending"

    focus_state.mode = WORK
    governor.on_totonou_mode_changed(WORK)
    assert [c["task_id"] for c in governor.outbound_discord_cards] == ["KREO-100"]


# --- §9.6 P0 ------------------------------------------------------------------------


def test_p0_pierces_the_focus_shield_but_rate_limits_the_mention(governor, focus_state):
    focus_state.focus_shield_active = True
    base = 3000.0

    tier = governor.ingest_event(
        KanbanTaskEvent(task_id="PANIC-1", board_slug="infra", kind=EventKind.CRITICAL_PANIC,
                        title="Kernel Memory Freeze Precursor", status="blocked",
                        payload={"error": "Zero-log freeze signature detected on marine"}),
        now=base,
    )
    assert tier is NotificationTier.P0_INTERRUPT
    assert governor.outbound_discord_pings[0]["mention"] == f"<@{OPERATOR_ID}>"

    tier = governor.ingest_event(
        KanbanTaskEvent(task_id="PANIC-2", board_slug="infra", kind=EventKind.SECURITY_BREACH,
                        title="Unauthorized Host Command", status="blocked",
                        payload={"error": "Tool attempted uncontained rm -rf /"}),
        now=base + 120.0,
    )
    assert tier is NotificationTier.P0_INTERRUPT
    assert len(governor.outbound_discord_pings) == 2
    # Inside the 15 minute cooldown the second incident is silent-but-visible.
    assert governor.outbound_discord_pings[1]["mention"] is None
    # Past the cooldown the next emergency may ping again.
    governor.ingest_event(
        KanbanTaskEvent(task_id="PANIC-3", board_slug="infra", kind=EventKind.CRITICAL_PANIC,
                        title="Disk read-only remount", status="blocked"),
        now=base + 1000.0,
    )
    assert governor.outbound_discord_pings[2]["mention"] == f"<@{OPERATOR_ID}>"


def test_p0_is_delivered_even_in_personal_mode(governor, focus_state):
    """Mode isolation gates work *reviews*, never a host/security invariant trip."""
    focus_state.mode = PERSONAL
    governor.ingest_event(
        KanbanTaskEvent(task_id="PANIC", board_slug="infra", kind=EventKind.SECURITY_BREACH,
                        title="Credential exfiltration attempt", status="blocked", payload={"mode": WORK}),
        now=5000.0,
    )
    assert len(governor.outbound_discord_pings) == 1


# --- §9 misc: starvation, milestones, deadlock ---------------------------------------


def test_unacknowledged_card_auto_defers_past_its_lease(governor):
    base = 4000.0
    governor.ingest_event(
        KanbanTaskEvent(task_id="STALE-1", board_slug="kontrol", kind=EventKind.BLOCKED,
                        block_kind=BlockKind.NEEDS_INPUT, title="Stale Question",
                        status="blocked", timestamp=base),
        now=base,
    )
    assert len(governor.decision_queue.queue) == 1

    timed_out = governor.decision_queue.check_timeouts(now=base + 2760.0)

    assert [c.task_id for c in timed_out] == ["STALE-1"]
    assert timed_out[0].status == "deferred"
    assert governor.decision_queue.active_card_task_id is None


def test_leaf_completion_is_silent_while_a_milestone_root_is_a_review(governor):
    leaf = governor.ingest_event(
        KanbanTaskEvent(task_id="SUBTASK-12", board_slug="kontrol", kind=EventKind.COMPLETED,
                        title="Helper function refactored", status="done",
                        payload={"is_milestone_root": False}),
    )
    assert leaf is NotificationTier.P2_SILENT_DIGEST
    assert governor.decision_queue.queue == {}

    root = governor.ingest_event(
        KanbanTaskEvent(task_id="EPIC-1", board_slug="kontrol", kind=EventKind.COMPLETED,
                        title="Harpy Pro Firmware v1.0 Release", status="done",
                        payload={"is_milestone_root": True, "summary": "Release binaries packaged"}),
    )
    assert root is NotificationTier.P1_REVIEW_QUEUE
    assert list(governor.decision_queue.queue) == ["EPIC-1"]


def test_swarm_deadlock_is_a_p0_interrupt(governor, focus_state):
    focus_state.focus_shield_active = True
    tier = governor.ingest_event(
        KanbanTaskEvent(task_id="SWARM-DEADLOCK", board_slug="kontrol", kind=EventKind.SWARM_DEADLOCK,
                        title="All 10 workers deadlocked", status="blocked",
                        payload={"reason": "Circular DAG lock detected across worktrees"}),
    )
    assert tier is NotificationTier.P0_INTERRUPT
    assert "EMERGENCY" in governor.outbound_discord_pings[0]["title"]


# --- Dual-mode Totonou resolver ------------------------------------------------------


class FakeTotonou:
    """``totonou_get_focus`` answers for ONE mode; the resolver must ask for both."""

    def __init__(self, per_mode: dict, *, fail: tuple = ()):
        self.per_mode = per_mode
        self.fail = fail
        self.calls: list[str] = []

    def totonou_get_focus(self, mode: str):
        self.calls.append(mode)
        if mode in self.fail:
            raise ConnectionError(f"totonou unreachable for {mode}")
        return self.per_mode.get(mode)


def test_resolver_probes_both_modes_because_the_api_is_mode_scoped():
    client = FakeTotonou({WORK: {"active": False}, PERSONAL: {"active": False}})

    state = resolve_totonou_focus(client)

    assert sorted(client.calls) == [PERSONAL, WORK]
    assert state.focus_shield_active is False
    assert state.mode == WORK


def test_resolver_reports_the_shield_for_whichever_mode_is_doing():
    client = FakeTotonou({
        WORK: {"active": True, "status": "doing", "task": "Kontrol USB descriptor", "id": "t-9"},
        PERSONAL: {"active": False},
    })

    state = resolve_totonou_focus(client)

    assert state.focus_shield_active is True
    assert state.mode == WORK
    assert state.active_task_title == "Kontrol USB descriptor"
    assert state.active_task_id == "t-9"


def test_personal_focus_wins_the_mode_so_work_never_breaches_personal_time():
    client = FakeTotonou({
        WORK: {"active": True, "status": "doing", "task": "Work thing"},
        PERSONAL: {"active": True, "status": "doing", "task": "Dinner"},
    })

    state = resolve_totonou_focus(client)

    assert state.focus_shield_active is True
    assert state.mode == PERSONAL


def test_a_paused_focus_task_is_not_a_shield():
    """§5.2A keys the shield on ``status == 'doing'``, not on a session merely existing."""
    client = FakeTotonou({WORK: {"active": True, "status": "parked"}, PERSONAL: {"active": False}})

    assert resolve_totonou_focus(client).focus_shield_active is False


def test_an_idle_probe_still_reports_the_operators_declared_mode():
    client = FakeTotonou({WORK: {"active": False}, PERSONAL: {"active": False, "mode": PERSONAL}})

    assert resolve_totonou_focus(client).mode == PERSONAL


def test_a_partial_probe_failure_still_uses_the_mode_that_answered():
    client = FakeTotonou({PERSONAL: {"active": True, "status": "doing"}}, fail=(WORK,))

    state = resolve_totonou_focus(client)

    assert state.focus_shield_active is True
    assert state.mode == PERSONAL
    assert state.degraded is False


def test_a_total_probe_failure_fails_open_rather_than_muting_the_swarm_forever():
    """An unreachable Totonou must not buffer every review indefinitely.

    A false "shield clear" costs one mention-free card; a false "shield active" silently
    swallows every HITL gate until someone notices.
    """
    client = FakeTotonou({}, fail=(WORK, PERSONAL))

    state = resolve_totonou_focus(client)

    assert state.focus_shield_active is False
    assert state.mode == WORK
    assert state.degraded is True


def test_resolver_accepts_a_bare_callable_probe():
    state = resolve_totonou_focus(lambda mode: {"active": mode == PERSONAL, "status": "doing"})

    assert state.focus_shield_active is True
    assert state.mode == PERSONAL


def test_governor_drives_the_resolver_through_a_mode_scoped_client():
    client = FakeTotonou({WORK: {"active": True, "status": "doing"}, PERSONAL: {"active": False}})
    gov = AttentionGovernor(
        discord_user_id=OPERATOR_ID, totonou_client=lambda: resolve_totonou_focus(client),
    )

    assert gov.ingest_event(_review("T-1")) is NotificationTier.P1_REVIEW_QUEUE
    assert gov.outbound_discord_cards == []
    assert client.calls.count(WORK) == 1 and client.calls.count(PERSONAL) == 1
