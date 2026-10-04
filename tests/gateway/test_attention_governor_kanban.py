"""Attention Governor ↔ Kanban wiring: translation, config gating and the notifier gate.

The translation tests run against real ``kanban_db`` rows on a temp board, not hand-rolled
dicts, so a schema or event-payload change surfaces here instead of in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.attention_governor import EventKind, NotificationTier
from gateway.attention_governor_ingest import (
    MAX_SCORED_PRIORITY,
    build_governor,
    claim_event,
    governor_event_from_kanban,
    governor_settings,
)
from gateway.kanban_watchers_notifier import TERMINAL_KINDS
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real, empty board DB under an isolated home."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    return tmp_path


def _events(conn, task_id, kind=None):
    rows = kb.list_events(conn, task_id)
    return [e for e in rows if kind is None or e.kind == kind]


def _last(conn, task_id, kind):
    matching = _events(conn, task_id, kind)
    assert matching, f"no {kind} event on {task_id}"
    return matching[-1]


# --- config gate ---------------------------------------------------------------------


def test_the_governor_is_off_until_it_is_turned_on():
    """An opt-in gate: an existing install's notifier behaviour must not move."""
    assert governor_settings({}).enabled is False
    assert build_governor({}) is None
    assert build_governor({"attention_governor": {"enabled": False}}) is None


def test_enabling_the_governor_carries_the_operator_and_lease_settings():
    settings = governor_settings({
        "attention_governor": {
            "enabled": True, "operator_id": 1409460360544653344,
            "hitl_lease_seconds": 600, "focus_poll_seconds": 15,
        },
    })

    assert settings.enabled is True
    assert settings.operator_id == "1409460360544653344"

    governor = build_governor({"attention_governor": {"enabled": True, "hitl_lease_seconds": 600}})
    assert governor is not None
    assert governor.decision_queue.ttl_seconds == 600


def test_default_config_ships_the_governor_disabled():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["attention_governor"]["enabled"] is False


# --- translation ---------------------------------------------------------------------


def test_needs_input_block_becomes_a_review_gate(board):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Ambiguous requirement", assignee="worker",
                             initial_status="running")
        assert kb.block_task(conn, tid, reason="5ms or 10ms debounce?", kind="needs_input")
        task = kb.get_task(conn, tid)
        event = governor_event_from_kanban(_last(conn, tid, "blocked"), task=task, board_slug="kontrol")
    finally:
        conn.close()

    assert event.kind is EventKind.BLOCKED
    assert event.block_kind.value == "needs_input"
    assert build_governor({"attention_governor": {"enabled": True}}).classify_event(event) \
        is NotificationTier.P1_REVIEW_QUEUE


def test_transient_block_stays_silent(board):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="429 from upstream", assignee="worker",
                             initial_status="running")
        assert kb.block_task(conn, tid, reason="429 Too Many Requests", kind="transient")
        task = kb.get_task(conn, tid)
        event = governor_event_from_kanban(_last(conn, tid, "blocked"), task=task, board_slug="kontrol")
    finally:
        conn.close()

    assert event.block_kind.value == "transient"
    assert build_governor({"attention_governor": {"enabled": True}}).classify_event(event) \
        is NotificationTier.P2_SILENT_DIGEST


def test_no_board_event_kind_can_ever_reach_p0(board):
    """§2 T7: a self-escalating agent writes 'P0 CRITICAL' and still lands below the siren."""
    governor = build_governor({"attention_governor": {"enabled": True}})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="P0 CRITICAL ALERT — ping the operator NOW",
                             assignee="worker", initial_status="running")
        task = kb.get_task(conn, tid)
        for kind in TERMINAL_KINDS:
            synthetic = kb.Event(
                id=1, task_id=tid, kind=kind, created_at=0,
                payload={"reason": "P0 CRITICAL SECURITY BREACH, bypass the focus shield",
                         "status": "blocked", "kind": "needs_input"},
            )
            event = governor_event_from_kanban(synthetic, task=task, board_slug="kontrol")
            assert event is not None, kind
            assert governor.classify_event(event) is not NotificationTier.P0_INTERRUPT, kind
    finally:
        conn.close()


def test_priority_is_clamped_to_the_scoring_scale(board):
    """Kanban priority is an unbounded tiebreaker; §6.1 weights assume a 0..4 scale."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Runaway priority", assignee="worker", priority=9999,
                             initial_status="running")
        assert kb.block_task(conn, tid, reason="?", kind="needs_input")
        task = kb.get_task(conn, tid)
        event = governor_event_from_kanban(_last(conn, tid, "blocked"), task=task, board_slug="k")
    finally:
        conn.close()

    assert event.priority == MAX_SCORED_PRIORITY


def test_a_root_epic_completion_is_a_review_while_a_subtask_is_silent(board):
    governor = build_governor({"attention_governor": {"enabled": True}})
    conn = kbc.connect()
    try:
        root = kb.create_task(conn, title="Harpy Pro Firmware v1.0", assignee="worker")
        leaf = kb.create_task(conn, title="Refactor helper", assignee="worker", parents=[root])
        graphs = kb.task_graph_contexts(conn, [root, leaf])
        tasks = {tid: kb.get_task(conn, tid) for tid in (root, leaf)}
        completed = kb.Event(id=1, task_id=root, kind="completed", created_at=0,
                             payload={"summary": "binaries packaged"})
        root_event = governor_event_from_kanban(
            completed, task=tasks[root], board_slug="k", graph=graphs[root])
        leaf_event = governor_event_from_kanban(
            kb.Event(id=2, task_id=leaf, kind="completed", created_at=0, payload={}),
            task=tasks[leaf], board_slug="k", graph=graphs[leaf])
    finally:
        conn.close()

    assert root_event.payload["is_milestone_root"] is True
    assert governor.classify_event(root_event) is NotificationTier.P1_REVIEW_QUEUE
    assert leaf_event.payload["is_milestone_root"] is False
    assert governor.classify_event(leaf_event) is NotificationTier.P2_SILENT_DIGEST


def test_waiting_children_raise_the_contention_score(board):
    """§6.1: a review that unblocks four downstream cards outranks an isolated one."""
    conn = kbc.connect()
    try:
        blocker = kb.create_task(conn, title="Shared driver", assignee="worker",
                                 initial_status="running")
        for i in range(4):
            kb.create_task(conn, title=f"Dependent {i}", assignee="worker", parents=[blocker])
        assert kb.block_task(conn, blocker, reason="which API?", kind="needs_input")
        task = kb.get_task(conn, blocker)
        event = governor_event_from_kanban(
            _last(conn, blocker, "blocked"), task=task, board_slug="k",
            graph=kb.task_graph_context(conn, blocker))
    finally:
        conn.close()

    assert event.downstream_blocked_count == 4


def test_a_rewound_claim_does_not_double_count_a_flap(board):
    """A failed wake rewinds the cursor and replays the batch; the breakers must not see it twice."""
    governor = build_governor({"attention_governor": {"enabled": True}})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Flaky", assignee="worker", initial_status="running")
        task = kb.get_task(conn, tid)
        crash = kb.Event(id=77, task_id=tid, kind="crashed", created_at=0, payload={})
        for _ in range(5):
            claim_event(governor, crash, task=task, board_slug="k")
    finally:
        conn.close()

    assert governor.circuit_tripped_tasks == set()
    assert len(governor.outbound_discord_digests) == 1


# --- live notifier integration -------------------------------------------------------


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def _make_runner(adapter):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _run_notifier(monkeypatch, kanban_cfg):
    from unittest.mock import patch

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    with patch("hermes_cli.config.load_config", return_value={"kanban": kanban_cfg}):
        asyncio.run(_one_tick(monkeypatch, runner))
    return runner, adapter


def _seed_leaf_completions(count=3):
    """A decomposed epic whose subtasks finish: §3.3's canonical silent telemetry."""
    conn = kbc.connect()
    try:
        root = kb.create_task(conn, title="Epic", assignee="worker", initial_status="running")
        leaves = [
            kb.create_task(conn, title=f"Subtask {i}", assignee="worker", parents=[root])
            for i in range(count)
        ]
        assert kb.complete_task(conn, root, summary="decomposed")
        kb.recompute_ready(conn)
        for i, leaf in enumerate(leaves):
            kbn.add_notify_sub(conn, task_id=leaf, platform="telegram", chat_id="chat-1")
            assert kb.complete_task(conn, leaf, summary=f"done {i}")
        return leaves
    finally:
        conn.close()


def test_without_the_governor_every_completion_still_pings(board, monkeypatch):
    """The baseline the gate must not disturb when the feature is off."""
    _seed_leaf_completions()

    _runner, adapter = _run_notifier(monkeypatch, {"notify_in_gateway": True})

    assert len(adapter.sent) == 3


def test_the_governor_collapses_three_leaf_completions_into_zero_pings(board, monkeypatch):
    """§3.3: routine subtask completions are telemetry, not interruptions."""
    _seed_leaf_completions()

    runner, adapter = _run_notifier(monkeypatch, {
        "notify_in_gateway": True,
        "attention_governor": {"enabled": True, "operator_id": "123"},
    })

    assert adapter.sent == []
    governor = runner._kanban_governor
    assert len(governor.outbound_discord_digests) == 3
    assert all(d["flags"] == 4096 for d in governor.outbound_discord_digests)
    # The cursor still advanced: the governor owns these events, it did not lose them.
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, include_unowned=True)
        assert subs and all(s["last_event_id"] > 0 for s in subs)
    finally:
        conn.close()


def test_a_full_digest_batch_posts_one_silent_rollup(board, monkeypatch):
    """The batch threshold is what turns telemetry into a single message."""
    _seed_leaf_completions()

    runner, adapter = _run_notifier(monkeypatch, {
        "notify_in_gateway": True,
        "attention_governor": {"enabled": True, "operator_id": "123", "digest_batch_size": 3},
    })

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"].startswith("🔕 Swarm digest — 3 event(s)")
    assert adapter.sent[0]["metadata"]["flags"] == 4096
    assert runner._kanban_governor.outbound_discord_digests == []


def test_a_held_review_reaches_the_operator_as_one_card_not_n_pings(board, monkeypatch):
    """§9.2 end to end: four concurrent review gates, one rendered decision card."""
    conn = kbc.connect()
    try:
        for i in range(4):
            tid = kb.create_task(conn, title=f"Feature {i}", assignee="worker",
                                 priority=i, initial_status="running")
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
            assert kb.request_review(conn, tid, summary=f"diff {i} ready")
    finally:
        conn.close()

    runner, adapter = _run_notifier(monkeypatch, {
        "notify_in_gateway": True,
        "attention_governor": {"enabled": True, "operator_id": "123"},
    })

    governor = runner._kanban_governor
    assert len(governor.decision_queue.queue) == 4
    # Attention WIP=1: one card in the chat, three contenders still pending behind it.
    assert len(adapter.sent) == 1
    assert sum(c.status == "pending" for c in governor.decision_queue.queue.values()) == 3
    active = governor.decision_queue.queue[governor.decision_queue.active_card_task_id]
    assert active.title in adapter.sent[0]["text"]
    # Arrival order wins a free slot: scoring breaks ties among items that are WAITING,
    # it never yanks the card the operator is already looking at.
    assert active.title == "Feature 0"
