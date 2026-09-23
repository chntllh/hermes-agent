"""Client-facing projection helpers for model-only compaction carriers."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    _HISTORICAL_SUMMARY_PREFIXES,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
    _content_text_for_contains,
)


_COMPACTION_INTERNAL_FIELDS = (
    "tool_calls",
    "finish_reason",
    "reasoning",
    # Provider replay/metadata fields that ride the wire on every request but are invisible to
    # ``msg["content"]``/``msg["tool_calls"]`` accounting. Codex Responses sessions in particular carry
    # ``codex_reasoning_items`` blobs of ``encrypted_content`` that can dominate the serialized session (a
    # measured 214-turn session held ~115K tokens / 27% of its payload there — #55572).
    # ``reasoning_details`` is handled separately (see ``_reasoning_details_text_chars``): its signed/base64
    # envelope is excluded from the budget, mirroring the preflight estimator's exclusion in
    # ``model_metadata._estimate_message_tokens_without_images`` (#73298).
    # An assistant turn may carry only reasoning/thinking content with no visible text (extended-thinking
    # turns, thinking-only recovery responses). Such a turn is persisted with its reasoning fields and is
    # recallable from the transcript, but dropping it here as "empty" makes it vanish from the
    # resumed/reloaded session view while the desktop's reasoning disclosure has nothing to render. Keep it
    # when it carries reasoning so the "Thinking…" block still shows. (#44022)
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)

_SUMMARY_PREFIXES = (
    SUMMARY_PREFIX,
    LEGACY_SUMMARY_PREFIX,
    *_HISTORICAL_SUMMARY_PREFIXES,
)

_SUMMARY_CLOSING_MARKERS = (
    _SUMMARY_END_MARKER,
    "</context_summary>",
    "</summary>",
    "[/CONTEXT SUMMARY]",
    "[/CONTEXT_SUMMARY]",
)

_SUMMARY_TAG_PAIRS = (
    ("<context_summary>", "</context_summary>"),
    ("[CONTEXT SUMMARY]", "[/CONTEXT SUMMARY]"),
    ("[CONTEXT_SUMMARY]", "[/CONTEXT_SUMMARY]"),
)


def is_compaction_summary_message(message: Any) -> bool:
    """Return True when *message* is a context-compaction handoff summary.

    Requires either explicit ``_compressed_summary`` metadata or a valid delimiter
    pair (opening prefix/tag and closing marker/tag such as ``_SUMMARY_END_MARKER``)
    rather than bare prefix matching, preventing false-positive drops of user text.
    """
    if isinstance(message, dict):
        if bool(message.get(COMPRESSED_SUMMARY_METADATA_KEY)):
            return True
        content = message.get("content")
    else:
        content = message

    if content is None:
        return False

    text = _content_text_for_contains(content).lstrip()
    if not text:
        return False

    # 1. Merged summary carrier: contains the merged delimiter and a closing marker
    if _MERGED_SUMMARY_DELIMITER in text:
        after = text.split(_MERGED_SUMMARY_DELIMITER, 1)[1]
        if any(close in after for close in _SUMMARY_CLOSING_MARKERS):
            return True

    # 2. Summary beginning with a known compaction prefix AND ending with a closing marker/tag
    for prefix in _SUMMARY_PREFIXES:
        if text.startswith(prefix):
            if any(close in text for close in _SUMMARY_CLOSING_MARKERS):
                return True

    # 3. Explicit tag pairs: <context_summary>...</context_summary> or [CONTEXT SUMMARY]...[/CONTEXT SUMMARY]
    for open_tag, close_tag in _SUMMARY_TAG_PAIRS:
        open_idx = text.find(open_tag)
        if open_idx != -1:
            close_idx = text.rfind(close_tag)
            if close_idx > open_idx:
                return True

    # 4. Standalone <summary>...</summary> when not part of HTML <details>
    if "<summary>" in text and "</summary>" in text and "<details" not in text:
        if text.find("<summary>") < text.rfind("</summary>"):
            return True

    return False


def project_compaction_message_for_display(message: Any) -> Optional[Dict[str, Any]]:
    """Return authentic transcript content, or ``None`` for a pure handoff.

    Model-facing recovery history retains the complete carrier. Display
    projections instead remove the handoff, inherited tool state, and internal
    reasoning while preserving any real prior-tail content or live user ask
    embedded in the carrier.
    """
    if not isinstance(message, dict):
        return None
    if not is_compaction_summary_message(message):
        return message.copy()

    projected = ContextCompressor._strip_context_summary_handoff_message(message)
    if projected is None:
        return None

    projected = projected.copy()
    for key in _COMPACTION_INTERNAL_FIELDS:
        projected.pop(key, None)

    # Preserve display_kind (specifically "steer") on merged carriers so that
    # downstream consumers (e.g. session_history) can unmask out-of-band steer messages.
    carrier_display_kind = message.get("display_kind")
    if carrier_display_kind:
        projected["display_kind"] = carrier_display_kind
    else:
        projected.pop("display_kind", None)

    return projected
