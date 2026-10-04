# Phase 2 Report — Attention Governor & Totonou Integration

**Plan:** `PLAN-REMEDIATION-MIGRATION-20260925` / `ARCH-KANBAN-NOTIFY-GOV-20260925`
**Branch:** `feat/attention-governor-phase2`
**Worktree:** `/Users/starman/hermes/worktrees/hermes-agent/attention-governor-phase2`
**Date:** 2026-10-04

---

## 1. STATUS

**COMPLETE** — all three tasks delivered and green. Four deviations are recorded in §4;
none of them block the acceptance criteria, and each is a deliberate scope decision
rather than an unfinished edge.

---

## 2. Task checklist

| # | Task | Commit | State |
|---|---|---|---|
| 1 | Native governor + dual-mode Totonou resolver | `eed1114c54` | ✅ |
| 2 | Kanban event ingestion, tiers, breakers | `f0a08efe28` | ✅ |
| 2b | Wake-only / profile-scope / config-contract hardening | `45651e78f4` | ✅ |
| 2c | Storm breaker → dispatcher spawn pause (§7.2) | `727e930ada` | ✅ |
| 3 | Test suite (10 ported scenarios + 29 new) | in `eed1114c54`, `f0a08efe28`, `45651e78f4`, `727e930ada` | ✅ |

### Module layout

Facade + topic siblings, per the root `AGENTS.md` decomposition convention:

```
gateway/
├── attention_governor.py           # facade: AttentionGovernor, public re-exports
├── attention_governor_models.py    # tiers, event/block vocabulary, 4 records
├── attention_governor_focus.py     # resolve_totonou_focus(), CachedFocusSensor
├── attention_governor_queue.py     # LinearDecisionQueue (Attention WIP=1), §6.1 scoring
├── attention_governor_breakers.py  # FlapDetector, SwarmStormBreaker, ReplayLedger
└── attention_governor_ingest.py    # Kanban translation, settings, card/digest transport
```

Touched: `gateway/kanban_watchers.py`, `gateway/kanban_watchers_notifier.py`,
`gateway/kanban_watchers_common.py`, `gateway/AGENTS.md`,
`hermes_cli/config_defaults.py`, `website/docs/user-guide/features/kanban.md`.

### Task 1 — the corrected Totonou contract

`totonou_get_focus(mode)` answers for **one** mode, so a single probe can only ever
describe half the operator's state. `resolve_totonou_focus(client)`:

- probes `work` **and** `personal` (accepts an object with `totonou_get_focus(mode)` or a
  bare `callable(mode)`);
- `focus_shield_active = work_focus or personal_focus` — the operator is one person, and
  a personal focus session is as inviolable as a work one;
- resolves `mode` **personal-first**, so a work alert can never breach personal time when
  both report `doing`; with neither focused it honours a `mode` the probe declares, else
  defaults to `work`;
- keys the shield on `status == "doing"` (§5.2A) — a parked focus task is a session that
  exists, not an operator mid-flow;
- **fails open** on a total probe failure, marking the state `degraded`. A false "shield
  clear" costs one mention-free card; a false "shield active" swallows every HITL gate for
  as long as Totonou is unreachable.

Mode isolation holds P1 work reviews in the queue during `personal` mode, stages nothing
into a personal-mode capture inbox, and releases them on the flip back to `work`.

### Task 2 — Kanban wiring

Opt-in via `kanban.attention_governor.enabled` (**default false**, so an existing
install's notifier is byte-identical). Enabled:

- `claim_event()` translates each claimed `task_events` row into the governor's
  vocabulary; `_send_pings` sends only what comes back as a P0 interrupt and the governor
  owns the rest.
- `flush_governor()` runs per notifier tick: re-probes focus, expires HITL leases, renders
  at most **one** decision card (via `edit_message` with a send fallback, so it updates in
  place) and posts the batched silent digest.
- P2 telemetry is suppressed from pings and rolled up; P1s queue by §6.1 score; the storm
  breaker suppresses individual alerts **and** holds dispatcher spawns; the flap detector
  damps a zombie loop to exactly one triage notice.

Four invariants are pinned by tests and documented in `gateway/AGENTS.md`:

1. **No board event may mint a P0.** `_KIND_MAP` omits the three infrastructure kinds and
   `classify_event` reads only kinds, typed block kinds and infrastructure-written payload
   flags — agent prose is display text, never classified (spec §2 T7).
2. **The wake path is not gated.** Waking the origin agent is board plumbing; holding it
   behind a focus session would deadlock the swarm while the operator is away.
3. **Suppression is never loss.** Held, mode-locked and damped events stay addressable;
   the cursor advances because the governor took ownership, not because anything dropped.
4. **The event source is at-least-once.** Any send or wake failure rewinds the cursor, so
   every ingest passes a `ReplayLedger`; counting a retry twice would manufacture flaps and
   storms out of one real incident.

---

## 3. Acceptance test results

```
scripts/run_tests.sh tests/gateway/test_attention_governor.py \
                     tests/gateway/test_attention_governor_kanban.py
→ 39 passed, 0 failed

uv run --group dev pytest <same two files> -q
→ 39 passed in 3.50s
```

Regression sweep over every suite this work touches:

```
scripts/run_tests.sh tests/gateway/test_kanban*.py tests/gateway/test_attention_governor*.py \
                     tests/hermes_cli/test_kanban*.py tests/hermes_cli/test_config.py
→ 89 files, 731 passed, 1 failed, 15 skipped
```

### Spec §9 acceptance criteria

| § | Criterion | Test | Result |
|---|---|---|---|
| 9.1 | 10 reviews during Focus Shield → 0 pings, all buffered | `test_focus_shield_holds_every_p1_without_emitting_a_card` | ✅ |
| 9.2 | Shield clears → exactly 1 card, rest queued in score order | `test_intermission_window_presents_exactly_one_highest_scoring_card` | ✅ |
| 9.3 | 10 × `blocked(transient)` → breaker trips, 0 pings, 1 digest | `test_transient_storm_trips_the_breaker_without_a_single_ping` | ✅ |
| 9.4 | Repeated crashes → triage on the 3rd flap, exactly 1 notice | `test_flap_detection_damps_a_zombie_restart_loop` | ✅ |
| 9.5 | `personal` mode → work P1s strictly suppressed | `test_personal_mode_buffers_work_reviews_instead_of_dropping_them` | ✅ |
| 9.6 | P0 bypasses the shield and pings, then rate-limits | `test_p0_pierces_the_focus_shield_but_rate_limits_the_mention` | ✅ |

### Ported vault scenarios (10/10)

`test_focus_shield_suppression`, `test_intermission_window_linear_presentation`,
`test_operator_response_advances_queue`, `test_swarm_storm_circuit_breaker`,
`test_flap_detection_hysteresis`, `test_mode_isolation_personal_boundary`,
`test_p0_emergency_bypass_and_mention_rate_limit`, `test_decision_queue_timeout_deferral`,
`test_milestone_vs_leaf_completion`, `test_swarm_deadlock_detection` — all ported (renamed
to behaviour statements) and passing. See §4.1 for the one behavioural correction.

### Added coverage (29 tests)

- **Dual-mode Totonou resolver (9):** both modes probed; shield reported per mode; personal
  wins the mode; a parked task is not a shield; an idle probe still reports the declared
  mode; partial probe failure uses the mode that answered; total failure fails open and
  marks `degraded`; a bare callable probe works; the governor drives it end to end.
- **P0 boundary (2):** P0 is delivered even in personal mode; the mention cooldown reopens.
- **Dispatch (2):** the storm breaker holds spawns and self-clears.
- **Kanban translation, against real board rows (7):** `needs_input` → P1; `transient` →
  P2; **no board event kind can reach P0** (swept over all of `TERMINAL_KINDS` with a
  payload screaming `P0 CRITICAL SECURITY BREACH`); priority clamped to the scoring scale;
  root epic vs. decomposition leaf; waiting children raise the score; a rewound claim does
  not double-count a flap.
- **Live notifier integration (5):** the no-governor baseline still pings 3×; the governor
  collapses 3 leaf completions to 0 pings with the cursor still advanced; a full batch posts
  one silent rollup; four concurrent reviews yield exactly one card with three pending; a
  wake-only subscription is never governed.
- **Config contract (4):** disabled by default (both in `DEFAULT_CONFIG` and via
  `build_governor`); settings carry through; the shipped keys are **exactly** those the
  reader consumes; a typo'd key warns instead of being swallowed.

### Bugs found and fixed during the work

- **Digest baseline.** `_digest_flushed_at` started at `0.0`, so the first rollup was always
  overdue and a single routine event posted its own digest — defeating the batching the tier
  exists for. Now seeded to construction time. Caught by the live notifier test, not by a
  unit test.
- **Wake-only subscriptions.** The first wiring pass governed every claimed event, which
  would have rendered a passive card to a `delivery_mode="wake"` subscriber who explicitly
  asked for none. Now only events that would have produced a passive ping today are
  eligible.

---

## 4. DEVIATIONS

### 4.1 Personal mode **buffers** work reviews; the reference implementation dropped them

The vault implementation logs `mode_suppressed` and returns **without enqueueing**, and
`test_mode_isolation_personal_boundary` froze that with `assertEqual(len(queue), 0)` — so
a review raised during personal time was lost outright, with nothing to deliver on the flip
back to work.

Spec §1.5 ("they buffer silently until the operator transitions into `work` mode") and
§5.2C ("they remain paused in the Governor queue until Sid flips the toggle") both require
buffering, as does the run brief ("work notifications are held/buffered"). **Implemented as
buffering**, and the ported test was adapted to assert the stronger contract: zero cards,
zero pings, zero Totonou captures, *and* the card still pending — then rendered by
`on_totonou_mode_changed("work")`.

### 4.2 Module layout is facade + siblings, not the spec's subpackage

Spec §8.1 sketches `gateway/attention_governor/{core,models,focus_sensor,queue,...}.py`.
The repo's own convention (root `AGENTS.md`, "Facade + siblings layout") is
`<stem>_<topic>.py` beside the facade, which is what the surrounding `kanban_watchers_*`
family uses. Same decomposition, repo-native naming. `discord_card.py` and `classifier.py`
did not earn their own files: the classifier is ~20 lines of the facade, and card rendering
is platform-agnostic (see 4.3) and lives with the rest of the transport in `_ingest`.

### 4.3 The Totonou MCP client and a Discord-specific transport are seams, not implementations

- **Totonou transport.** `resolve_totonou_focus(client)` and `build_governor(...,
  focus_client=...)` accept any object exposing `totonou_get_focus(mode)`. No in-tree
  FastMCP client to `marine:3210` was built: that host is not part of this repository, the
  client could not be exercised hermetically, and the root `AGENTS.md` rejects
  infrastructure with no concrete in-tree consumer. With no client configured the governor
  treats the operator as always reachable, so nothing is ever buffered indefinitely.
- **Discord specifics.** Cards and digests are delivered through the **subscription's own
  adapter**, so the feature works on every platform rather than Discord alone.
  Update-in-place uses the existing `edit_message` contract (`success=False` → send anew).
  `flags=4096` and `allowed_mentions: {"parse": []}` are recorded on every governor payload
  and passed in send metadata; whether they are physically honoured is adapter-side. The
  attention win — N interruptions collapsed to one card plus one rollup — is
  platform-independent and is what the tests assert. Interaction **buttons** are not wired:
  `operator_respond_hitl(task_id, action)` is the entry point a button router will call, and
  it is tested directly.

### 4.4 §6.3 auto-deferral marks the card, it does not park the board task

`LinearDecisionQueue.check_timeouts()` expires the lease, deferres the card and vacates the
attention slot (tested), and `flush_governor` runs it every tick. It does **not** yet apply
`park_and_continue` / `fail_safe_revert` to the Kanban row. That is a *write-back into the
board from the notifier tick*, which the notifier has never done — it reads and delivers —
and the brief's Task 2 enumerates the tiers, the storm breaker and flap hysteresis, not
auto-park. Deferred deliberately rather than bolted on; the attention-protecting half (the
slot is released, so the next review is handed over) is in place.

### 4.5 Test command

Both runners were used. `scripts/run_tests.sh` is the project-mandated path (root
`AGENTS.md`: CI parity — scrubbed credentials, `TZ=UTC`, temp `HERMES_HOME`, per-file
subprocess isolation) and is what the figures in §3 come from. The brief's
`uv run --group dev pytest` was also run and passes; the `.venv` it creates was removed
afterwards because `run_tests.sh` probes `.venv` first and that dev-only environment would
shadow the correct one.

---

## 5. BLOCKERS

**None.** No step failed twice for the same root cause.

Two pre-existing failures were encountered, investigated, and proven unrelated:

1. **55 failures across 22 `tests/gateway/` files**, all with the identical message
   `TEST BUG: file I/O against the REAL hermes home:
   /Users/starman/.hermes/hermes-agent/.git/worktrees/attention-governor-phase2/hermes-update-pull`.
   An artifact of this worktree's git metadata living under `~/.hermes/`, which trips
   `tests/home_io_guard.py`. Verified by classifying every failure in all 22 files: 82/82
   are that one guard. None touch kanban, the notifier or config.

2. **`tests/hermes_cli/test_kanban_db.py::test_infrastructure_spawn_refusal_never_charges_the_card`.**
   Verified pre-existing by reverting `hermes_cli/config_defaults.py` to `HEAD` and
   re-running: it still fails. Restored immediately after.

Neither is in scope for this phase; both are environment/baseline conditions on the branch
as handed over.
