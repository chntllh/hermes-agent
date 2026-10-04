"""The linearized HITL decision queue (Attention WIP=1).

One slot, many contenders. §6.1 scores every pending item on each evaluation so a
low-priority card cannot starve behind a stream of urgent ones.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from gateway.attention_governor_models import DecisionCard, KanbanTaskEvent

logger = logging.getLogger("hermes.attention_governor")

#: §6.3 HITL lease: an unacknowledged head-of-queue card parks itself rather than
#: holding a worker slot until the operator wakes up.
DEFAULT_LEASE_SECONDS = 45 * 60

# §6.1 Score(T) = w_p·P + w_d·D + w_t·Δt + w_b·B
WEIGHT_PRIORITY = 40.0
WEIGHT_DOWNSTREAM = 25.0
WEIGHT_WAIT_PER_SECOND = 1.0 / 60.0  # one point per minute waited
WEIGHT_RESOURCE_HOLD = 10.0


class LinearDecisionQueue:
    """At most one card is ``active``; everything else waits in score order."""

    def __init__(self, ttl_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self.queue: Dict[str, DecisionCard] = {}
        self.active_card_task_id: Optional[str] = None

    def calculate_score(self, event: KanbanTaskEvent, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        waited = max(0.0, now - event.timestamp)
        return (
            WEIGHT_PRIORITY * event.priority
            + WEIGHT_DOWNSTREAM * event.downstream_blocked_count
            + WEIGHT_WAIT_PER_SECOND * waited
            + (WEIGHT_RESOURCE_HOLD if event.holds_resources else 0.0)
        )

    def enqueue(self, card: DecisionCard) -> DecisionCard:
        """Add *card*, or refresh an unresolved one in place.

        Refreshing matters for §7.1: a damped task re-emits ``block_loop_detected`` on
        every subsequent flap, and replacing the card would reset its lease and re-render
        it. The operator sees one notice per incident, not one per bounce.
        """
        existing = self.queue.get(card.task_id)
        if existing is not None and existing.status in ("pending", "active"):
            existing.title = card.title
            existing.summary = card.summary
            existing.priority = card.priority
            existing.score = card.score
            existing.delivery = card.delivery or existing.delivery
            return existing
        self.queue[card.task_id] = card
        return card

    def pending(self) -> List[DecisionCard]:
        """Pending cards, best first. Ties break on enqueue order so FIFO survives."""
        return sorted(
            (c for c in self.queue.values() if c.status == "pending"),
            key=lambda c: (-c.score, c.enqueued_at),
        )

    def get_highest_priority(self) -> Optional[DecisionCard]:
        pending = self.pending()
        return pending[0] if pending else None

    def acquire_active_slot(self) -> Optional[DecisionCard]:
        """Return the card occupying the single attention slot, promoting one if free."""
        if self.active_card_task_id:
            active = self.queue.get(self.active_card_task_id)
            if active is not None and active.status == "active":
                return active
            self.active_card_task_id = None

        candidate = self.get_highest_priority()
        if candidate is None:
            return None
        candidate.status = "active"
        self.active_card_task_id = candidate.task_id
        return candidate

    def release_active_slot(self) -> None:
        """Vacate the slot without resolving anything (the shield went back up)."""
        active = self.queue.get(self.active_card_task_id or "")
        if active is not None and active.status == "active":
            active.status = "pending"
        self.active_card_task_id = None

    def resolve(self, task_id: str, action: str) -> Optional[DecisionCard]:
        """Mark *task_id* handled and promote the next contender."""
        card = self.queue.get(task_id)
        if card is not None:
            card.status = "resolved"
            card.actions = [action]
        if self.active_card_task_id == task_id:
            self.active_card_task_id = None
        return self.acquire_active_slot()

    # Historical name used by the reference implementation.
    resolve_active = resolve

    def check_timeouts(self, now: Optional[float] = None) -> List[DecisionCard]:
        """Defer every card past its lease so the swarm is never starved by an absent operator."""
        now = time.time() if now is None else now
        expired = []
        for card in list(self.queue.values()):
            if card.status not in ("pending", "active"):
                continue
            if (now - card.enqueued_at) <= self.ttl_seconds:
                continue
            card.status = "deferred"
            if self.active_card_task_id == card.task_id:
                self.active_card_task_id = None
            expired.append(card)
            logger.info("attention governor: HITL lease expired for %s; auto-deferring", card.task_id)
        return expired
