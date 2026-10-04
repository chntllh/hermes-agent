"""Totonou focus sensing: the dual-mode probe behind the Focus Shield gate.

Totonou's MCP surface exposes ``totonou_get_focus(mode)`` — it answers for ONE mode
and has no all-modes form, so a single call can only ever describe half the operator's
state. :func:`resolve_totonou_focus` asks for both and folds the answers into one
:class:`TotonouFocusState`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Optional

from gateway.attention_governor_models import (
    FOCUS_MODES,
    PERSONAL_MODE,
    WORK_MODE,
    TotonouFocusState,
)

logger = logging.getLogger("hermes.attention_governor")

#: Spec §5.2B polls focus every 30s; a cached sensor keeps per-event classification
#: off the FastMCP wire without letting the answer go stale across an intermission.
DEFAULT_FOCUS_TTL_SECONDS = 30.0


def _focus_probe(client: Any) -> Callable[[str], Any]:
    """The mode-scoped probe of *client*: its ``totonou_get_focus``, else the callable itself."""
    probe = getattr(client, "totonou_get_focus", None)
    if callable(probe):
        return probe
    if callable(client):
        return client
    raise TypeError("totonou client exposes neither totonou_get_focus(mode) nor __call__(mode)")


def _is_doing(payload: Any) -> bool:
    """§5.2A: the shield is up only while a task is actively ``doing``.

    A parked or done focus task is a session that exists, not an operator mid-flow.
    A response that omits ``status`` is read as ``doing`` — older Totonou builds
    report only ``active``, and under-reporting the shield is the costly direction.
    """
    if not isinstance(payload, Mapping):
        return False
    return bool(payload.get("active")) and str(payload.get("status") or "doing").lower() == "doing"


def _declared_mode(payloads: "dict[str, Any]") -> Optional[str]:
    """The operator's self-declared mode when neither probe is focused."""
    for mode in FOCUS_MODES:
        payload = payloads.get(mode)
        declared = payload.get("mode") if isinstance(payload, Mapping) else None
        if declared in FOCUS_MODES:
            return declared
    return None


def resolve_totonou_focus(client: Any, *, default_mode: str = WORK_MODE) -> TotonouFocusState:
    """Probe every Totonou mode and resolve the operator's single effective focus state.

    ``focus_shield_active`` is the OR of the modes: the operator is one person, and a
    personal focus session is as inviolable as a work one. ``mode`` resolves
    personal-first so a work alert can never breach personal time when both report
    ``doing``.

    A probe that raises is treated as "that mode is not focused". When *every* mode
    fails the result is marked ``degraded`` and fails OPEN (shield clear, work mode):
    a false "shield clear" costs one mention-free card, while a false "shield active"
    swallows every HITL gate for as long as Totonou stays unreachable.
    """
    probe = _focus_probe(client)
    payloads: "dict[str, Any]" = {}
    failures = 0
    for mode in FOCUS_MODES:
        try:
            payloads[mode] = probe(mode)
        except Exception as exc:
            failures += 1
            payloads[mode] = None
            logger.debug("totonou focus probe failed for mode %s: %s", mode, exc)

    focused = {mode: _is_doing(payloads.get(mode)) for mode in FOCUS_MODES}
    if focused[PERSONAL_MODE]:
        active_mode = PERSONAL_MODE
    elif focused[WORK_MODE]:
        active_mode = WORK_MODE
    else:
        active_mode = _declared_mode(payloads) or default_mode

    active = payloads.get(active_mode) if focused.get(active_mode) else None
    active = active if isinstance(active, Mapping) else {}
    return TotonouFocusState(
        mode=active_mode,
        focus_shield_active=any(focused.values()),
        active_task_id=_str_or_none(active.get("id") or active.get("task_id")),
        active_task_title=_str_or_none(active.get("task") or active.get("title")),
        elapsed_seconds=_float_or_zero(active.get("elapsed_seconds")),
        degraded=failures == len(FOCUS_MODES),
    )


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class CachedFocusSensor:
    """A zero-arg focus source that reuses one resolution for ``ttl`` seconds.

    Also detects the §5.2B *intermission*: the ``doing → not doing`` edge that means
    the operator just finished a focus task and can be handed the next review.
    """

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: float = DEFAULT_FOCUS_TTL_SECONDS,
        on_focus_cleared: Optional[Callable[[TotonouFocusState], None]] = None,
        on_mode_changed: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.on_focus_cleared = on_focus_cleared
        self.on_mode_changed = on_mode_changed
        self._state: Optional[TotonouFocusState] = None
        self._fetched_at = 0.0

    def __call__(self, *, now: Optional[float] = None) -> TotonouFocusState:
        now = time.monotonic() if now is None else now
        if self._state is not None and (now - self._fetched_at) < self.ttl_seconds:
            return self._state
        return self.refresh(now=now)

    def refresh(self, *, now: Optional[float] = None) -> TotonouFocusState:
        """Re-probe Totonou and fire the intermission / mode-flip edges."""
        previous = self._state
        state = resolve_totonou_focus(self.client)
        self._state = state
        self._fetched_at = time.monotonic() if now is None else now
        if previous is not None:
            if previous.focus_shield_active and not state.focus_shield_active and self.on_focus_cleared:
                self.on_focus_cleared(state)
            if previous.mode != state.mode and self.on_mode_changed:
                self.on_mode_changed(state.mode)
        return state
