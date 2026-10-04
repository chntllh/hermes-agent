"""Deterministic local completion verification (plan Priority 4).

Local completion was purely nominal: `_substantive_text(val)` only asked
`bool(val and val.strip())`, so an agent that narrated success after a fatal
command still closed the card. Over 50 false completions were catalogued.

Cards with a GitHub `completion_contract` already have a real gate
(`kanban_pr_acceptance`). This adds the local equivalent: a machine-executable
`verification_command` that must exit 0, and `expected_artifacts` that must exist
on disk and be non-empty. Both are opt-in -- a card that declares neither keeps
the old evidence check, which is the documented rollback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def _make(conn, workspace, **kwargs):
    return kb.create_task(
        conn, title="t", workspace_kind="dir", workspace_path=str(workspace), **kwargs,
    )


def _status(conn, task_id):
    return kb.get_task(conn, task_id).status


# ---------------------------------------------------------------------------
# verification_command
# ---------------------------------------------------------------------------


def test_a_passing_verification_command_lets_the_card_close(kanban_home, workspace):
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command=f"{sys.executable} -c \"raise SystemExit(0)\"")
        assert kb.complete_task(conn, tid, result="built it") is True
        assert _status(conn, tid) == "done"


def test_a_failing_verification_command_blocks_completion(kanban_home, workspace):
    """Plan acceptance: a task with a failing test cannot be moved to done."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command=f"{sys.executable} -c \"raise SystemExit(3)\"")
        with pytest.raises(kb.TaskVerificationFailedError) as excinfo:
            kb.complete_task(conn, tid, result="all tests pass, honest")
        assert "exit code 3" in str(excinfo.value)
        assert _status(conn, tid) != "done"


def test_the_command_runs_inside_the_tasks_workspace(kanban_home, workspace):
    """`cwd` is the card's own workspace, not whatever directory the completer is in."""
    (workspace / "marker.txt").write_text("here\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _make(
            conn, workspace,
            verification_command=f"{sys.executable} -c \"import os,sys; sys.exit(0 if os.path.exists('marker.txt') else 1)\"",
        )
        assert kb.complete_task(conn, tid, result="done") is True


def test_a_blocked_completion_is_recorded_as_an_auditable_event(kanban_home, workspace):
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command=f"{sys.executable} -c \"raise SystemExit(1)\"")
        with pytest.raises(kb.TaskVerificationFailedError):
            kb.complete_task(conn, tid, result="x")
        kinds = [event.kind for event in kb.list_events(conn, tid)]
        assert "completion_blocked_verification" in kinds


def test_verification_failure_leaves_the_card_completable_after_a_real_fix(kanban_home, workspace):
    """The gate is recoverable, not terminal: fix the build, complete again."""
    gate = workspace / "gate"
    command = f"{sys.executable} -c \"import os,sys; sys.exit(0 if os.path.exists('gate') else 1)\""
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command=command)
        with pytest.raises(kb.TaskVerificationFailedError):
            kb.complete_task(conn, tid, result="x")
        gate.write_text("ok\n", encoding="utf-8")
        assert kb.complete_task(conn, tid, result="x") is True


def test_a_hanging_verification_command_fails_rather_than_wedging(kanban_home, workspace):
    with kbc.connect() as conn:
        tid = _make(
            conn, workspace,
            verification_command=f"{sys.executable} -c \"import time; time.sleep(30)\"",
        )
        with pytest.raises(kb.TaskVerificationFailedError, match="timed out"):
            kb.complete_task(conn, tid, result="x", verification_timeout=1)
        assert _status(conn, tid) != "done"


def test_an_unlaunchable_command_blocks_rather_than_passing(kanban_home, workspace):
    """Fail CLOSED: a command we could not run is not a command that succeeded."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command="definitely-not-a-real-binary-xyz --go")
        with pytest.raises(kb.TaskVerificationFailedError):
            kb.complete_task(conn, tid, result="x")


def test_a_missing_workspace_blocks_rather_than_running_somewhere_else(kanban_home, tmp_path):
    gone = tmp_path / "deleted"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", workspace_kind="dir", workspace_path=str(gone),
                             verification_command=f"{sys.executable} -c \"raise SystemExit(0)\"")
        with pytest.raises(kb.TaskVerificationFailedError, match="workspace"):
            kb.complete_task(conn, tid, result="x")


# ---------------------------------------------------------------------------
# expected_artifacts
# ---------------------------------------------------------------------------


def test_present_non_empty_artifacts_let_the_card_close(kanban_home, workspace):
    (workspace / "out.bin").write_bytes(b"\x00\x01")
    (workspace / "report.md").write_text("findings\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["out.bin", "report.md"])
        assert kb.complete_task(conn, tid, result="shipped") is True


def test_a_missing_artifact_blocks_completion(kanban_home, workspace):
    """Plan acceptance: a task with a missing output file cannot be moved to done."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["out.bin"])
        with pytest.raises(kb.TaskVerificationFailedError, match="out.bin"):
            kb.complete_task(conn, tid, result="wrote the file, promise")
        assert _status(conn, tid) != "done"


def test_an_empty_artifact_blocks_completion(kanban_home, workspace):
    """A zero-byte file is the signature of a build that died after `touch`/`> out`."""
    (workspace / "out.bin").write_bytes(b"")
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["out.bin"])
        with pytest.raises(kb.TaskVerificationFailedError, match="empty"):
            kb.complete_task(conn, tid, result="x")


def test_a_directory_does_not_satisfy_an_artifact(kanban_home, workspace):
    (workspace / "dist").mkdir()
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["dist"])
        with pytest.raises(kb.TaskVerificationFailedError):
            kb.complete_task(conn, tid, result="x")


def test_artifacts_cannot_escape_the_workspace(kanban_home, workspace, tmp_path):
    """A card must not satisfy itself with a file it did not produce."""
    outside = tmp_path / "preexisting.txt"
    outside.write_text("not mine\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["../preexisting.txt"])
        with pytest.raises(kb.TaskVerificationFailedError, match="outside"):
            kb.complete_task(conn, tid, result="x")


def test_an_absolute_artifact_path_is_refused(kanban_home, workspace, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=[str(outside)])
        with pytest.raises(kb.TaskVerificationFailedError, match="outside"):
            kb.complete_task(conn, tid, result="x")


def test_command_runs_before_artifacts_so_the_build_error_is_reported(kanban_home, workspace):
    """A failing build and a missing output are one problem; name the cause."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace,
                    verification_command=f"{sys.executable} -c \"raise SystemExit(7)\"",
                    expected_artifacts=["never-built.bin"])
        with pytest.raises(kb.TaskVerificationFailedError, match="exit code 7"):
            kb.complete_task(conn, tid, result="x")


# ---------------------------------------------------------------------------
# Opt-in, persistence and rollback
# ---------------------------------------------------------------------------


def test_a_card_declaring_neither_keeps_the_old_behaviour(kanban_home, workspace):
    """The documented rollback: clear the fields and the nominal check governs again."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace)
        assert kb.complete_task(conn, tid, result="prose only") is True
        tid2 = _make(conn, workspace)
        with pytest.raises(kb.EmptyCompletionError):
            kb.complete_task(conn, tid2, result="   ")


def test_fields_round_trip_through_the_task_row(kanban_home, workspace):
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command="make check",
                    expected_artifacts=["a.bin", "b.txt"])
        task = kb.get_task(conn, tid)
        assert task.verification_command == "make check"
        assert task.expected_artifacts == ["a.bin", "b.txt"]


def test_empty_declarations_are_stored_as_unset(kanban_home, workspace):
    with kbc.connect() as conn:
        tid = _make(conn, workspace, verification_command="   ", expected_artifacts=[])
        task = kb.get_task(conn, tid)
        assert task.verification_command is None
        assert task.expected_artifacts is None
        assert kb.complete_task(conn, tid, result="fine") is True


def test_review_approval_still_runs_the_gate(kanban_home, workspace):
    """A human approving out of `review` is exempt from the PROSE check, but a
    machine-checkable claim is still machine-checked."""
    with kbc.connect() as conn:
        tid = _make(conn, workspace, expected_artifacts=["out.bin"])
        kb.request_review(conn, tid, summary="please look")
        assert _status(conn, tid) == "review"
        with pytest.raises(kb.TaskVerificationFailedError):
            kb.complete_task(conn, tid)
        (workspace / "out.bin").write_text("ok\n", encoding="utf-8")
        assert kb.complete_task(conn, tid) is True


# ---------------------------------------------------------------------------
# Reachability: a gate nobody can declare is not a gate
# ---------------------------------------------------------------------------


def test_the_cli_can_declare_a_gate_end_to_end(kanban_home, workspace, capsys):
    """Through the real parser and the real `kanban create` handler."""
    import argparse
    import json

    from hermes_cli import kanban as kc
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "kanban", "create", "build the thing",
        "--workspace", f"dir:{workspace}",
        "--verify-with", "make check",
        "--expect-artifact", "out.bin",
        "--expect-artifact", "report.md",
        "--json",
    ])
    assert kc.kanban_command(args) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["verification_command"] == "make check"
    assert payload["expected_artifacts"] == ["out.bin", "report.md"]


def test_the_model_tool_advertises_the_gate():
    """The schema the model sees must carry both fields, or no card ever declares one."""
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA

    properties = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "verification_command" in properties
    assert properties["expected_artifacts"]["type"] == "array"
