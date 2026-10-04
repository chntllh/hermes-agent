"""Notification & Attention Governor — the membrane between Kanban events and the operator.

Implements ``ARCH-KANBAN-NOTIFY-GOV-20260925``: three segregated tiers (P0 interrupt,
P1 serialized review queue, P2 silent digest), Totonou Focus Shield gating, the
single-slot HITL queue, anti-flap hysteresis and the cluster storm breaker.

Topic siblings: ``_models`` (vocabulary), ``_focus`` (Totonou dual-mode probe),
``_queue`` (Attention WIP=1), ``_breakers`` (dampers), ``_ingest`` (Kanban wiring).

Two properties are load-bearing and easy to erode:

* **Agents cannot escalate themselves.** :meth:`AttentionGovernor.classify_event` reads
  only the event *kind*, the typed block kind and infrastructure-set payload flags —
  never agent-authored prose. The three P0 kinds are unreachable from the board event
  stream (``AGENT_REPORTABLE_KINDS``).
* **Suppression is never loss.** Every held, locked-out or damped event stays
  addressable: P1s sit in the decision queue until their gate opens, P2s accumulate in
  the digest. Nothing is dropped on the floor.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from gateway.attention_governor_breakers import FlapDetector, ReplayLedger, SwarmStormBreaker
from gateway.attention_governor_focus import (
    CachedFocusSensor,
    DEFAULT_FOCUS_TTL_SECONDS,
    resolve_totonou_focus,
)
from gateway.attention_governor_models import (
    AGENT_REPORTABLE_KINDS,
    BlockKind,
    DecisionCard,
    EventKind,
    FOCUS_MODES,
    KanbanTaskEvent,
    NotificationTier,
    PERSONAL_MODE,
    TotonouFocusState,
    WORK_MODE,
)
from gateway.attention_governor_queue import LinearDecisionQueue

__all__ = [
    "AGENT_REPORTABLE_KINDS",
    "AttentionGovernor",
    "BlockKind",
    "CachedFocusSensor",
    "DecisionCard",
    "EventKind",
    "FOCUS_MODES",
    "FlapDetector",
    "KanbanTaskEvent",
    "LinearDecisionQueue",
    "ReplayLedger",
    "NotificationTier",
    "PERSONAL_MODE",
    "SwarmStormBreaker",
    "TotonouFocusState",
    "WORK_MODE",
    "resolve_totonou_focus",
]

logger = logging.getLogger("hermes.attention_governor")

#: §3.1 at most one audible emergency mention per quarter hour; the rest coalesce.
DEFAULT_P0_COOLDOWN_SECONDS = 15 * 60
#: §3.3 the silent digest flushes on whichever comes first.
DEFAULT_DIGEST_BATCH_SIZE = 25
DEFAULT_DIGEST_INTERVAL_SECONDS = 2 * 60 * 60

_P0_KINDS = (EventKind.CRITICAL_PANIC, EventKind.SECURITY_BREACH, EventKind.SWARM_DEADLOCK)
#: Failure transitions the flap detector counts (§7.1).
_FLAPPABLE_KINDS = (EventKind.CRASHED, EventKind.TIMED_OUT, EventKind.BLOCKED)
#: Block kinds that genuinely need a person rather than a retry.
_P1_BLOCK_KINDS = (BlockKind.NEEDS_INPUT, BlockKind.CAPABILITY)


class AttentionGovernor:
    """Stateful classifier and gate for one operator's attention budget."""

    def __init__(
        self,
        discord_user_id: str = "",
        totonou_client: Optional[Callable[[], TotonouFocusState]] = None,
        *,
        p0_cooldown_seconds: float = DEFAULT_P0_COOLDOWN_SECONDS,
        digest_batch_size: int = DEFAULT_DIGEST_BATCH_SIZE,
        digest_interval_seconds: float = DEFAULT_DIGEST_INTERVAL_SECONDS,
    ) -> None:
        self.discord_user_id = discord_user_id
        #: Zero-arg focus source. Production passes a :class:`CachedFocusSensor` over the
        #: Totonou MCP client; the default assumes an unmonitored operator (shield clear).
        self.totonou_client = totonou_client or (lambda: TotonouFocusState())
        self.flap_detector = FlapDetector()
        self.storm_breaker = SwarmStormBreaker()
        self.decision_queue = LinearDecisionQueue()

        self.last_p0_timestamp = 0.0
        self.p0_cooldown_seconds = p0_cooldown_seconds
        self.digest_batch_size = digest_batch_size
        self.digest_interval_seconds = digest_interval_seconds
        # Seeded to construction time, not 0: the interval is measured from the last
        # flush, and an epoch-zero baseline makes the very first digest overdue, so a
        # single routine event would post its own rollup — the batching this exists for.
        self._digest_flushed_at = time.time()

        # Transport-decoupled outboxes: the governor decides, a transport drains.
        self.outbound_discord_pings: List[Dict[str, Any]] = []
        self.outbound_discord_cards: List[Dict[str, Any]] = []
        self.outbound_discord_digests: List[Dict[str, Any]] = []
        self.outbound_totonou_captures: List[Dict[str, Any]] = []
        self.circuit_tripped_tasks: Set[str] = set()
        self.displayed_card_task_id: Optional[str] = None
        #: At-least-once sources replay; see :class:`ReplayLedger`.
        self.event_ledger = ReplayLedger()
        #: §5.1A the durable "single card" message id per destination, so the next
        #: contender edits the card in place instead of posting a second one.
        self.card_message_ids: Dict[str, str] = {}

    # --- classification -------------------------------------------------------------

    def classify_event(self, event: KanbanTaskEvent) -> NotificationTier:
        """Map an event to its tier from invariant conditions alone.

        Deliberately blind to free text: §2 T7's self-escalating agent writes
        ``P0 CRITICAL`` into a summary and still lands in the silent digest.
        """
        if event.kind in _P0_KINDS:
            return NotificationTier.P0_INTERRUPT

        if event.kind == EventKind.REVIEW_REQUESTED:
            return NotificationTier.P1_REVIEW_QUEUE
        if event.kind == EventKind.BLOCKED and event.block_kind in _P1_BLOCK_KINDS:
            return NotificationTier.P1_REVIEW_QUEUE
        if event.kind in (EventKind.BLOCK_LOOP_DETECTED, EventKind.GAVE_UP):
            return NotificationTier.P1_REVIEW_QUEUE
        if event.kind == EventKind.COMPLETED and event.payload.get("is_milestone_root"):
            return NotificationTier.P1_REVIEW_QUEUE

        # Transient/dependency blocks, in-budget crashes and timeouts, lane moves,
        # reviewer-driven rework and leaf completions are all healthy swarm mechanics.
        return NotificationTier.P2_SILENT_DIGEST

    # --- ingestion ------------------------------------------------------------------

    def ingest_event(self, event: KanbanTaskEvent, now: Optional[float] = None) -> NotificationTier:
        """Classify, damp and route one board event; returns the tier it resolved to."""
        now = time.time() if now is None else now

        self._apply_flap_hysteresis(event, now)
        if self._absorbed_by_storm(event, now):
            return NotificationTier.P2_SILENT_DIGEST

        tier = self.classify_event(event)
        state = self._focus_state()

        if tier is NotificationTier.P0_INTERRUPT:
            # §3.1 pierces the shield and the mode boundary alike: a host freeze or a
            # credential breach is not a work notification, it is the house on fire.
            self._handle_p0(event, now)
        elif self._mode_locked_out(event, state):
            self._handle_mode_lockout(event, tier, now)
        elif tier is NotificationTier.P1_REVIEW_QUEUE:
            self._handle_p1(event, state, now)
        else:
            self._handle_p2(event, now)
        return tier

    def ingest_once(
        self, event: KanbanTaskEvent, *, key: Any, now: Optional[float] = None,
    ) -> Optional[NotificationTier]:
        """:meth:`ingest_event` guarded by the replay ledger; ``None`` when *key* repeats.

        An at-least-once event source (the notifier rewinds its cursor on any send or
        wake failure) must not inflate the flap and storm counters on retry.
        """
        if not self.event_ledger.add_if_new(key):
            logger.debug("attention governor: ignoring replayed event %s", key)
            return None
        return self.ingest_event(event, now=now)

    def _apply_flap_hysteresis(self, event: KanbanTaskEvent, now: float) -> None:
        """§7.1: rewrite a flapping task's event into one triage notice, in place."""
        if event.kind not in _FLAPPABLE_KINDS:
            return
        if not self.flap_detector.record_and_check_flap(event.task_id, now=now):
            return
        logger.warning("attention governor: task %s is flapping; damping to triage", event.task_id)
        self.circuit_tripped_tasks.add(event.task_id)
        event.kind = EventKind.BLOCK_LOOP_DETECTED
        event.status = "triage"

    def _absorbed_by_storm(self, event: KanbanTaskEvent, now: float) -> bool:
        """§7.2: True when a cluster-wide upstream outage owns this transient failure."""
        if event.kind != EventKind.BLOCKED or event.block_kind is not BlockKind.TRANSIENT:
            return False
        if not self.storm_breaker.record_transient(event.task_id, now=now):
            return False
        self.outbound_discord_digests.append({
            "type": "storm_breaker_active",
            "tier": NotificationTier.P2_SILENT_DIGEST.value,
            "task_id": event.task_id,
            "board": event.board_slug,
            "timestamp": now,
            "flags": 4096,
            "delivery": event.delivery,
            "message": f"Cluster backoff active: transient failure on {event.task_id} suppressed",
        })
        return True

    # --- focus / mode ---------------------------------------------------------------

    def _focus_state(self) -> TotonouFocusState:
        """Current operator state; a sensor failure fails open (see ``resolve_totonou_focus``)."""
        try:
            state = self.totonou_client()
        except Exception as exc:
            logger.debug("attention governor: focus sensor failed (%s); assuming shield clear", exc)
            return TotonouFocusState(degraded=True)
        return state if isinstance(state, TotonouFocusState) else TotonouFocusState()

    @staticmethod
    def _event_mode(event: KanbanTaskEvent) -> str:
        mode = event.payload.get("mode", WORK_MODE)
        return mode if mode in FOCUS_MODES else WORK_MODE

    def _mode_locked_out(self, event: KanbanTaskEvent, state: TotonouFocusState) -> bool:
        """§1.5: personal time is a hard boundary for work-scoped notifications."""
        return state.mode == PERSONAL_MODE and self._event_mode(event) == WORK_MODE

    def _attention_open(self, state: TotonouFocusState) -> bool:
        """True when a P1 card may occupy the operator's surface right now."""
        return not state.focus_shield_active and state.mode != PERSONAL_MODE

    # --- tier handlers --------------------------------------------------------------

    def _handle_p0(self, event: KanbanTaskEvent, now: float) -> None:
        """Always visible; the *mention* (the audible part) is what the cooldown limits."""
        should_mention = (now - self.last_p0_timestamp) >= self.p0_cooldown_seconds
        if should_mention:
            self.last_p0_timestamp = now
        self.outbound_discord_pings.append({
            "tier": NotificationTier.P0_INTERRUPT.value,
            "task_id": event.task_id,
            "board": event.board_slug,
            "mention": f"<@{self.discord_user_id}>" if should_mention and self.discord_user_id else None,
            "title": f"🚨 EMERGENCY: {event.title}",
            "reason": event.payload.get("error") or event.payload.get("reason") or "Critical invariant trip",
            "timestamp": now,
        })

    def _handle_mode_lockout(self, event: KanbanTaskEvent, tier: NotificationTier, now: float) -> None:
        """Hold a work event through personal time without losing it.

        A P1 is still enqueued — §5.2C says it stays *paused in the queue* until the
        operator flips back to work, not that it evaporates — but nothing reaches a
        surface and no work item is staged into a personal-mode Totonou inbox.
        """
        if tier is NotificationTier.P1_REVIEW_QUEUE:
            self.decision_queue.enqueue(self._build_card(event, now))
        self.outbound_discord_digests.append({
            "type": "mode_suppressed",
            "tier": tier.value,
            "task_id": event.task_id,
            "board": event.board_slug,
            "timestamp": now,
            "flags": 4096,
            "delivery": event.delivery,
            "message": f"Work task {event.task_id} held while the operator is in personal mode",
        })

    def _build_card(self, event: KanbanTaskEvent, now: float) -> DecisionCard:
        return DecisionCard(
            task_id=event.task_id,
            tier=NotificationTier.P1_REVIEW_QUEUE,
            title=event.title,
            summary=event.payload.get("summary") or event.payload.get("reason") or "Action required",
            priority=event.priority,
            score=self.decision_queue.calculate_score(event, now=now),
            enqueued_at=now,
            target_mode=self._event_mode(event),
            board_slug=event.board_slug,
            delivery=event.delivery,
        )

    def _handle_p1(self, event: KanbanTaskEvent, state: TotonouFocusState, now: float) -> None:
        """Enqueue the review; render it only if the single attention slot is reachable."""
        card = self.decision_queue.enqueue(self._build_card(event, now))
        self.outbound_totonou_captures.append({
            "mode": card.target_mode,
            "raw_text": f"HITL Review: [{event.task_id}] {event.title}",
            "task_id": event.task_id,
            "timestamp": now,
        })
        if not self._attention_open(state):
            logger.info(
                "attention governor: focus shield up on %r; holding P1 card for %s",
                state.active_task_title, event.task_id,
            )
            return
        self._refresh_active_decision_card(state)

    def _handle_p2(self, event: KanbanTaskEvent, now: float) -> None:
        """Zero audio, zero push: ``flags=4096`` is Discord's SUPPRESS_NOTIFICATIONS."""
        self.outbound_discord_digests.append({
            "tier": NotificationTier.P2_SILENT_DIGEST.value,
            "task_id": event.task_id,
            "board": event.board_slug,
            "kind": event.kind.value,
            "status": event.status,
            "flags": 4096,
            "delivery": event.delivery,
            "timestamp": now,
        })

    # --- surface refresh ------------------------------------------------------------

    def _refresh_active_decision_card(
        self, state: Optional[TotonouFocusState] = None,
    ) -> Optional[DecisionCard]:
        """Promote the best pending card into the one slot and render it if it changed."""
        state = self._focus_state() if state is None else state
        if not self._attention_open(state):
            return None
        card = self.decision_queue.acquire_active_slot()
        if card is None or card.task_id == self.displayed_card_task_id:
            return card
        self.displayed_card_task_id = card.task_id
        self.outbound_discord_cards.append(self.render_card(card))
        return card

    def render_card(self, card: DecisionCard) -> Dict[str, Any]:
        """The update-in-place payload for the single Discord decision card (§5.1A)."""
        return {
            "action": "update_or_post_card",
            "task_id": card.task_id,
            "board": card.board_slug,
            "title": card.title,
            "summary": card.summary,
            "score": card.score,
            "queue_depth": len(self.decision_queue.pending()) + 1,
            "actions": card.actions,
            "delivery": card.delivery,
            # §5.1B: a review card never pings. Only a verified P0 may mention.
            "allowed_mentions": {"parse": []},
        }

    # --- operator / sensor edges ----------------------------------------------------

    def on_totonou_focus_completed(self, now: Optional[float] = None) -> Optional[DecisionCard]:
        """§5.2B intermission: the shield came down, so hand over the next review."""
        logger.info("attention governor: focus shield cleared; refreshing the decision card")
        return self._refresh_active_decision_card()

    def on_totonou_mode_changed(self, mode: str) -> Optional[DecisionCard]:
        """The operator flipped work/personal; release anything the boundary was holding."""
        logger.info("attention governor: operator mode is now %s", mode)
        return self._refresh_active_decision_card()

    def operator_respond_hitl(
        self, task_id: str, action: str, now: Optional[float] = None,
    ) -> Optional[DecisionCard]:
        """Apply an operator button press and advance the queue in place."""
        self.displayed_card_task_id = None
        self.flap_detector.reset(task_id)
        self.circuit_tripped_tasks.discard(task_id)
        next_card = self.decision_queue.resolve(task_id, action)
        if next_card is not None:
            self.displayed_card_task_id = next_card.task_id
            self.outbound_discord_cards.append(self.render_card(next_card))
        return next_card

    # --- dispatcher / digest consumers ----------------------------------------------

    def refresh_focus(self, now: Optional[float] = None) -> TotonouFocusState:
        """Per-tick housekeeping: re-probe focus, expire leases, reopen the slot.

        Called by the transport loop rather than by ingestion: the gate that holds a
        card open is the *operator's* state changing, which no board event announces.
        """
        sensor_refresh = getattr(self.totonou_client, "refresh", None)
        state = sensor_refresh() if callable(sensor_refresh) else self._focus_state()
        self.decision_queue.check_timeouts(now=now)
        self._refresh_active_decision_card(state)
        return state

    def drain_rendered_cards(self) -> List[Dict[str, Any]]:
        """Hand every card rendered since the last drain to the transport."""
        cards, self.outbound_discord_cards = self.outbound_discord_cards, []
        return cards

    def dispatch_allowed(self, now: Optional[float] = None) -> bool:
        """§7.2: False while the storm breaker holds new worker spawns back."""
        return not self.storm_breaker.is_tripped(now=now)

    def digest_due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if not self.outbound_discord_digests:
            return False
        return (
            len(self.outbound_discord_digests) >= self.digest_batch_size
            or (now - self._digest_flushed_at) >= self.digest_interval_seconds
        )

    def drain_digest(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Hand the accumulated silent telemetry to the digest transport and reset."""
        self._digest_flushed_at = time.time() if now is None else now
        entries, self.outbound_discord_digests = self.outbound_discord_digests, []
        return entries
