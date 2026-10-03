"""Darwin memory sensing for the dispatch governors (plan Priority 1).

``sample_memory()`` was Linux-only: on macOS both ``/proc`` reads returned ``{}``,
so ``derive_default_max_in_progress`` saw no ``mem_total_kib``, failed open, and
every memory guard on the M4 host was silently inert — an uncapped board on a
fanless laptop. The probe below closes that hole.
"""

from __future__ import annotations

import pytest

from gateway import lifecycle_ledger as ledger

_MEMORY_KEYS = ("mem_total_kib", "mem_available_kib", "swap_used_kib")


def test_psutil_sample_reports_the_three_governor_inputs():
    """Host-independent: psutil answers on every platform the governors run on."""
    sample = ledger._psutil_memory_sample()
    assert set(_MEMORY_KEYS) <= set(sample)
    assert sample["mem_total_kib"] > 0
    assert 0 <= sample["mem_available_kib"] <= sample["mem_total_kib"]
    assert sample["swap_used_kib"] >= 0


def test_psutil_sample_degrades_to_empty_without_psutil(monkeypatch):
    """Fail-open contract: an unusable probe means "unknown", never a crash."""
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("psutil unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert ledger._psutil_memory_sample() == {}


@pytest.mark.platforms("macos")
def test_sample_memory_is_populated_on_darwin():
    """Plan acceptance: ``sample_memory()`` on Starman returns valid metrics."""
    sample = ledger.sample_memory()
    assert set(_MEMORY_KEYS) <= set(sample), f"macOS sampling still blind: {sample}"
    assert sample["mem_total_kib"] > 0
    # The whole point: the derived cap must now see a number here.
    from hermes_cli.kanban_db_dispatch import derive_default_max_in_progress

    assert derive_default_max_in_progress(sample) is not None


@pytest.mark.platforms("linux")
def test_sample_memory_keeps_the_cheap_proc_path_on_linux():
    """The /proc reader stays authoritative where it works (no psutil cost per heartbeat)."""
    sample = ledger.sample_memory()
    assert sample.get("mem_total_kib", 0) > 0
    assert "rss_kib" in sample


def test_fallback_never_overwrites_a_populated_proc_reading(monkeypatch):
    """Host-independent: the merge takes the platform fork as data."""
    monkeypatch.setattr(ledger, "_proc_fields", lambda path, wanted: (
        {"rss_kib": 111} if "status" in path
        else {"mem_total_kib": 222, "mem_available_kib": 33, "SwapTotal": 10, "SwapFree": 4}
    ))
    monkeypatch.setattr(ledger, "_psutil_memory_sample", lambda: {
        "rss_kib": 999, "mem_total_kib": 999, "mem_available_kib": 999, "swap_used_kib": 999,
    })
    assert ledger.sample_memory() == {
        "rss_kib": 111, "mem_total_kib": 222, "mem_available_kib": 33, "swap_used_kib": 6,
    }


def test_fallback_fills_in_when_proc_is_absent(monkeypatch):
    monkeypatch.setattr(ledger, "_proc_fields", lambda path, wanted: {})
    monkeypatch.setattr(ledger, "_psutil_memory_sample", lambda: {
        "mem_total_kib": 222, "mem_available_kib": 33, "swap_used_kib": 7,
    })
    assert ledger.sample_memory() == {
        "mem_total_kib": 222, "mem_available_kib": 33, "swap_used_kib": 7,
    }
