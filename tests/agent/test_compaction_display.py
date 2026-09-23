"""Tests for agent/compaction_display.py: compaction projection and summary recognition."""

from __future__ import annotations

import pytest

from agent.compaction_display import (
    is_compaction_summary_message,
    project_compaction_message_for_display,
)
from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    HISTORICAL_TASK_HEADING,
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    _HISTORICAL_SUMMARY_PREFIXES,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
)
from agent.prompt_builder import STEER_DISPLAY_KIND, steer_user_row


STANDALONE_SUMMARY = (
    f"{SUMMARY_PREFIX}\n\n"
    f"{HISTORICAL_TASK_HEADING}\nold work\n\n"
    f"{_SUMMARY_END_MARKER}"
)

MERGED_CARRIER_TEXT = (
    f"{_MERGED_PRIOR_CONTEXT_HEADER}\n"
    "Prior task in progress.\n\n"
    f"{_MERGED_SUMMARY_DELIMITER}\n\n"
    f"{STANDALONE_SUMMARY}"
)


class TestIsCompactionSummaryMessage:
    def test_recognizes_compressed_summary_metadata(self):
        msg = {"role": "user", "content": "plain text", COMPRESSED_SUMMARY_METADATA_KEY: True}
        assert is_compaction_summary_message(msg) is True

    def test_recognizes_standalone_summary_with_end_marker(self):
        msg = {"role": "user", "content": STANDALONE_SUMMARY}
        assert is_compaction_summary_message(msg) is True

    def test_recognizes_legacy_summary_with_end_marker(self):
        content = f"{LEGACY_SUMMARY_PREFIX} old work\n\n{_SUMMARY_END_MARKER}"
        msg = {"role": "user", "content": content}
        assert is_compaction_summary_message(msg) is True

    def test_recognizes_historical_prefix_with_end_marker(self):
        for hist_prefix in _HISTORICAL_SUMMARY_PREFIXES:
            content = f"{hist_prefix}\n\n{HISTORICAL_TASK_HEADING}\nold\n\n{_SUMMARY_END_MARKER}"
            assert is_compaction_summary_message({"role": "user", "content": content}) is True

    def test_recognizes_merged_carrier_with_end_marker(self):
        msg = {"role": "assistant", "content": MERGED_CARRIER_TEXT}
        assert is_compaction_summary_message(msg) is True

    def test_recognizes_tag_pairs(self):
        xml_summary = "<context_summary>previous turns summarized</context_summary>"
        assert is_compaction_summary_message({"role": "assistant", "content": xml_summary}) is True

        bracket_summary = "[CONTEXT SUMMARY]\nprevious turns summarized\n[/CONTEXT SUMMARY]"
        assert is_compaction_summary_message({"role": "user", "content": bracket_summary}) is True

        legacy_bracket_close = f"{LEGACY_SUMMARY_PREFIX} summary\n[/CONTEXT SUMMARY]"
        assert is_compaction_summary_message({"role": "user", "content": legacy_bracket_close}) is True

    def test_recognizes_standalone_summary_tag(self):
        tag_summary = "<summary>previous turns summarized</summary>"
        assert is_compaction_summary_message({"role": "assistant", "content": tag_summary}) is True

    def test_ignores_html_details_summary(self):
        details_html = "<details><summary>Click to view</summary>Some info</details>"
        assert is_compaction_summary_message({"role": "user", "content": details_html}) is False

    def test_prefix_collision_safety_legacy_summary_prefix_without_delimiters(self):
        # Bare prefix without closing marker/tag is a normal message, NOT a compaction summary
        bare_msgs = [
            {"role": "user", "content": "[CONTEXT SUMMARY]: user discussing a summary"},
            {"role": "user", "content": f"{LEGACY_SUMMARY_PREFIX} here are my meeting notes"},
            {"role": "tool", "content": "[CONTEXT SUMMARY]: test report output"},
            {"role": "assistant", "content": "[CONTEXT SUMMARY]: draft of ideas"},
        ]
        for msg in bare_msgs:
            assert is_compaction_summary_message(msg) is False, f"Failed for {msg['content']}"

    def test_prefix_collision_safety_compaction_prefix_without_end_marker(self):
        # User message mentioning the compaction prefix text without end marker
        content = f"{SUMMARY_PREFIX}\nUser asking what this prefix means"
        msg = {"role": "user", "content": content}
        assert is_compaction_summary_message(msg) is False

    def test_ignores_regular_messages(self):
        assert is_compaction_summary_message({"role": "user", "content": "hello world"}) is False
        assert is_compaction_summary_message({"role": "assistant", "content": ""}) is False
        assert is_compaction_summary_message({"role": "tool", "content": "ok"}) is False
        assert is_compaction_summary_message({"role": "user", "content": None}) is False
        assert is_compaction_summary_message(None) is False
        assert is_compaction_summary_message("plain string") is False


class TestProjectCompactionMessageForDisplay:
    def test_pure_standalone_summary_returns_none(self):
        msg = {
            "role": "user",
            "content": STANDALONE_SUMMARY,
            "tool_calls": [{"id": "stale"}],
            "reasoning": "internal reasoning",
        }
        assert project_compaction_message_for_display(msg) is None

    def test_merged_carrier_preserves_real_content_and_strips_internal_fields(self):
        carrier = {
            "role": "assistant",
            "content": MERGED_CARRIER_TEXT,
            "tool_calls": [{"id": "stale-call"}],
            "finish_reason": "tool_calls",
            "reasoning": "provider reasoning",
            "reasoning_content": "provider reasoning content",
            "reasoning_details": [{"type": "summary", "text": "detail"}],
            "codex_reasoning_items": [{"type": "item"}],
            "codex_message_items": [{"type": "msg"}],
        }
        projected = project_compaction_message_for_display(carrier)
        assert projected is not None
        assert projected["role"] == "assistant"
        assert projected["content"] == "Prior task in progress."
        for key in (
            "tool_calls", "finish_reason", "reasoning", "reasoning_content",
            "reasoning_details", "codex_reasoning_items", "codex_message_items",
        ):
            assert key not in projected

    def test_preserves_steer_display_kind_on_merged_carrier(self):
        steer = steer_user_row("please stop and re-check files")
        carrier = {
            "role": "user",
            "content": f"{_MERGED_PRIOR_CONTEXT_HEADER}\n{steer['content']}\n\n{_MERGED_SUMMARY_DELIMITER}\n\n{STANDALONE_SUMMARY}",
            "display_kind": steer["display_kind"],
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
        assert carrier["display_kind"] == STEER_DISPLAY_KIND

        projected = project_compaction_message_for_display(carrier)
        assert projected is not None
        assert projected["display_kind"] == STEER_DISPLAY_KIND
        assert steer["content"] in projected["content"]
        assert "tool_calls" not in projected

    def test_preserves_steer_display_kind_on_end_marker_only_carrier(self):
        steer = steer_user_row("cancel the run")
        carrier = {
            "role": "user",
            "content": f"{STANDALONE_SUMMARY}\n\n{steer['content']}",
            "display_kind": "steer",
        }
        projected = project_compaction_message_for_display(carrier)
        assert projected is not None
        assert projected["display_kind"] == "steer"
        assert projected["content"] == steer["content"]

    def test_prefix_collision_message_is_not_dropped(self):
        # A normal user message starting with [CONTEXT SUMMARY]: without delimiters
        # must be returned as a copy, not dropped as None
        msg = {
            "role": "user",
            "content": "[CONTEXT SUMMARY]: user report from the field",
            "timestamp": 1234567890.0,
        }
        projected = project_compaction_message_for_display(msg)
        assert projected is not None
        assert projected == msg
        assert projected is not msg

    def test_invalid_input_returns_none(self):
        assert project_compaction_message_for_display(None) is None
        assert project_compaction_message_for_display("not a dict") is None
        assert project_compaction_message_for_display([]) is None
