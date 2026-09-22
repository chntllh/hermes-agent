"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import os
import tempfile
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_wa_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


def _set_approval(subsystem, enabled):
    import hermes_cli.config as cfg
    c = cfg.load_config()
    c.setdefault(subsystem, {})["write_approval"] = enabled
    cfg.save_config(c)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def test_default_gate_is_off(hermes_home):
    from tools import write_approval as wa
    # Default: gate off → writes flow freely.
    assert wa.write_approval_enabled("memory") is False
    assert wa.write_approval_enabled("skills") is False


def test_invalid_subsystem_is_off(hermes_home):
    from tools import write_approval as wa
    assert wa.write_approval_enabled("bogus") is False


def test_list_pending_skips_non_dict_record(hermes_home):
    """A parseable-but-non-object pending file must be skipped, not crash the sort."""
    from tools import write_approval as wa
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "ok"},
                   summary="ok", origin="foreground")
    pending_dir = wa._pending_path("memory", "").parent
    (pending_dir / "bad.json").write_text('"not a record"', encoding="utf-8")
    records = wa.list_pending("memory")
    assert len(records) == 1 and records[0]["payload"]["content"] == "ok"
    assert wa.get_pending("memory", "bad") is None


def test_discard_pending_archives_resolution(hermes_home):
    """Approved/rejected staged writes remain recoverable before deletion."""
    from tools import write_approval as wa

    record = wa.stage_write(
        "memory", {"action": "add", "target": "user", "content": "recover me"},
        summary="recover me", origin="background_review",
    )
    assert wa.discard_pending("memory", record["id"], outcome="rejected") is True
    assert wa.pending_count("memory") == 0

    archived = Path(hermes_home) / "archive" / "pending" / "memory" / f"{record['id']}.json"
    saved = json.loads(archived.read_text(encoding="utf-8"))
    assert saved["id"] == record["id"]
    assert saved["resolution"] == "rejected"
    assert saved["payload"]["content"] == "recover me"


def test_stage_write_fails_closed_when_record_cannot_persist(hermes_home, monkeypatch):
    """Never report a pending ID when no durable pending record was written."""
    from tools import write_approval as wa

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wa, "atomic_json_write", _boom)
    with pytest.raises(RuntimeError, match="Could not persist pending memory write"):
        wa.stage_write(
            "memory", {"action": "add", "target": "memory", "content": "lost"},
            summary="lost", origin="background_review",
        )
    assert wa.pending_count("memory") == 0


def test_stage_write_fsyncs_pending_directory(hermes_home, monkeypatch):
    """A returned pending ID must survive a crash after the atomic rename."""
    from tools import write_approval as wa

    calls = []
    real_write = wa.atomic_json_write

    def tracked_write(path, record, **kwargs):
        calls.append(kwargs)
        return real_write(path, record, **kwargs)

    monkeypatch.setattr(wa, "atomic_json_write", tracked_write)
    wa.stage_write("memory", {"action": "add", "target": "memory", "content": "durable"},
                   summary="durable", origin="background_review")
    assert calls == [{"fsync_dir": True}]


def test_approve_rejects_changed_memory_base_revision(hermes_home):
    """A proposal is compare-and-swap against the memory projection it reviewed."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    assert store.add("memory", "canonical fact")["success"] is True
    record = wa.stage_write(
        "memory", {
            "action": "replace", "target": "memory",
            "old_text": "canonical fact", "content": "proposed fact",
        },
        summary="replace canonical fact", origin="background_review",
    )
    assert record["schema_version"] == 2
    assert record["provenance"]["source_host"]
    assert record["base"]["target"] == "memory"
    assert record["base"]["sha256"]

    assert store.add("memory", "concurrent fact")["success"] is True
    out = handle_pending_subcommand(wa.MEMORY, ["approve", record["id"]], memory_store=store)

    assert out and "base revision changed" in out
    assert wa.pending_count("memory") == 1
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert "proposed fact" not in reloaded.memory_entries
    assert "canonical fact" in reloaded.memory_entries

def test_approve_checks_base_after_acquiring_memory_file_lock(hermes_home, monkeypatch):
    """A writer that wins the file lock before approval must invalidate the proposal.

    This deterministically places the competing write after the approval thread begins
    but before it can acquire the MemoryStore transaction lock. The old split
    validate-then-apply path approved the stale proposal; CAS must run inside that lock.
    """
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    assert store.add("memory", "canonical fact")["success"] is True
    record = wa.stage_write(
        "memory", {"action": "replace", "target": "memory", "old_text": "canonical fact", "content": "proposed fact"},
        summary="replace canonical fact", origin="background_review",
    )
    path = MemoryStore._path_for("memory")
    original_lock = MemoryStore._file_lock
    attempting_transaction = threading.Event()

    @contextmanager
    def observed_lock(lock_path):
        attempting_transaction.set()
        with original_lock(lock_path):
            yield

    monkeypatch.setattr(MemoryStore, "_file_lock", staticmethod(observed_lock))
    outcome = []

    with original_lock(path):
        thread = threading.Thread(
            target=lambda: outcome.append(handle_pending_subcommand(
                wa.MEMORY, ["approve", record["id"]], memory_store=store)),
        )
        thread.start()
        assert attempting_transaction.wait(timeout=2), "approval never attempted the memory transaction"
        path.write_text("canonical fact\n§\nconcurrent fact", encoding="utf-8")
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcome and "base revision changed" in outcome[0]
    assert wa.pending_count("memory") == 1
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert reloaded.memory_entries == ["canonical fact", "concurrent fact"]


def test_approve_does_not_report_success_when_archiving_fails(hermes_home, monkeypatch):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    record = wa.stage_write("memory", {"action": "add", "target": "memory", "content": "applied but retained"},
                            summary="archive failure", origin="background_review")
    monkeypatch.setattr(wa, "discard_pending", lambda *_args, **_kwargs: False)

    out = handle_pending_subcommand(wa.MEMORY, ["approve", record["id"]], memory_store=store)
    assert "Approved" not in out
    assert "No memory writes were finalized" in out
    assert "archive/discard failed" in out
    assert "applied but retained" in store.memory_entries


def test_schema_v2_missing_or_malformed_base_fails_closed_but_legacy_replays(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    for broken_base in (None, {"target": "memory", "sha256": "not-a-hash"}):
        record = wa.stage_write("memory", {"action": "add", "target": "memory", "content": "blocked"},
                                summary="blocked", origin="background_review")
        path = wa._pending_path("memory", record["id"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        if broken_base is None:
            saved.pop("base")
        else:
            saved["base"] = broken_base
        path.write_text(json.dumps(saved), encoding="utf-8")
        out = handle_pending_subcommand(wa.MEMORY, ["approve", record["id"]], memory_store=store)
        assert "missing or malformed schema-v2 base revision" in out
        assert wa.get_pending("memory", record["id"]) is not None

    legacy_id = "abcdef12"
    legacy = {"id": legacy_id, "subsystem": "memory", "payload": {
        "action": "add", "target": "memory", "content": "legacy remains compatible"}}
    legacy_path = wa._pending_path("memory", legacy_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", legacy_id], memory_store=store)
    assert "Approved 1 memory write" in out
    assert "legacy remains compatible" in store.memory_entries


def test_pending_id_and_record_identity_are_strict(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write("memory", {"action": "add", "target": "memory", "content": "identity"},
                            summary="identity", origin="background_review")
    path = wa._pending_path("memory", record["id"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["id"] = "deadbeef"
    path.write_text(json.dumps(saved), encoding="utf-8")

    assert wa.list_pending("memory") == []
    assert wa.get_pending("memory", record["id"]) is None
    assert wa.discard_pending("memory", record["id"]) is False
    assert wa.get_pending("memory", "../../config") is None

    skill = wa.stage_write("skills", {"action": "delete", "name": "demo"},
                           summary="wrong subsystem", origin="background_review")
    skill_path = wa._pending_path("skills", skill["id"])
    skill_saved = json.loads(skill_path.read_text(encoding="utf-8"))
    skill_saved["subsystem"] = "memory"
    skill_path.write_text(json.dumps(skill_saved), encoding="utf-8")
    assert wa.list_pending("skills") == []
    assert wa.get_pending("skills", skill["id"]) is None


def test_normalize_enabled_coerces_values():
    from tools import write_approval as wa
    # Real bools pass through.
    assert wa._normalize_enabled(True) is True
    assert wa._normalize_enabled(False) is False
    # Truthy strings → True (incl. legacy 'approve').
    assert wa._normalize_enabled("on") is True
    assert wa._normalize_enabled("approve") is True
    assert wa._normalize_enabled("true") is True
    # Everything else → False (gate off is the safe default).
    assert wa._normalize_enabled("off") is False
    assert wa._normalize_enabled("garbage") is False
    assert wa._normalize_enabled(None) is False


# ---------------------------------------------------------------------------
# Memory gate
# ---------------------------------------------------------------------------

def test_memory_gate_off_allows_write(hermes_home):
    # Default (gate off) → write straight through, no staging.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "user", "save me", store=store))
    assert r["success"] is True
    assert r["entry_count"] == 1
    assert wa.pending_count("memory") == 0


def test_cli_memory_approve_without_live_agent_uses_fresh_store(hermes_home, capsys):
    """#46783: ``/memory approve`` from a context with no live agent (e.g. the
    Desktop GUI) passed ``memory_store=None`` into the shared handler, which
    returned "memory store unavailable" and applied nothing. The CLI handler must
    fall back to a freshly loaded on-disk store, like the gateway path does."""
    import json
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    _set_approval("memory", True)
    staging = MemoryStore(); staging.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "remember the launch date", store=staging))
    assert r.get("pending_id"), r
    assert wa.pending_count("memory") == 1

    # Bare CLI handler with no live agent → store resolves to None pre-fix.
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memory_command("/memory approve all")

    out = capsys.readouterr().out
    assert "memory store unavailable" not in out, out
    assert "Approved 1" in out, out
    assert wa.pending_count("memory") == 0
    # The approved write landed in a freshly loaded on-disk store (MEMORY.md).
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert any("remember the launch date" in e for e in reloaded.memory_entries)


def test_load_on_disk_store_honors_configured_limits_and_permissions(hermes_home, monkeypatch):
    """Fresh approval stores must match the live agent's limits and target gates."""
    from tools.memory_tool import load_on_disk_store

    # Config override path: helper picks up configured limits and store flags.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "memory": {
                "memory_char_limit": 999,
                "user_char_limit": 444,
                "memory_enabled": False,
                "user_profile_enabled": True,
            }
        },
    )
    store = load_on_disk_store()
    assert store.memory_char_limit == 999
    assert store.user_char_limit == 444
    assert store.memory_enabled is False
    assert store.user_profile_enabled is True

    # Failure path: config raises → defaults, never blows up.
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    fallback = load_on_disk_store()
    assert fallback.memory_char_limit == 2200
    assert fallback.user_char_limit == 1375
    assert fallback.memory_enabled is True
    assert fallback.user_profile_enabled is True


# ---------------------------------------------------------------------------
# Skill gate
# ---------------------------------------------------------------------------

_SKILL = (
    "---\nname: test-skill\ndescription: A test skill\nversion: 1.0.0\n---\n"
    "# Test\nbody\n"
)


# ---------------------------------------------------------------------------
# Pending store CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared command handler
# ---------------------------------------------------------------------------


def test_handle_approve_all_refuses_revision_fenced_memory_queue(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "a"},
                   summary="a", origin="foreground")
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "b"},
                   summary="b", origin="foreground")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert out and "Refusing approve all" in out
    assert wa.pending_count("memory") == 2
    assert store.user_entries == []


def test_handle_approval_on(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.MEMORY, ["approval", "on"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is True
    assert "on" in out


def test_handle_approval_off(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.SKILLS, ["approval", "off"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is False
    assert "off" in out


# ---------------------------------------------------------------------------
# Inline (interactive CLI) approval path — regression for the bug where the
# per-thread approval callback was never passed to prompt_dangerous_approval,
# so every gated foreground memory write was silently denied.
# ---------------------------------------------------------------------------

@pytest.fixture
def approval_callback_cleanup():
    yield
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)


def test_memory_inline_approve_writes(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)

    calls = []
    def approve_cb(command, description, **kw):
        calls.append((command, description))
        return "once"
    set_approval_callback(approve_cb)

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "approved fact", store=store))
    assert r["success"] is True
    assert r.get("staged") is None  # real write, not staged
    assert store.memory_entries == ["approved fact"]
    assert wa.pending_count("memory") == 0
    # The registered callback must actually be invoked (not the input() path).
    assert len(calls) == 1
    assert "approved fact" in calls[0][0]


def test_memory_inline_deny_blocks(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)
    set_approval_callback(lambda command, description, **kw: "deny")

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "denied fact", store=store))
    assert r["success"] is False
    assert "denied" in r["error"].lower()
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 0  # denied, not staged


def test_memory_invalid_params_rejected_before_staging(hermes_home):
    # Param validation must run BEFORE the gate so a broken write is rejected
    # immediately instead of staged and failing at approve time.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", None, store=store))
    assert r["success"] is False
    assert wa.pending_count("memory") == 0


class TestSkillGist:
    """skill_gist builds a heuristic one-line summary for a pending skill write.

    Pure, no model call — every branch is verifiable from the function source.
    """

    def test_create_with_frontmatter_description(self):
        from tools import write_approval as wa
        content = "---\ndescription: My cool skill\n---\nprint('hi')\n"
        assert (
            wa.skill_gist("create", "demo", content=content)
            == f"create 'demo' — My cool skill ({len(content)} chars)"
        )

    def test_edit_without_description_uses_size_only(self):
        from tools import write_approval as wa
        content = "no frontmatter here"
        assert (
            wa.skill_gist("edit", "demo", content=content)
            == f"rewrite 'demo' ({len(content)} chars)"
        )


    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"
