"""Unit tests verifying profile isolation for subagent child runs and live mirroring.

Fixes Issue #120212:
1. _active_child_runs partitioned by (profile_home, session_key).
2. _child_run_active checks specific profile_home.
3. session.resume (_resume_locate) passes ctx.profile_home into _child_run_active,
   preventing cross-profile DB bypass.
4. _mirror_subagent_to_child looks up live sessions with the owning session's
   profile_home rather than _ANY_PROFILE, preventing cross-profile eavesdropping.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_subagent_iso")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")

    yield mod
    mod._sessions.clear()
    __import__("tui_gateway.server_requests", fromlist=["x"]).reset_for_tests()
    mod._child_mirrors.clear()
    mod._active_child_runs.clear()


@pytest.fixture()
def emits(server, monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: captured.append((event, sid, payload)),
    )
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True)
    return captured


def _relay(server, parent_sid: str, event_type: str, **payload):
    """Drive _on_tool_progress from a specific parent session."""
    server._on_tool_progress(
        parent_sid,
        event_type,
        payload.pop("tool_name", None),
        payload.pop("preview", None),
        None,
        goal="isolate subagents",
        task_count=1,
        task_index=0,
        **payload,
    )


def test_active_child_runs_partitioned_by_profile(server, tmp_path):
    prof_a = tmp_path / "profiles" / "alpha"
    prof_b = tmp_path / "profiles" / "beta"

    server._sessions["parent-a"] = {
        "session_key": "parent-a-key",
        "profile_home": str(prof_a),
    }

    _relay(server, "parent-a", "subagent.start", child_session_id="child-1", preview="start")

    # Partitioned: active under profile_a, but NOT profile_b and NOT launch profile (None)
    assert server._child_run_active("child-1", profile_home=prof_a) is True
    assert server._child_run_active("child-1", profile_home=str(prof_a)) is True
    assert server._child_run_active("child-1", profile_home=prof_b) is False
    assert server._child_run_active("child-1", profile_home=str(prof_b)) is False
    assert server._child_run_active("child-1", profile_home=None) is False
    assert server._child_run_active("child-1") is False

    # Check keys in _active_child_runs
    assert (str(prof_a), "child-1") in server._active_child_runs
    assert (str(prof_b), "child-1") not in server._active_child_runs
    assert (None, "child-1") not in server._active_child_runs


def test_cross_profile_mirror_eavesdropping_prevented(server, emits, tmp_path):
    """Events relayed from Profile A's parent session must NOT stream to Profile B's live window."""
    prof_a = str(tmp_path / "profiles" / "alpha")
    prof_b = str(tmp_path / "profiles" / "beta")

    # Parent session in Profile A
    server._sessions["parent-a"] = {"session_key": "parent-a-key", "profile_home": prof_a}

    # Profile B attacker has a live watch window opened for child-1
    server._sessions["live-child-b"] = {
        "session_key": "child-1",
        "profile_home": prof_b,
        "agent": None,
    }

    # Profile A legitimate user has a live watch window for child-1
    server._sessions["live-child-a"] = {
        "session_key": "child-1",
        "profile_home": prof_a,
        "agent": None,
    }

    # Parent-a relays subagent events
    _relay(server, "parent-a", "subagent.start", preview="secret child work", child_session_id="child-1")
    _relay(server, "parent-a", "subagent.tool", tool_name="terminal", preview="cat secrets.txt", child_session_id="child-1")
    _relay(server, "parent-a", "subagent.thinking", preview="found the credentials", child_session_id="child-1")
    _relay(server, "parent-a", "subagent.text", preview="API_KEY=12345", child_session_id="child-1")
    _relay(server, "parent-a", "subagent.complete", child_session_id="child-1", status="completed", summary="all done")

    # Profile B received NOTHING
    emits_b = [(e, s, p) for e, s, p in emits if s == "live-child-b"]
    assert emits_b == [], f"Cross-profile eavesdropping detected: {emits_b}"

    # Profile A received the full native mirrored stream
    emits_a = [e for e, s, _ in emits if s == "live-child-a"]
    assert emits_a == [
        "message.start",
        "message.delta",
        "tool.start",
        "reasoning.delta",
        "message.delta",
        "tool.complete",
        "message.complete",
    ]


def test_cross_profile_lazy_resume_db_bypass_prevented(server, tmp_path, monkeypatch):
    """Resuming an in-flight child in Profile B must NOT bypass DB check when child is active in Profile A."""
    prof_a = tmp_path / "profiles" / "alpha"
    prof_b = tmp_path / "profiles" / "beta"
    prof_a.mkdir(parents=True, exist_ok=True)
    prof_b.mkdir(parents=True, exist_ok=True)

    server._sessions["parent-a"] = {
        "session_key": "parent-a-key",
        "profile_home": str(prof_a),
    }

    _relay(server, "parent-a", "subagent.start", child_session_id="child-1", preview="start")
    assert server._child_run_active("child-1", profile_home=prof_a) is True

    # Setup mock profile dbs where child-1 does not exist
    class _MockDB:
        def get_session(self, _id):
            return None

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _id):
            return None

        def get_messages_as_conversation(self, _id, **_k):
            return []

        def get_history(self, _id, **_k):
            return []

        def close(self):
            pass

    monkeypatch.setattr(server, "_profile_session_db", lambda home: (_MockDB(), True))
    monkeypatch.setattr(server, "_profile_home", lambda p: prof_b if p == "beta" else (prof_a if p == "alpha" else None))

    # Profile B tries to lazy-resume child-1
    res_b = server.handle_request({
        "id": "req-b",
        "method": "session.resume",
        "params": {"session_id": "child-1", "profile": "beta", "lazy": True},
    })
    # DB check was NOT bypassed -> 4007 session not found
    assert "error" in res_b
    assert res_b["error"]["code"] == 4007

    # Profile A lazy-resumes child-1 -> bypasses DB check because child is active in profile_a
    monkeypatch.setattr(server, "_live_session_payload", lambda sid, session, **_k: {"session_id": sid, "message_count": 0, "messages": [], "info": {}})
    res_a = server.handle_request({
        "id": "req-a",
        "method": "session.resume",
        "params": {"session_id": "child-1", "profile": "alpha", "lazy": True},
    })
    assert "result" in res_a, f"Expected success for profile_a, got: {res_a}"
    assert res_a["result"]["resumed"] == "child-1"


def test_completion_cleans_up_profile_partitioned_registry(server, tmp_path):
    prof_a = str(tmp_path / "profiles" / "alpha")
    prof_b = str(tmp_path / "profiles" / "beta")

    server._sessions["parent-a"] = {"session_key": "parent-a-key", "profile_home": prof_a}
    server._sessions["parent-b"] = {"session_key": "parent-b-key", "profile_home": prof_b}

    # Start children with same session key in different profiles
    _relay(server, "parent-a", "subagent.start", child_session_id="child-shared", preview="a")
    _relay(server, "parent-b", "subagent.start", child_session_id="child-shared", preview="b")

    assert server._child_run_active("child-shared", profile_home=prof_a) is True
    assert server._child_run_active("child-shared", profile_home=prof_b) is True

    # Complete only in profile_a
    _relay(server, "parent-a", "subagent.complete", child_session_id="child-shared", status="completed")

    assert server._child_run_active("child-shared", profile_home=prof_a) is False
    assert (prof_a, "child-shared") not in server._active_child_runs
    # profile_b remains active
    assert server._child_run_active("child-shared", profile_home=prof_b) is True
    assert (prof_b, "child-shared") in server._active_child_runs


def test_prompt_submit_rejected_only_in_owning_profile(server, tmp_path):
    """Typing into a watch window is rejected only in the profile where the child is active."""
    prof_a = str(tmp_path / "profiles" / "alpha")
    prof_b = str(tmp_path / "profiles" / "beta")

    server._sessions["parent-a"] = {"session_key": "parent-a-key", "profile_home": prof_a}
    _relay(server, "parent-a", "subagent.tool", tool_name="terminal", child_session_id="child-1")

    # Watch window in profile_a
    server._sessions["watch-a"] = {
        "agent": None,
        "history_lock": threading.Lock(),
        "lazy": True,
        "running": False,
        "session_key": "child-1",
        "profile_home": prof_a,
    }

    # Watch window in profile_b
    server._sessions["watch-b"] = {
        "agent": None,
        "history_lock": threading.Lock(),
        "lazy": True,
        "running": False,
        "session_key": "child-1",
        "profile_home": prof_b,
    }

    # Submit in profile_a is rejected with 4009 (busy)
    res_a = server._methods["prompt.submit"]("rid-a", {"session_id": "watch-a", "text": "hello"})
    assert res_a["error"]["code"] == 4009

    # Submit in profile_b is NOT rejected with 4009
    res_b = server._methods["prompt.submit"]("rid-b", {"session_id": "watch-b", "text": "hello"})
    assert res_b is None or res_b.get("error", {}).get("code") != 4009


def test_launch_profile_child_run_isolated_from_secondary_profile(server, tmp_path):
    """Launch profile (profile_home=None) child runs are isolated from secondary profiles."""
    prof_custom = str(tmp_path / "profiles" / "custom")

    # Parent in launch profile has no profile_home (or None)
    server._sessions["parent-launch"] = {"session_key": "parent-launch-key", "profile_home": None}

    # Watch window in secondary profile
    server._sessions["watch-custom"] = {
        "session_key": "child-launch",
        "profile_home": prof_custom,
        "agent": None,
    }
    # Watch window in launch profile
    server._sessions["watch-launch"] = {
        "session_key": "child-launch",
        "profile_home": None,
        "agent": None,
    }

    _relay(server, "parent-launch", "subagent.start", preview="launch work", child_session_id="child-launch")

    assert server._child_run_active("child-launch", profile_home=None) is True
    assert server._child_run_active("child-launch") is True
    assert server._child_run_active("child-launch", profile_home=prof_custom) is False

    assert (None, "child-launch") in server._active_child_runs
    assert (prof_custom, "child-launch") not in server._active_child_runs
