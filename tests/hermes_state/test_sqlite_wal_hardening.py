"""WAL checkpoint unblocking and retention for state.db (plan Priority 3).

The live bug: ``state.db-wal`` was measured at 157 MB because a `hermes serve`
process held an unfinalized read connection, so ``wal_checkpoint`` could not
advance past frame 384 of 39,699. Two causes, two fixes:

* A pooled read connection can be returned to the pool with an UNEXHAUSTED
  cursor. In autocommit SQLite ends a read transaction when a statement is
  fully stepped -- a half-read one keeps the reader's WAL mark pinned forever,
  and no amount of checkpointing moves past it.
* ``journal_size_limit`` was 64 MiB, so even a checkpointed WAL kept the high
  water mark of the largest transaction ever run.

These exercise the REAL SessionDB against a real file in tmp_path.
"""

from __future__ import annotations

import sqlite3

import pytest

import hermes_state_wal as wal
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    if not handle._wal_active:
        pytest.skip("WAL unavailable on this filesystem; the checkpoint contract does not apply")
    yield handle
    handle.close()


def _pragma(conn: sqlite3.Connection, name: str):
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# PRAGMA hardening
# ---------------------------------------------------------------------------


def test_wal_retention_is_bounded_to_sixteen_mib(db):
    """Plan: journal_size_limit = 16 MB, so the WAL returns space after a big txn."""
    assert wal._WAL_SIZE_LIMIT_BYTES == 16 * 1024 * 1024
    assert _pragma(db._conn, "journal_size_limit") == wal._WAL_SIZE_LIMIT_BYTES


def test_autocheckpoint_is_set_explicitly(db):
    """Plan: wal_autocheckpoint = 1000. Explicit, not inherited: the value is a
    durability/latency decision and must survive a different SQLite build's default."""
    assert wal._WAL_AUTOCHECKPOINT_PAGES == 1000
    assert _pragma(db._conn, "wal_autocheckpoint") == 1000


def test_read_connections_carry_a_positive_busy_timeout(db):
    """Plan: busy_timeout = 5000 on the read path.

    Writers deliberately keep a SHORT timeout and retry with jitter instead (see
    SessionDB's write-contention tuning), so the contract asserted here is the read
    one plus "no connection waits forever".
    """
    from hermes_state import _READ_BUSY_TIMEOUT_S

    assert _READ_BUSY_TIMEOUT_S * 1000 == 5000
    with db._read_ctx() as conn:
        assert _pragma(conn, "busy_timeout") > 0
    assert _pragma(db._conn, "busy_timeout") > 0


def test_every_wal_activation_applies_the_companions(tmp_path):
    """A fresh connection to an EXISTING db gets the same retention settings."""
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()
    second = SessionDB(db_path=path)
    try:
        if not second._wal_active:
            pytest.skip("WAL unavailable on this filesystem")
        assert _pragma(second._conn, "journal_size_limit") == wal._WAL_SIZE_LIMIT_BYTES
        assert _pragma(second._conn, "wal_autocheckpoint") == 1000
    finally:
        second.close()


# ---------------------------------------------------------------------------
# Read-connection finalization (the 157 MB WAL)
# ---------------------------------------------------------------------------
#
# What actually pins a WAL here, established by probing SQLite rather than assumed:
# a TEMPORARY cursor (`conn.execute(...).fetchone()`, which is every hermes_state read)
# is finalized by refcounting and pins nothing. An EXPLICIT `BEGIN` left open does pin,
# indefinitely, and is detectable via `conn.in_transaction`. That is the shape in
# hermes_state_timeline._snapshot, which hands a connection inside a read transaction to
# a caller for the length of a request -- so an abandoned request leaves the pooled
# connection holding a read mark no checkpoint can advance past.


def _checkpoint_blocked(path) -> bool:
    """True when a TRUNCATE checkpoint cannot complete -- i.e. a reader pins the WAL."""
    probe = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    try:
        busy, _log, _ckpt = probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return bool(busy)
    finally:
        probe.close()


def _dirty_the_wal(db, prefix="s", count=40) -> None:
    """Commit frames AFTER any open read snapshot, so a pinned reader shows up."""
    for i in range(count):
        db.create_session(f"{prefix}{i}", source="cli")


def test_a_leaked_read_transaction_pins_the_wal(db, tmp_path):
    """Pins the mechanism, so the fix below is not asserting a tautology."""
    conn = db._checkout_read_conn()
    if conn is None:
        pytest.skip("read pool unavailable")
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT id FROM sessions").fetchall()
        _dirty_the_wal(db, "pin")
        assert conn.in_transaction is True
        assert _checkpoint_blocked(tmp_path / "state.db") is True
    finally:
        conn.rollback()
        db._close_read_conn(conn)


def test_returning_a_read_connection_finalizes_a_leaked_transaction(db, tmp_path):
    """Plan acceptance: a checkpoint reduces the WAL under active server operation.

    The reader leaves its transaction open -- the shape an abandoned request produces.
    """
    with db._read_ctx() as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT id FROM sessions").fetchall()
    _dirty_the_wal(db, "after")
    assert _checkpoint_blocked(tmp_path / "state.db") is False


def test_finalization_happens_even_when_the_reader_raises(db, tmp_path):
    with pytest.raises(RuntimeError):
        with db._read_ctx() as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT id FROM sessions").fetchall()
            raise RuntimeError("handler exploded mid-read")
    _dirty_the_wal(db, "raised")
    assert _checkpoint_blocked(tmp_path / "state.db") is False


def test_a_finalized_connection_is_still_usable_for_the_next_request(db):
    """Finalizing must return the connection to the pool healthy, not scrap it."""
    db.create_session("s_reuse", source="cli")
    with db._read_ctx() as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT id FROM sessions").fetchall()
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    assert any(row[0] == "s_reuse" for row in rows)


def test_an_ordinary_read_is_untouched_by_the_finalizer(db, tmp_path):
    """The common path must stay a no-op: nothing to roll back, nothing rolled back."""
    _dirty_the_wal(db, "plain")
    with db._read_ctx() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 40
        assert conn.in_transaction is False
    assert _checkpoint_blocked(tmp_path / "state.db") is False


def test_wal_stays_bounded_across_many_leaky_requests(db, tmp_path):
    """The 157 MB shape: a long-running server serving request after request."""
    wal_file = tmp_path / "state.db-wal"
    for i in range(150):
        db.create_session(f"req{i}", source="api_server")
        with db._read_ctx() as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT id FROM sessions").fetchall()
    assert _checkpoint_blocked(tmp_path / "state.db") is False
    assert not wal_file.exists() or wal_file.stat().st_size <= wal._WAL_SIZE_LIMIT_BYTES
