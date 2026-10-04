"""Kanban → Attention Governor wiring: settings, event translation and card transport.

The board speaks ``task_events`` rows; the governor speaks tiers. This sibling owns the
translation in both directions and nothing else, so neither side has to know the other.

The translation is the anti-self-escalation boundary (§2 T7). Every field it sets comes
from a column or a typed payload key the *infrastructure* writes — status, block kind,
priority, graph topology. Agent-authored prose rides along in ``summary``/``reason`` as
display text and is never read by :meth:`AttentionGovernor.classify_event`, so no board
event can mint a P0.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from gateway.attention_governor import (
    AttentionGovernor,
    BlockKind,
    CachedFocusSensor,
    DEFAULT_DIGEST_BATCH_SIZE,
    DEFAULT_DIGEST_INTERVAL_SECONDS,
    DEFAULT_P0_COOLDOWN_SECONDS,
    EventKind,
    KanbanTaskEvent,
    NotificationTier,
)
from gateway.attention_governor_focus import DEFAULT_FOCUS_TTL_SECONDS
from gateway.attention_governor_queue import DEFAULT_LEASE_SECONDS

logger = logging.getLogger("hermes.attention_governor")

SETTINGS_KEY = "attention_governor"

#: §6.1 weights ``w_p = 40`` per priority point. Kanban's ``priority`` is an unbounded
#: ordering tiebreaker (``ORDER BY priority DESC``), so a card created with 9999 would
#: swamp every other term. Clamp into the scale the weights were chosen for.
MAX_SCORED_PRIORITY = 4

#: Child statuses that mean "this card is still waiting on its parent" — the §6.1
#: downstream-blocked term. ``running``/``review``/``done``/``archived`` are not waiting.
_WAITING_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "blocked"})

#: Board event kinds the governor understands, mapped onto its vocabulary. Deliberately
#: missing: ``critical_panic`` / ``security_breach`` / ``swarm_deadlock``. Those are P0
#: and no board event may produce them.
_KIND_MAP = {
    "completed": EventKind.COMPLETED,
    "review_requested": EventKind.REVIEW_REQUESTED,
    "changes_requested": EventKind.CHANGES_REQUESTED,
    "blocked": EventKind.BLOCKED,
    "block_loop_detected": EventKind.BLOCK_LOOP_DETECTED,
    "gave_up": EventKind.GAVE_UP,
    "crashed": EventKind.CRASHED,
    "timed_out": EventKind.TIMED_OUT,
    "status": EventKind.STATUS,
    "archived": EventKind.ARCHIVED,
    "unblocked": EventKind.UNBLOCKED,
}


@dataclass(frozen=True)
class GovernorSettings:
    """Resolved ``kanban.attention_governor.*`` settings."""

    enabled: bool = False
    operator_id: str = ""
    focus_poll_seconds: float = DEFAULT_FOCUS_TTL_SECONDS
    hitl_lease_seconds: float = DEFAULT_LEASE_SECONDS
    p0_cooldown_seconds: float = DEFAULT_P0_COOLDOWN_SECONDS
    digest_batch_size: int = DEFAULT_DIGEST_BATCH_SIZE
    digest_interval_seconds: float = DEFAULT_DIGEST_INTERVAL_SECONDS


#: Every key :func:`governor_settings` consumes. ``DEFAULT_CONFIG`` must declare exactly
#: these: a key only one side knows about is a setting that silently does nothing, which
#: is how ``gateway.auto_migrate`` shipped against a guard reading another spelling.
SETTING_KEYS = (
    "enabled",
    "operator_id",
    "focus_poll_seconds",
    "hitl_lease_seconds",
    "p0_cooldown_seconds",
    "digest_batch_size",
    "digest_interval_seconds",
)


def _number(cfg: Mapping, key: str, default: float) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        logger.warning("attention governor: invalid kanban.%s.%s=%r; using %s",
                       SETTINGS_KEY, key, cfg.get(key), default)
        return default
    return value if value > 0 else default


def governor_settings(kanban_cfg: Optional[Mapping]) -> GovernorSettings:
    """Parse the ``kanban.attention_governor`` block; absent or invalid means disabled."""
    section = (kanban_cfg or {}).get(SETTINGS_KEY)
    if not isinstance(section, Mapping):
        return GovernorSettings()
    unknown = sorted(set(section) - set(SETTING_KEYS))
    if unknown:
        # A typo'd setting that silently does nothing is worse than a loud one.
        logger.warning("attention governor: ignoring unknown kanban.%s key(s): %s",
                       SETTINGS_KEY, ", ".join(unknown))
    operator = section.get("operator_id")
    return GovernorSettings(
        enabled=bool(section.get("enabled", False)),
        operator_id="" if operator in (None, "") else str(operator),
        focus_poll_seconds=_number(section, "focus_poll_seconds", DEFAULT_FOCUS_TTL_SECONDS),
        hitl_lease_seconds=_number(section, "hitl_lease_seconds", DEFAULT_LEASE_SECONDS),
        p0_cooldown_seconds=_number(section, "p0_cooldown_seconds", DEFAULT_P0_COOLDOWN_SECONDS),
        digest_batch_size=int(_number(section, "digest_batch_size", DEFAULT_DIGEST_BATCH_SIZE)),
        digest_interval_seconds=_number(
            section, "digest_interval_seconds", DEFAULT_DIGEST_INTERVAL_SECONDS),
    )


def build_governor(
    kanban_cfg: Optional[Mapping], *, focus_client: Any = None,
) -> Optional[AttentionGovernor]:
    """Build the governor for this gateway, or ``None`` when it is not enabled.

    *focus_client* is any object exposing ``totonou_get_focus(mode)`` (Totonou's MCP
    tool handle, or a stub). Without one the governor assumes an unmonitored operator:
    the shield reads clear, so nothing is ever buffered indefinitely.
    """
    settings = governor_settings(kanban_cfg)
    if not settings.enabled:
        return None
    governor = AttentionGovernor(
        discord_user_id=settings.operator_id,
        p0_cooldown_seconds=settings.p0_cooldown_seconds,
        digest_batch_size=settings.digest_batch_size,
        digest_interval_seconds=settings.digest_interval_seconds,
    )
    governor.decision_queue.ttl_seconds = settings.hitl_lease_seconds
    if focus_client is not None:
        governor.totonou_client = CachedFocusSensor(
            focus_client,
            ttl_seconds=settings.focus_poll_seconds,
            on_focus_cleared=lambda _state: governor.on_totonou_focus_completed(),
            on_mode_changed=governor.on_totonou_mode_changed,
        )
    logger.info("attention governor: enabled (operator=%s, lease=%.0fs)",
                settings.operator_id or "<unset>", settings.hitl_lease_seconds)
    return governor


# --- translation ---------------------------------------------------------------------


def _block_kind(payload: Mapping, task: Any) -> Optional[BlockKind]:
    """The typed block classification: the event payload wins, the row is the fallback.

    An untyped block (the failure breaker parks cards with ``block_kind IS NULL``)
    stays ``None`` and classifies as P2 — the dispatcher is still retrying it, and
    ``gave_up`` is the event that escalates once the retry budget is spent.
    """
    raw = payload.get("kind") or getattr(task, "block_kind", None)
    if raw in (None, ""):
        return None
    try:
        return BlockKind(str(raw))
    except ValueError:
        return None


def _downstream_waiters(graph: Optional[Mapping]) -> int:
    children = (graph or {}).get("children") or ()
    return sum(1 for child in children if str(child.get("status")) in _WAITING_STATUSES)


def _is_milestone_root(graph: Optional[Mapping]) -> bool:
    """A card with no parent is a user-facing deliverable, not a decomposition leaf.

    With no graph loaded we answer False: a leaf completion misread as a milestone
    would ping the operator for a routine subtask, which is the failure this exists
    to prevent.
    """
    return graph is not None and not (graph.get("parents") or ())


def governor_event_from_kanban(
    ev: Any, *, task: Any, board_slug: str,
    graph: Optional[Mapping] = None, delivery: Optional[dict] = None,
) -> Optional[KanbanTaskEvent]:
    """Translate one ``kanban_db.Event`` into the governor's vocabulary.

    ``None`` for a kind the governor has no opinion on, which leaves the caller's
    existing delivery path in charge.
    """
    kind = _KIND_MAP.get(str(ev.kind))
    if kind is None:
        return None
    payload = dict(ev.payload or {})
    if kind is EventKind.COMPLETED:
        payload["is_milestone_root"] = _is_milestone_root(graph)
    status = str(getattr(task, "status", "") or payload.get("status") or "")
    priority = max(0, min(MAX_SCORED_PRIORITY, int(getattr(task, "priority", 0) or 0)))
    return KanbanTaskEvent(
        task_id=str(ev.task_id),
        board_slug=board_slug,
        kind=kind,
        title=str(getattr(task, "title", None) or ev.task_id)[:120],
        status=status,
        priority=priority,
        block_kind=_block_kind(payload, task) if kind in (
            EventKind.BLOCKED, EventKind.BLOCK_LOOP_DETECTED) else None,
        payload=payload,
        timestamp=float(getattr(ev, "created_at", 0) or 0) or time.time(),
        downstream_blocked_count=_downstream_waiters(graph),
        # A card pinning a worktree while it waits is costlier to leave queued (§6.1 B).
        holds_resources=bool(getattr(task, "workspace_path", None)),
        delivery=delivery,
    )


@dataclass(frozen=True)
class ClaimResult:
    """What the governor did with one offered event.

    ``owned`` is the only thing a caller needs to branch on: True means the governor
    has taken responsibility for reaching the operator and the caller must not also
    ping. ``tier`` is ``None`` on a replay — already counted, still owned.
    """

    owned: bool
    tier: Optional[NotificationTier] = None
    replayed: bool = False


DECLINED = ClaimResult(owned=False)


def claim_event(
    governor: AttentionGovernor, ev: Any, *, task: Any, board_slug: str,
    graph: Optional[Mapping] = None, delivery: Optional[dict] = None,
) -> ClaimResult:
    """Offer one claimed board event to *governor*."""
    event = governor_event_from_kanban(
        ev, task=task, board_slug=board_slug, graph=graph, delivery=delivery)
    if event is None:
        return DECLINED
    tier = governor.ingest_once(event, key=(board_slug, getattr(ev, "id", None), ev.kind))
    return ClaimResult(owned=True, tier=tier, replayed=tier is None)


# --- transport -----------------------------------------------------------------------


def delivery_route(sub: Mapping, board_slug: Optional[str]) -> dict:
    """Everything needed to reach this subscription again on a later tick.

    A held card outlives the claim that produced it — it may sit behind a 40-minute
    focus session — so the route is captured at ingest rather than re-derived.
    """
    metadata = sub.get("delivery_metadata")
    return {
        "platform": str(sub.get("platform") or "").lower(),
        "chat_id": sub.get("chat_id"),
        "thread_id": sub.get("thread_id") or "",
        "profile": sub.get("notifier_profile") or None,
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        "board": board_slug,
        "sub": dict(sub),
    }


def _route_key(route: Mapping) -> str:
    return f"{route.get('platform')}:{route.get('chat_id')}:{route.get('thread_id') or ''}"


def _mobile_safe(value: Any, limit: int = 160) -> str:
    """§2 T5: redact credentials and local paths, collapse to one mobile-sized line."""
    from gateway.kanban_watchers_notifier import _safe_review_reason

    return _safe_review_reason(value, limit)


def format_card(payload: Mapping) -> str:
    """The update-in-place decision card body (§5.1A), sized for a phone."""
    depth = int(payload.get("queue_depth") or 1)
    queue_tag = f" [HITL {depth} queued]" if depth > 1 else ""
    lines = [f"📋 Review{queue_tag} · {payload.get('task_id')} — {_mobile_safe(payload.get('title'), 80)}"]
    summary = _mobile_safe(payload.get("summary"))
    if summary:
        lines.append(summary)
    lines.append(f"Actions: {' / '.join(payload.get('actions') or ['approve', 'reject', 'defer'])}")
    return "\n".join(lines)


def format_digest(entries: "list[Mapping]") -> str:
    """One silent rollup replacing N individual pings (§3.3)."""
    counts: "dict[str, int]" = {}
    for entry in entries:
        label = str(entry.get("type") or entry.get("kind") or "event")
        counts[label] = counts.get(label, 0) + 1
    breakdown = ", ".join(f"{count}× {label}" for label, count in sorted(counts.items()))
    return f"🔕 Swarm digest — {len(entries)} event(s): {breakdown}"


async def _send_to_route(runner: Any, route: Mapping, text: str, platform_cls: Any,
                         *, message_id: Optional[str] = None, silent: bool = False) -> Optional[str]:
    """Deliver *text* to a captured route; the sent/edited message id, or ``None``.

    Prefers ``edit_message`` so the single decision card is updated in place rather
    than re-posted; adapters without edit support answer ``success=False`` and we
    send anew, which is the documented contract.
    """
    from gateway.kanban_watchers_notifier import _adapter_for_subscription, owner_scope

    try:
        platform = platform_cls(route.get("platform"))
    except ValueError:
        logger.debug("attention governor: unknown platform %r; dropping render", route.get("platform"))
        return None
    sub = route.get("sub") or {}
    # The route recheck reads the served profile's session store for a stateless
    # destination, so it runs off the event loop like the notifier's own recheck.
    adapter = await asyncio.to_thread(
        _adapter_for_subscription, runner, platform, sub, route.get("profile"))
    if adapter is None:
        logger.debug("attention governor: no adapter for %s; render deferred", _route_key(route))
        return None

    metadata = dict(route.get("metadata") or {})
    if route.get("thread_id") and not metadata.get("thread_id"):
        metadata["thread_id"] = route["thread_id"]
    if silent:
        # Discord SUPPRESS_NOTIFICATIONS; adapters that do not model it ignore the key
        # and the batching alone still removes N-1 interruptions.
        metadata["suppress_notifications"] = True
        metadata["flags"] = 4096

    # Rendering reads the SUBSCRIBER profile's config (language, media policy), while
    # this loop runs in the launch profile's scope.
    async with owner_scope(runner, platform, route.get("chat_id"), route.get("profile")):
        if message_id:
            edited = await adapter.edit_message(route["chat_id"], message_id, text)
            if getattr(edited, "success", False):
                return getattr(edited, "message_id", None) or message_id
        result = await adapter.send(route["chat_id"], text, metadata=metadata)
    if getattr(result, "success", True) is False:
        logger.warning("attention governor: send to %s failed: %s",
                       _route_key(route), getattr(result, "error", None))
        return None
    return getattr(result, "message_id", None)


async def flush_governor(runner: Any, platform_cls: Any) -> None:
    """Per-tick governor transport: re-probe focus, render the card, roll up the digest.

    Runs after the notifier's own deliveries so an event claimed this tick can reach
    the operator on the same tick when its gate is already open.
    """
    governor = getattr(runner, "_kanban_governor", None)
    if governor is None:
        return
    try:
        governor.refresh_focus()
    except Exception as exc:
        logger.debug("attention governor: focus refresh failed: %s", exc)

    for payload in governor.drain_rendered_cards():
        route = payload.get("delivery")
        if not route:
            logger.debug("attention governor: card for %s has no route; it stays queued",
                         payload.get("task_id"))
            continue
        key = _route_key(route)
        try:
            new_id = await _send_to_route(
                runner, route, format_card(payload), platform_cls,
                message_id=governor.card_message_ids.get(key))
        except Exception as exc:
            logger.warning("attention governor: card render for %s failed: %s",
                           payload.get("task_id"), exc)
            continue
        if new_id:
            governor.card_message_ids[key] = new_id

    if not governor.digest_due():
        return
    batched: "dict[str, list]" = {}
    routes: "dict[str, Mapping]" = {}
    for entry in governor.drain_digest():
        route = entry.get("delivery")
        if not route:
            continue
        key = _route_key(route)
        batched.setdefault(key, []).append(entry)
        routes.setdefault(key, route)
    for key, entries in batched.items():
        try:
            await _send_to_route(runner, routes[key], format_digest(entries), platform_cls, silent=True)
        except Exception as exc:
            logger.warning("attention governor: digest render for %s failed: %s", key, exc)
