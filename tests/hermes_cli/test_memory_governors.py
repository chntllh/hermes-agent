"""Hardware memory governors for kanban dispatch (plan Priority 1).

The shipped formula was ``MemTotal / 512 MB`` clamped to [2, 8]. Two things were
wrong with it on a real host:

1. 512 MB never covered a worker. A worker is a full Hermes agent process -- model
   client, tool runtime, usually a terminal backend.
2. ``MemTotal`` cannot see memory the host has already promised away. The Marine
   devbox holds 7.2 GiB in 12 rootless Podman containers permanently, so the
   formula read 13.5 GiB, derived 8 workers, and fanned out into memory that was
   never free -- the shape of the Sep 13 systemd-oomd incident.

The replacement bounds headroom twice (reserve-adjusted total AND live
``MemAvailable``), budgets 2 GiB per worker, and caps per host.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db_dispatch as kbd

GIB = 1024 * 1024  # KiB per GiB

# The two audited hosts, as memory samples.
MARINE = {"mem_total_kib": 27 * GIB // 2, "mem_available_kib": 32 * GIB // 5}   # 13.5 / 6.4 GiB
STARMAN = {"mem_total_kib": 24 * GIB, "mem_available_kib": 66 * GIB // 10}      # 24 / 6.6 GiB


def test_marine_derives_exactly_two_workers():
    """Plan acceptance: 13.5 GiB total / 6.4 GiB available on Marine evaluates to 2."""
    assert kbd.derive_default_max_in_progress(MARINE, ceiling=kbd.DERIVED_MAX_IN_PROGRESS_CEILING) == 2


def test_marine_would_have_derived_eight_under_the_old_formula():
    """Pins the bug the reserve exists for: MemTotal alone still says 8 workers."""
    assert (MARINE["mem_total_kib"] // 1024) // 512 == 27
    assert min((MARINE["mem_total_kib"] // 1024) // 512, 8) == 8


def test_starman_caps_at_three():
    """Plan acceptance: the fanless M4 is held to 3 regardless of free memory."""
    assert kbd.derive_default_max_in_progress(STARMAN, ceiling=kbd.DARWIN_MAX_IN_PROGRESS_CEILING) == 3


def test_container_reserve_is_subtracted_from_total():
    """A host whose total is entirely inside the reservation gets the floor, not 8."""
    swamped = {"mem_total_kib": 7 * GIB, "mem_available_kib": 7 * GIB}
    assert kbd.derive_default_max_in_progress(swamped, ceiling=2) == 1
    # One worker's budget above the reserve buys exactly one worker.
    assert kbd.derive_default_max_in_progress(
        {"mem_total_kib": 9 * GIB, "mem_available_kib": 9 * GIB}, ceiling=2) == 1
    assert kbd.derive_default_max_in_progress(
        {"mem_total_kib": 11 * GIB, "mem_available_kib": 11 * GIB}, ceiling=2) == 2


def test_live_availability_bounds_the_reserve_adjusted_total():
    """Both bounds apply: a roomy host under live pressure still derives low."""
    pressured = {"mem_total_kib": 64 * GIB, "mem_available_kib": 2 * GIB}
    assert kbd.derive_default_max_in_progress(pressured, ceiling=8) == 1
    roomy = {"mem_total_kib": 64 * GIB, "mem_available_kib": 60 * GIB}
    assert kbd.derive_default_max_in_progress(roomy, ceiling=8) == 8


def test_small_vm_floors_at_one_worker():
    """The old floor of 2 put 4 GiB of workers on a 1 GiB VM. One is the honest floor."""
    assert kbd.derive_default_max_in_progress({"mem_total_kib": 1 * GIB}, ceiling=2) == 1
    assert kbd.derive_default_max_in_progress({"mem_total_kib": GIB // 4}, ceiling=2) == 1


def test_fails_open_without_memtotal():
    for sample in ({}, {"mem_total_kib": 0}, {"mem_total_kib": -5},
                   {"mem_total_kib": True}, {"mem_total_kib": "1048576"}):
        assert kbd.derive_default_max_in_progress(sample) is None


def test_bad_availability_is_ignored_rather_than_trusted():
    """A nonsense MemAvailable must not silently zero the cap."""
    for bad in (None, -1, True, "lots"):
        sample = {"mem_total_kib": 24 * GIB, "mem_available_kib": bad}
        assert kbd.derive_default_max_in_progress(sample, ceiling=8) == 8


def test_derived_cap_never_exceeds_the_host_ceiling():
    """Relationship, not a snapshot: whatever the host ceiling is, the cap respects it."""
    huge = {"mem_total_kib": 512 * GIB, "mem_available_kib": 512 * GIB}
    ceiling = kbd.host_max_in_progress_ceiling()
    assert 1 <= ceiling <= 3
    assert kbd.derive_default_max_in_progress(huge) == ceiling


@pytest.mark.platforms("macos")
def test_host_ceiling_is_three_on_darwin():
    assert kbd.host_max_in_progress_ceiling() == kbd.DARWIN_MAX_IN_PROGRESS_CEILING == 3


@pytest.mark.platforms("not macos")
def test_host_ceiling_is_two_off_darwin():
    assert kbd.host_max_in_progress_ceiling() == kbd.DERIVED_MAX_IN_PROGRESS_CEILING == 2


def test_resolve_max_in_progress_still_lets_the_operator_win(monkeypatch):
    """The documented rollback is `kanban.max_in_progress` in config.yaml."""
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: dict(MARINE))
    assert kbd.resolve_max_in_progress(6) == 6
    assert kbd.resolve_max_in_progress(None) == kbd.derive_default_max_in_progress(MARINE)
