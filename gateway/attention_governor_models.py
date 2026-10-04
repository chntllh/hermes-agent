"""Value types shared by the Attention Governor siblings.

Tier, event and block vocabularies plus the four records that travel between the
classifier, the queue and the transports. Kept dependency-free so the breakers and
the focus sensor can import it without dragging the gateway in.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WORK_MODE = "work"
PERSONAL_MODE = "personal"
FOCUS_MODES = (WORK_MODE, PERSONAL_MODE)


class NotificationTier(enum.Enum):
    """The three segregated attention budgets of ``ARCH-KANBAN-NOTIFY-GOV-20260925`` §3."""

    P0_INTERRUPT = "P0"      # Immediate; breaks the Focus Shield; mention rate-limited
    P1_REVIEW_QUEUE = "P1"   # Serialized HITL gate; held behind the shield; Attention WIP=1
    P2_SILENT_DIGEST = "P2"  # Zero-audio telemetry; batched rollups


class BlockKind(enum.Enum):
    NEEDS_INPUT = "needs_input"
    DEPENDENCY = "dependency"
    CAPABILITY = "capability"
    TRANSIENT = "transient"


class EventKind(enum.Enum):
    COMPLETED = "completed"
    REVIEW_REQUESTED = "review_requested"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    BLOCK_LOOP_DETECTED = "block_loop_detected"
    GAVE_UP = "gave_up"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    STATUS = "status"
    ARCHIVED = "archived"
    UNBLOCKED = "unblocked"
    # Infrastructure invariants. §2 T7: these are never derived from agent-authored
    # text, so a confused or compromised subagent cannot escalate itself to P0.
    CRITICAL_PANIC = "critical_panic"
    SECURITY_BREACH = "security_breach"
    SWARM_DEADLOCK = "swarm_deadlock"


#: Kinds a Kanban board may legitimately produce. The P0 trio is deliberately absent:
#: only infrastructure probes construct those, never the board event stream.
AGENT_REPORTABLE_KINDS = frozenset(
    k for k in EventKind
    if k not in (EventKind.CRITICAL_PANIC, EventKind.SECURITY_BREACH, EventKind.SWARM_DEADLOCK)
)


@dataclass
class TotonouFocusState:
    """One resolved answer to "may this alert reach the operator right now?".

    ``mode`` is the operator's *active* mode, not the mode of any one probe:
    ``resolve_totonou_focus`` folds the per-mode ``totonou_get_focus`` answers into it.
    """

    mode: str = WORK_MODE
    focus_shield_active: bool = False
    active_task_id: Optional[str] = None
    active_task_title: Optional[str] = None
    elapsed_seconds: float = 0.0
    #: True when no mode could be probed at all, so this state is a fail-open guess.
    degraded: bool = False


@dataclass
class KanbanTaskEvent:
    """A board transition presented to the governor for classification."""

    task_id: str
    board_slug: str
    kind: EventKind
    title: str
    status: str
    priority: int = 2                     # 0 (lowest) .. 4 (critical), the Kanban scale
    block_kind: Optional[BlockKind] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    downstream_blocked_count: int = 0
    holds_resources: bool = False         # Pinning a worktree / GPU lock while it waits
    #: Where a held card is rendered once its gate opens; set by the notifier ingest.
    delivery: Optional[Dict[str, Any]] = None


@dataclass
class DecisionCard:
    """One HITL item competing for the single attention slot."""

    task_id: str
    tier: NotificationTier
    title: str
    summary: str
    priority: int
    score: float
    enqueued_at: float
    status: str = "pending"               # pending | active | deferred | resolved
    discord_message_id: Optional[str] = None
    actions: List[str] = field(default_factory=lambda: ["approve", "reject", "defer"])
    target_mode: str = WORK_MODE
    board_slug: Optional[str] = None
    delivery: Optional[Dict[str, Any]] = None
