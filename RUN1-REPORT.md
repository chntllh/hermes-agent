# RUN1 REPORT — Hermes Agent Reliability, Concurrency & Spend Hardening (Phase 1)

**Plan:** `PLAN-REMEDIATION-MIGRATION-20260925` §2, Priorities 0–4
**Branch:** `fix/remediation-and-hardening-phase1`
**Worktree:** `/Users/starman/hermes/worktrees/hermes-agent/remediation-and-hardening-phase1`
**Base commit:** `eb7e8620324`
**Host:** Starman — macOS Darwin 27.0.0, arm64, Python 3.14 via `uv`
**Date:** 2026-10-04

---

## 1. STATUS

**COMPLETE** — all five priorities implemented, tested and committed.

109 tests across the five new suites pass, 0 fail. No regression was introduced in any suite
measured against a reverted baseline. Pre-existing failures unrelated to this work are
documented in §5 with the evidence that they predate it.

---

## 2. TASK CHECKLIST

| # | Priority | Result | Commit |
|---|---|---|---|
| 1 | P0 — Session runaway cost & polling circuit breakers | Session-scoped spend/turn/wall-clock tripwires in the turn loop + a command-shape polling detector on the tool-result path; `HERMES_DISABLE_COST_BREAKER=1` rollback. **29 tests pass.** | `49e40dcd2e` |
| 2 | P1 — Hardware memory governors & concurrency caps | Concurrency sized from reserve-adjusted total AND live availability at 2 GiB/worker, ceiling 2 (3 on macOS); `sample_memory()` gains a psutil fallback so macOS is no longer blind. **16 tests pass** (1 Linux-only skipped). | `c61576870d` |
| 3 | P2 — Git worktree concurrency & branch safety | TOCTOU `worktree add` fallback, shared-`refs/stash` prohibition guard, flat `wt-<id>` branches with legacy teardown, repo-scoped gitlock sweep. **31 tests pass.** | `6201d3ffce` |
| 4 | P3 — SQLite WAL checkpoint unblocking & durability | Pooled read connections finalized on return, `journal_size_limit` 64→16 MiB, explicit `wal_autocheckpoint=1000`, cross-store retention contract. **12 tests pass.** | `702bd957cc` |
| 5 | P4 — Deterministic local completion verification gate | `verification_command` + `expected_artifacts` enforced in `complete_task` via `TaskVerificationFailedError`; declarable from CLI and model tool. **21 tests pass.** | `fc4a11cb82` |

Diffstat vs base: **31 files, +2174 / −85.**

---

## 3. ACCEPTANCE TABLE

Run command: `HERMES_PYTHON="$PWD/.venv/bin/python3" scripts/run_tests.sh <paths>`
(`scripts/run_tests.sh` is mandated by `AGENTS.md` over bare pytest — see DEVIATION D1.)

### Combined run of all five suites

```
[ 22.2% |    20/~90 | ✓18 | ✗ 0] ✓ tests/hermes_cli/test_memory_governors.py (11✓ 1s, 4.8s)
[ 43.3% |    39/~90 | ✓47 | ✗ 0] ✓ tests/agent/test_circuit_breakers.py (29✓, 5.9s)
[ 54.4% |    49/~90 | ✓57 | ✗ 0] ✓ tests/hermes_state/test_sqlite_wal_hardening.py (10✓, 8.7s)
[ 76.7% |    69/~90 | ✓88 | ✗ 0] ✓ tests/hermes_cli/test_worktree_concurrency.py (31✓, 10.9s)
[100.0% |    90/~90 | ✓109 | ✗  0] ✓ tests/hermes_cli/test_completion_verification.py (21✓, 13.4s)

=== Summary: 7 files, 109 tests passed, 0 failed, 2 skipped (100% complete) in 13.5s (10 workers) ===

  note: platforms('linux') tests (in 1 file) were SKIPPED on this host (darwin); they run on the main Linux CI lane.
  note: platforms('not macos') tests (in 1 file) were SKIPPED on this host (darwin); they run on the main Linux CI lane, the Windows Python-tests lane.
```

### Per plan acceptance criterion

| Plan criterion (§2) | Verdict | Evidence |
|---|---|---|
| **P0:** a synthetic loop of 10 `terminal("tmux capture-pane …")` calls is intercepted and paused at turn 4 | **PASS** | `test_detector_trips_on_the_fourth_poll_of_a_five_call_window` — `trips[0..2] is None`, `trips[3]` carries `STEERING_INTERRUPT`, all of `trips[3:]` stay flagged |
| **P0:** an artificial burn past $10.00 halts the session and prevents subsequent model API calls without user confirmation | **PASS** | `test_cost_tripwire_halts_before_the_next_api_call` + `test_begin_iteration_breaks_the_loop_once_the_cost_ceiling_is_crossed` — the loop returns `break` with `_turn_exit_reason="halted_budget_tripwire(session_cost)"` and `api_call_count` does **not** advance |
| **P0:** `HERMES_DISABLE_COST_BREAKER=1` reverts to unconstrained execution | **PASS** | `test_rollback_env_var_disables_every_breaker`, `test_detector_is_disabled_by_the_rollback_env_var`, `test_begin_iteration_is_unconstrained_under_the_rollback_switch` |
| **P1:** Marine (13.5 GiB total / 6.4 GiB free) strictly evaluates to `workers = 2` | **PASS** | `test_marine_derives_exactly_two_workers`; `test_marine_would_have_derived_eight_under_the_old_formula` pins the bug being fixed |
| **P1:** `sample_memory()` on Starman returns valid non-empty memory metrics | **PASS** | `test_sample_memory_is_populated_on_darwin` (`@pytest.mark.platforms("macos")`, executed natively on this host) — asserts all three keys present *and* that `derive_default_max_in_progress` is no longer `None` |
| **P1:** Starman capped at 3 | **PASS** | `test_starman_caps_at_three`, `test_host_ceiling_is_three_on_darwin` |
| **P2:** concurrent creation of two worktrees on the same branch succeeds without exit 255 | **PASS** | `test_worktree_add_falls_back_when_the_branch_appeared_mid_flight` (real git repo in `tmp_path`) |
| **P2:** `git stash` in an agent worktree triggers an immediate explanatory rejection | **PASS** | `test_stash_is_blocked_inside_a_linked_worktree` — message contains "repository-global" and names `git commit`; `test_stash_is_allowed_in_a_repo_with_no_linked_worktrees` pins the negative |
| **P3:** a PASSIVE checkpoint reduces the WAL under active server operations | **PASS** | `test_returning_a_read_connection_finalizes_a_leaked_transaction`; `test_a_leaked_read_transaction_pins_the_wal` first proves the unfixed mechanism, so the fix is not a tautology |
| **P3:** `state.db-wal` does not grow past 32 MB during intensive concurrent tool runs | **PASS** | `test_wal_stays_bounded_across_many_leaky_requests` (150 write+leaky-read cycles) asserts size ≤ `_WAL_SIZE_LIMIT_BYTES` = 16 MiB, half the stated bound |
| **P4:** a task with a failing test cannot be moved to `done` | **PASS** | `test_a_failing_verification_command_blocks_completion` |
| **P4:** a task with a missing output file cannot be moved to `done` | **PASS** | `test_a_missing_artifact_blocks_completion`, `test_an_empty_artifact_blocks_completion` |

### Regression baselines (reverted-file A/B on this host)

| Suite | Baseline failures | With my changes | New failures introduced |
|---|---|---|---|
| `tests/hermes_state/` (447 files) | 69 | 64 | **0** (the 5 deltas are my own tests flipping green) |
| `tests/agent/test_stall_guards.py` | 14 | 14 | **0** |
| `tests/hermes_cli/test_kanban_db.py` | 1 | 1 | **0** |

---

## 4. DEVIATIONS

**D1 — Test placement and runner.** The brief names `tests/test_circuit_breakers.py` etc.
`AGENTS.md` requires tests to mirror the source tree, reserving `tests/` root for root-level
modules only, and mandates `scripts/run_tests.sh` over bare `pytest` for CI parity (credential
scrub, `TZ=UTC`, temp `HERMES_HOME`, per-file subprocess isolation). I kept the brief's file
*names* and placed them per `AGENTS.md`: `tests/agent/test_circuit_breakers.py`,
`tests/{gateway,hermes_cli}/test_memory_governors.py`,
`tests/hermes_cli/test_worktree_concurrency.py`,
`tests/{hermes_state,hermes_cli}/test_sqlite_wal_hardening.py`,
`tests/hermes_cli/test_completion_verification.py`. The brief explicitly authorises pragmatic
path fixes. Both runners were used; `run_tests.sh` output is what this report quotes.

**D2 — P1: the plan's own arithmetic is internally inconsistent; I resolved it toward the
stated acceptance number.** The plan's snippet subtracts the 7 GiB reserve from
`mem_available_kib`. On Marine that is `6.4 − 7 < 0 → 0 workers → 1`, which contradicts its own
acceptance criterion of exactly 2. Subtracting from `mem_total_kib` gives
`(13.5−7)·1024/2048 = 3 → min(3,2) = 2`, which matches. The reserve *models* memory the host has
permanently promised to containers, so subtracting it from `MemAvailable` double-counts.
Implemented: `usable = min(total − RESERVE, available)` — both bounds, smaller wins. Marine → 2 ✓,
Starman → 3 ✓, and it is strictly safer than either single bound.

**D3 — P1: host ceiling is injected, not sniffed per host name.** The plan phrases caps as
"Marine: 2 / Starman: 3". Hard-coding hostnames is wrong for shipped code, so the ceiling is
keyed on OS family (`hermes_platform.host.facts.os_family()`): 2 generally, 3 on Darwin, where
there is no container tenancy but thermal throttling bites first. `derive_default_max_in_progress`
takes `ceiling=` so arithmetic is tested as data — `AGENTS.md` forbids faking the host OS in tests.

**D4 — P1: the derived floor moved 2 → 1, changing two shipped tests.**
`test_derived_cap_small_vm_floors_at_two` and `test_resolve_max_in_progress_derives_when_unset`
encoded the superseded policy (floor 2, which budgets 4 GiB of workers onto a 1 GiB VM). Updated
to the new, strictly safer policy and renamed.

**D5 — P0: limits live in `config.yaml`, not a new env var.** `AGENTS.md` forbids new `HERMES_*`
env vars for non-secret config. Limits read from `agent.circuit_breakers`.
`HERMES_DISABLE_COST_BREAKER=1` is implemented exactly as specified — it is a rollback
kill-switch, not configuration.

**D6 — P0: the polling detector keys on command SHAPE, not on "no output state change".** The
plan says "≥4 near-identical polling commands … with no output state change". Requiring the
output to be unchanged would reproduce the exact blind spot being fixed: `agent/tool_guardrails.py`
already halts on byte-identical `(tool, args, result)` streaks, and a `tmux capture-pane` whose
pane clock ticks is never byte-identical. The detector therefore folds drifting literals (counts,
pane ids, pids) into a shape key and trips on 4-of-5 matching shapes regardless of output. Real
interleaved work dilutes the window — pinned by
`test_detector_tolerates_polls_interleaved_with_real_work`.

**D7 — P0: a tripped breaker latches; resuming needs an explicit human act.** The plan says
"prompt user before continuing". A breaker that re-armed on the next user message would let the
same loop spend the ceiling again. The halt message names the three ways forward (raise the
ceiling in `config.yaml`, start a fresh session, or set the rollback var).

**D8 — P2: `wt-<id>` rename needed a legacy path the plan does not mention.** Teardown only
deletes branches Hermes generated. A card created before the rename has `branch_name` NULL in the
DB and `wt/<id>` on disk, so reconstructing only the new name would strand one branch per legacy
card. `is_auto_branch()` recognises both prefixes and the no-recorded-name path tries both.

**D9 — P2: `gitlock` scoping is by shared git dir, with an explicit conservative case.** The plan
says "check if the lockfile's owning PID is still alive". Git lock files carry no PID, so that is
not implementable as written. Implemented instead: enumerate live `git` processes (psutil, a core
dependency) and compare each one's cwd against this repo's `--git-common-dir`, so every linked
worktree of a repo protects its siblings (they share an object store). A process whose cwd cannot
be read is still treated as possibly ours — narrowing the probe must not become yanking a lock
from a live fetch we merely could not see. The old global `pgrep -x git` probe was removed; the
Windows console-hiding test that covered it is retargeted at the git probe that replaced it.

**D10 — P3: the plan's diagnosis of the 157 MB WAL was half right; I probed SQLite instead of
assuming.** A *temporary* cursor (`conn.execute(...).fetchone()` — every read in `hermes_state`)
is finalized by refcounting and pins nothing; measured directly. An *explicit* `BEGIN` handed to
a caller does pin, indefinitely, and `hermes_state_timeline._snapshot` is precisely that shape —
it yields a connection inside a read transaction for the length of a request. The fix therefore
finalizes any transaction left open when a connection returns to the pool. `test_a_leaked_read_
transaction_pins_the_wal` pins the mechanism so the fix is verifiable rather than assumed.

**D11 — P3: three sub-items were already satisfied or deliberately stricter; I did not loosen
them.**
- `busy_timeout = 5000`: state.db read connections already use exactly 5.0 s (`_READ_BUSY_TIMEOUT_S`).
  The **writer** deliberately uses 1 s plus jittered application-level retry (documented
  anti-convoy tuning); raising it to 5 s would fight that design. Left alone, with a contract test
  asserting every connection has a bounded positive timeout.
- **kanban.db** already sets `journal_size_limit=8 MiB`, `wal_autocheckpoint=100`,
  `busy_timeout=120 s` — all stricter than the plan. Relaxing them to the plan's numbers would be
  a regression. `tests/hermes_cli/test_sqlite_wal_hardening.py` pins the *invariant* (bounded
  retention, armed autocheckpoint, bounded wait) across both stores rather than the numbers.
- **Automated maintenance checkpoint:** already shipped (`SessionDB._try_wal_checkpoint`, PASSIVE,
  every N writes, plus `kanban_db_connect`'s per-interval board checkpoint). Nothing added.

**D12 — P4: the fields did not exist; the gate needed a surface to be reachable.** The plan
writes `task.verification_command` / `task.expected_artifacts` as if present. They were not.
Added as additive schema columns, `Task` fields, `create_task` parameters, CLI flags
(`--verify-with`, repeatable `--expect-artifact`) and `kanban_create` tool-schema properties. A
gate nobody can declare is not a gate, so reachability is tested end-to-end through the real
parser and the real schema.

**D13 — P4: the gate runs before the write transaction and is not exempt for review.** It spawns
a subprocess; holding SQLite's write lock across someone's build would block every other worker on
the board, so it runs outside the txn (mirroring `prepare_acceptance`). It runs before the prose
check so a red build is reported as a red build. Review approval is exempt from the *prose* check
but not from this one — a human vouching for a card is not evidence its suite passes. It also
fails **closed** (timeout, unlaunchable command, missing workspace all block) and rejects
artifact paths that resolve outside the workspace.

**D14 — Incidental: one command touched the real `~/.hermes`.** An early
`uv run python -c "import run_agent"` triggered Hermes's source-update bootstrap, which re-synced
bundled skills and refreshed the model-catalog cache under the real home. **No database was
touched** — `state.db` and `kanban.db` were not opened, and nothing was deleted (2 user-modified
skills were correctly preserved). Every later command set `HERMES_HOME` to a temp dir. Flagged for
completeness against the brief's boundary.

---

## 5. BLOCKERS

**B1 — `run_agent` cannot be imported under pytest on this host (pre-existing, environment).**
Any test importing `run_agent` fails at collection:

```
tests/home_io_guard.py:140: AssertionError
E  TEST BUG: file I/O against the REAL hermes home:
   /Users/starman/.hermes/hermes-agent/.git/worktrees/remediation-and-hardening-phase1/hermes-update-pull
```

Cause: this worktree's gitdir lives under the real `~/.hermes`, and `run_agent.py:23` runs
`_early_recovery.restore_interrupted_pull()` at import time. At
`hermes_cli/_early_recovery.py:455` the guard reads
`if not marker.is_file() or _pytest_owns_live_checkout(root):` — `marker.is_file()` is evaluated
**first** and trips the home-I/O guard before the pytest-ownership check can short-circuit it.

**Proven pre-existing:** `tests/agent/test_stall_guards.py` fails 14/14 identically with my
changes reverted (`git checkout -- run_agent.py agent/turn_iteration_prep.py`), and the same
signature accounts for the `tests/gateway/test_agent_cache*.py` and `test_api_server_*.py`
failures.

**Impact:** Priority 0's two wiring seams are covered by tests that deliberately avoid importing
`run_agent` (a `_LoopAgent` stub exercising the real `begin_iteration`), so P0 is verified. What
cannot be verified on this host is the *full-stack* path through `AIAgent`. Not fixed: it is a
one-line reordering in a file outside all five priorities, and narrowing or widening this run's
scope is the operator's call.

**B2 — Pre-existing macOS failures in `tests/hermes_state/`.** 64 failures remain, all present on
the reverted baseline (69 there; the 5-test delta is my own suite going green). The dominant
signature is `assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1` → `2`, i.e. the
intentional Darwin `synchronous=FULL` floor colliding with tests written for NORMAL. Unrelated to
this work.

Nothing in the five priorities was left incomplete.

---

## 6. REMAINING HUMAN STEPS

1. **Review and merge.** Five commits on `fix/remediation-and-hardening-phase1`. Nothing has been
   pushed (per the brief).

2. **Activate the spend breakers (opt-in tuning).** Defaults ($10 / 120 turns / 2 h) are live as
   soon as the branch lands. To change them, add to `~/.hermes/config.yaml`:
   ```yaml
   agent:
     circuit_breakers:
       max_session_cost_usd: 10.0   # 0 disables this one tripwire
       max_turns: 120
       max_continuous_hours: 2.0
   ```
   Emergency rollback: `HERMES_DISABLE_COST_BREAKER=1`.

3. **Set Marine's concurrency explicitly before the Phase 2 pilot.** The derived cap now lands on
   2, but the plan's rollback is an explicit value — set `kanban.max_in_progress: 2` on Marine and
   `0` on Starman in `config.yaml` so the pilot does not depend on a derivation.

4. **Verify Marine's numbers on Marine.** The Marine arithmetic is tested against its recorded
   memory profile as data; the live Linux `/proc` path and the `platforms("linux")` test in
   `tests/gateway/test_memory_governors.py` were skipped on this macOS host. Run
   `scripts/run_tests.sh tests/gateway/test_memory_governors.py tests/hermes_cli/test_memory_governors.py`
   on Marine, and sanity-check `python -c "from hermes_cli.kanban_db_dispatch import *; print(derive_default_max_in_progress())"`.

5. **Decide on B1.** If you want full-stack `run_agent` tests runnable here, the fix is reordering
   the two operands at `hermes_cli/_early_recovery.py:455` so `_pytest_owns_live_checkout(root)` is
   evaluated before `marker.is_file()`. One line; out of scope for this run.

6. **Adopt the P4 gate on real cards.** It is opt-in and inert until declared. For the Phase 2
   pilot, give each of the 5–8 pilot cards a machine-executable check:
   ```
   hermes kanban create "bring up SPI driver" \
     --workspace worktree:/path/to/repo \
     --verify-with "pytest tests/ -q" \
     --expect-artifact build/firmware.bin
   ```
   This is the plan's "machine-executable verification commands" roadmap item, and it is what
   makes the Phase 3 go-criterion "false-positive completion rate strictly 0%" measurable rather
   than observed.

7. **Phase 2 entry.** With P0–P4 landed, the plan's Phase 1 is complete and the staged pilot
   (bounded Kanban pilot on one repo, worker cap 2/0, Attention Governor prototype) is unblocked.
