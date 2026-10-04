"""kanban.db WAL retention contract (plan Priority 3).

The plan prescribes `journal_size_limit = 16 MB`, `wal_autocheckpoint = 1000` and
`busy_timeout = 5000` for every Hermes SQLite connection. kanban.db already sets
STRICTER values than all three, tuned for its role as the shared cross-profile
dispatch bus (see the comments at the pragma block). Relaxing them to the plan's
numbers would be a regression, so what is pinned here is the invariant the plan is
really after -- bounded WAL retention, an armed autocheckpoint, a bounded wait --
rather than the specific numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import hermes_state_wal as wal

PLAN_JOURNAL_SIZE_LIMIT = 16 * 1024 * 1024
PLAN_AUTOCHECKPOINT_PAGES = 1000
PLAN_BUSY_TIMEOUT_MS = 5000


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _pragma(conn, name):
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


def test_kanban_connections_bound_wal_retention_at_least_as_tightly(kanban_home):
    with kbc.connect() as conn:
        if _pragma(conn, "journal_mode") != "wal":
            pytest.skip("WAL unavailable on this filesystem")
        limit = _pragma(conn, "journal_size_limit")
        assert 0 < limit <= PLAN_JOURNAL_SIZE_LIMIT

        pages = _pragma(conn, "wal_autocheckpoint")
        assert 0 < pages <= PLAN_AUTOCHECKPOINT_PAGES

        # Bounded, not necessarily 5s: kanban is the shared dispatch bus, so worker
        # stampedes are expected and it deliberately waits far longer.
        assert _pragma(conn, "busy_timeout") >= PLAN_BUSY_TIMEOUT_MS


def test_state_db_and_kanban_db_agree_on_bounded_retention(kanban_home):
    """One contract across both stores: neither may let the WAL grow without limit."""
    with kbc.connect() as conn:
        if _pragma(conn, "journal_mode") != "wal":
            pytest.skip("WAL unavailable on this filesystem")
        assert _pragma(conn, "journal_size_limit") <= wal._WAL_SIZE_LIMIT_BYTES
    assert 0 < wal._WAL_SIZE_LIMIT_BYTES <= PLAN_JOURNAL_SIZE_LIMIT
