#!/usr/bin/env python3
"""Write-approval gate + pending store for memory and skill writes.

A per-subsystem boolean ``write_approval`` gates the agent's cross-session writes —
**memory** (MEMORY.md / USER.md) and **skills** (SKILL.md + files) — from either
origin (**foreground** turn or **background_review** fork). ``false`` (default)
writes freely; ``true`` never commits directly: it prompts inline (memory,
interactive CLI only) or **stages** the write under
``<HERMES_HOME>/pending/{memory,skills}/<id>.json`` for out-of-band review.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import socket
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Subsystem identifiers
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)

# Per-subsystem config key. Intentionally a single boolean with no "block all writes"
# state — to disable a subsystem use its own enable flag (e.g. ``memory.memory_enabled``).
CONFIG_KEY = "write_approval"
_TRUTHY_STRINGS = frozenset({"on", "true", "yes", "1", "approve", "enabled"})
_PENDING_ID_RE = re.compile(r"[0-9a-f]{8}")


def _valid_pending_id(pending_id: Any) -> bool:
    return isinstance(pending_id, str) and _PENDING_ID_RE.fullmatch(pending_id) is not None


def _valid_subsystem(subsystem: Any) -> bool:
    return subsystem in _SUBSYSTEMS


def _record_matches_location(record: Any, subsystem: str, pending_id: str) -> bool:
    return (isinstance(record, dict) and record.get("id") == pending_id
            and record.get("subsystem") == subsystem)


# --- Config resolution ---

def write_approval_enabled(subsystem: str) -> bool:
    """Read ``<subsystem>.write_approval``; any unset/invalid value means gate off."""
    if subsystem not in _SUBSYSTEMS:
        return False
    try:
        from hermes_cli.config import load_config, cfg_get
        return _normalize_enabled(cfg_get(load_config(), subsystem, CONFIG_KEY, default=False))
    except Exception:
        return False


def _normalize_enabled(value: Any) -> bool:
    """Coerce a config value to bool; unknown → False (gate off). The string branch
    covers hand-edited configs (YAML already parses bare on/off/yes/no)."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY_STRINGS


# --- Pending store (file-backed) ---

def _pending_path(subsystem: str, pending_id: str) -> Path:
    if not _valid_subsystem(subsystem):
        raise ValueError(f"invalid pending subsystem: {subsystem!r}")
    if pending_id and not _valid_pending_id(pending_id):
        raise ValueError(f"invalid pending id: {pending_id!r}")
    return get_hermes_home() / "pending" / subsystem / f"{pending_id}.json"


def _pending_files(subsystem: str) -> list:
    if not _valid_subsystem(subsystem):
        return []
    d = _pending_path(subsystem, "").parent
    return list(d.glob("*.json")) if d.exists() else []


def _archived_pending_path(subsystem: str, pending_id: str) -> Path:
    if not _valid_subsystem(subsystem) or not _valid_pending_id(pending_id):
        raise ValueError("invalid archived pending location")
    return get_hermes_home() / "archive" / "pending" / subsystem / f"{pending_id}.json"


def _memory_projection_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Revision fence for the target projection a proposal reviewed."""
    target = payload.get("target", "memory")
    filename = "USER.md" if target == "user" else "MEMORY.md"
    path = get_hermes_home() / "memories" / filename
    raw = path.read_bytes() if path.exists() else b""
    return {"target": target, "sha256": hashlib.sha256(raw).hexdigest()}


def _proposal_provenance() -> Dict[str, str]:
    home = get_hermes_home()
    profile = home.name if home.parent.name == "profiles" else "default"
    return {"source_host": socket.gethostname(), "source_profile": profile}


def stage_write(subsystem: str, payload: Dict[str, Any], *, summary: str, origin: str) -> Dict[str, Any]:
    """Persist a pending write and return its record (``id`` + metadata). ``payload`` is the exact
    kwargs to replay the write on approval; ``origin`` is ``foreground`` or ``background_review``.
    Persistence is fail-closed: callers never receive an ID for a record that was not durably written."""
    if not _valid_subsystem(subsystem):
        raise ValueError(f"invalid pending subsystem: {subsystem!r}")
    pid = uuid.uuid4().hex[:8]
    record: Dict[str, Any] = {
        "schema_version": 2,
        "id": pid, "subsystem": subsystem, "action": payload.get("action", ""),
        "summary": (summary or "").strip(), "origin": origin or "foreground",
        "created_at": time.time(), "payload": payload,
        "provenance": _proposal_provenance(),
    }
    if subsystem == MEMORY:
        record["base"] = _memory_projection_state(payload)
    try:
        atomic_json_write(_pending_path(subsystem, pid), record, fsync_dir=True)
    except Exception as e:  # pragma: no cover - disk failure path
        logger.error("Failed to stage pending %s write: %s", subsystem, e, exc_info=True)
        raise RuntimeError(f"Could not persist pending {subsystem} write: {e}") from e
    return record


def validate_pending_base(record: Dict[str, Any]) -> tuple[bool, str]:
    """Validate the revision fence shape before replaying a memory proposal.

    Schema-v2 records must carry a well-formed target/hash fence. True legacy
    records predate ``schema_version`` and keep their original replay behavior.
    The actual hash comparison happens inside MemoryStore's locked transaction.
    """
    if record.get("subsystem") != MEMORY:
        return True, ""
    version = record.get("schema_version")
    if version is None:
        return True, ""
    if type(version) is not int or version != 2:
        return False, f"unsupported pending memory schema version {version!r}; proposal was left pending"
    base, payload = record.get("base"), record.get("payload")
    target = payload.get("target", "memory") if isinstance(payload, dict) else None
    if (not isinstance(base, dict) or base.get("target") != target or target not in {"memory", "user"}
            or not isinstance(base.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", base["sha256"])):
        return False, "missing or malformed schema-v2 base revision; proposal was left pending for safety"
    return True, ""


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """Return all well-formed pending records for ``subsystem``, oldest first."""
    if not _valid_subsystem(subsystem):
        return []
    records: List[Dict[str, Any]] = []
    for p in _pending_files(subsystem):
        try:
            pending_id = p.stem
            if not _valid_pending_id(pending_id):
                raise ValueError("invalid filename id")
            record = json.loads(p.read_text(encoding="utf-8"))
            if not _record_matches_location(record, subsystem, pending_id):
                raise ValueError("record id/subsystem does not match its pending location")
            records.append(record)
        except Exception:
            logger.warning("Skipping unreadable or inconsistent pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Return a single location-consistent pending record by id, or None."""
    if not _valid_subsystem(subsystem) or not _valid_pending_id(pending_id):
        return None
    path = _pending_path(subsystem, pending_id)
    if not path.exists():
        return None
    with suppress(Exception):
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if _record_matches_location(data, subsystem, pending_id) else None
    return None


def discard_pending(subsystem: str, pending_id: str, *, outcome: str = "discarded") -> bool:
    """Archive and delete a location-consistent pending record. Returns True only after both succeed.

    Pending writes may contain useful memory proposals even when rejected; preserving the
    exact record before deletion lets the vault sync back them up for later recovery.
    """
    if not _valid_subsystem(subsystem) or not _valid_pending_id(pending_id):
        return False
    try:
        path = _pending_path(subsystem, pending_id)
        if not path.exists():
            return False
        try:
            record: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if not _record_matches_location(record, subsystem, pending_id):
                raise ValueError("pending record does not match its location")
        except Exception:
            return False
        record["resolution"] = outcome
        record["resolved_at"] = time.time()
        atomic_json_write(_archived_pending_path(subsystem, pending_id), record)
        path.unlink()
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to archive/discard pending %s/%s: %s", subsystem, pending_id, e)
    return False


def pending_count(subsystem: str) -> int:
    """Cheap count of pending records (for notification badges)."""
    if not _valid_subsystem(subsystem):
        return 0
    d = _pending_path(subsystem, "").parent
    if not d.exists():
        return 0
    with suppress(Exception):
        return sum(1 for _ in d.glob("*.json"))
    return 0


# --- Write origin ---

def current_origin() -> str:
    """``foreground`` or ``background_review`` — reuses the skill-provenance ContextVar
    the background review fork sets; foreground turns leave it at the default."""
    with suppress(Exception):
        from tools.skill_provenance import get_current_write_origin
        return get_current_write_origin()
    return "foreground"


# --- Gate decision ---

@dataclass(slots=True, kw_only=True)
class GateDecision:
    """Result of evaluating the write gate; exactly one flag is True. ``allow``: do the real write;
    ``blocked``: user denied the inline prompt (``message`` says why); ``stage``: caller must
    ``stage_write`` the payload (``message`` is the user-facing "staged for approval" note)."""

    allow: bool = False
    blocked: bool = False
    stage: bool = False
    message: str = ""


def _staged(subsystem: str) -> GateDecision:
    where = "/skills pending" if subsystem == SKILLS else "/memory pending"
    return GateDecision(stage=True, message=(f"Staged for approval ({subsystem}.write_approval is on). "
                                             f"Not yet saved — review with {where}."))


def evaluate_gate(subsystem: str, *, inline_summary: str = "", inline_detail: str = "") -> GateDecision:
    """Decide what to do with a pending write: gate off → allow; gate on + skills (any origin) or
    background → stage; gate on + memory + foreground → inline prompt when an interactive channel
    exists, else stage. The gate only ever delays a write, never silently refuses it; ``blocked``
    is produced only when the user actively denies the inline prompt."""
    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)
    # Skills are too big to review inline; a background write runs in a daemon thread with no user.
    if subsystem == SKILLS or current_origin() == "background_review":
        return _staged(subsystem)
    granted = _prompt_inline_memory_approval(inline_summary, inline_detail)
    if granted is None:
        return _staged(MEMORY)
    if granted:
        return GateDecision(allow=True)
    return GateDecision(blocked=True, message="Memory write denied by user. The change was not saved.")


def _prompt_inline_memory_approval(summary: str, detail: str) -> Optional[bool]:
    """Prompt inline for a memory write: True approved, False denied, None → stage. Uses the per-thread
    CLI approval callback (``tools.terminal_tool.set_approval_callback``) directly, not
    ``prompt_dangerous_approval``: that wrapper falls back to ``input()`` (deadlock-prone under
    prompt_toolkit; silent deny in gateway sessions) and turns callback errors into a deny, whereas
    here a missing channel or failed prompt must stage instead.

    See #15216.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
    except Exception:
        return None
    callback = _get_approval_callback()
    if callback is None:
        return None
    header = summary.strip() or "Save to memory?"
    try:
        from tools.approval_prompt import callback_accepts
        extra = {"title": "Save to memory?"} if callback_accepts(callback, "title") else {}
        choice = callback(detail.strip() or header, f"Save to memory: {header}", allow_permanent=False, **extra)
    except Exception as e:
        logger.error("Inline memory approval prompt failed: %s", e)
        return None
    # unknown outcome → stage rather than drop
    return {"once": True, "session": True, "deny": False}.get(choice)


# --- Skill-specific helpers (gist + diff for the review affordances) ---

_GIST_TEMPLATES = {"write_file": "write {file_path} in '{name}'", "remove_file": "remove {file_path} from '{name}'",
                   "delete": "delete skill '{name}'"}


def skill_gist(action: str, name: str, *, content: str = "", file_path: str = "",
               old_string: str = "", new_string: str = "") -> str:
    """One-line heuristic gist (no model call) for a pending skill write: create/edit use
    the frontmatter ``description:``; patch/write_file describe the size of the change."""
    if action in {"create", "edit"} and content:
        desc = _frontmatter_description(content)
        size = f"{len(content) // 1024 + 1} KB" if len(content) >= 1024 else f"{len(content)} chars"
        return f"{'create' if action == 'create' else 'rewrite'} '{name}'{f' — {desc}' if desc else ''} ({size})"
    if action == "patch":
        removed = old_string.count("\n") + 1 if old_string else 0
        added = new_string.count("\n") + 1 if new_string else 0
        return f"patch '{name}' {file_path or 'SKILL.md'} (+{added}/-{removed} lines)"
    return _GIST_TEMPLATES.get(action, "{action} '{name}'").format(action=action, name=name, file_path=file_path)


def _frontmatter_description(content: str) -> str:
    """Extract the ``description:`` value from SKILL.md YAML frontmatter (≤140 chars)."""
    m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    return m.group(1).strip().strip("'\"")[:140] if m else ""


def _find_skill_path(name: str) -> Optional[Path]:
    """Directory of an installed skill, or None if unknown / lookup unavailable."""
    try:
        from tools.skill_manager_tool import _find_skill
    except Exception:
        return None
    # Only the import is guarded (as on main); a lookup failure propagates.
    found = _find_skill(name)
    return found["path"] if found else None


def skill_pending_diff(record: Dict[str, Any]) -> str:
    """Full content (create) or unified diff vs. the on-disk skill (edit/patch/write_file),
    rendered by /skills diff <id> on surfaces that can show it."""
    payload = record.get("payload", {})
    action = payload.get("action", "")
    name = payload.get("name", "")
    if action == "create":
        return payload.get("content") or ""
    if action not in {"edit", "patch", "write_file"}:
        return {"remove_file": f"remove file: {payload.get('file_path')} from skill '{name}'",
                "delete": f"delete skill '{name}'"}.get(action, f"({action} on '{name}')")

    # patch/write_file target a file inside the skill; edit always targets SKILL.md.
    target_label, current = "SKILL.md", ""
    skill_dir = _find_skill_path(name)
    if skill_dir:
        if action != "edit":
            target_label = payload.get("file_path") or "SKILL.md"
        with suppress(Exception):
            p = skill_dir / target_label
            current = p.read_text(encoding="utf-8") if p.exists() else ""

    if action == "patch":
        old_s, new_s = payload.get("old_string") or "", payload.get("new_string") or ""
        new = current.replace(old_s, new_s) if current else f"(patch {old_s!r} → {new_s!r})"
    else:
        new = payload.get("content" if action == "edit" else "file_content") or ""
    diff = difflib.unified_diff(current.splitlines(keepends=True), new.splitlines(keepends=True),
                                fromfile=f"a/{target_label}", tofile=f"b/{target_label}")
    return "".join(diff) or "(no textual change)"


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def is_background() -> bool:
    return current_origin() == "background_review"
# ---- END PLUGIN-COMPAT ----
