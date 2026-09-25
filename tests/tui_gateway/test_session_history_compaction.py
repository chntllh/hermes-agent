"""Unit tests for session history compaction projection and steer turn unmasking (Issue #120214)."""

from __future__ import annotations

import pytest

import tui_gateway.server as server
from tui_gateway.session_history import _history_to_messages
from agent.prompt_builder import STEER_DISPLAY_KIND, steer_user_row
from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    HISTORICAL_TASK_HEADING,
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
)


STANDALONE_SUMMARY = (
    f"{SUMMARY_PREFIX}\n\n"
    f"{HISTORICAL_TASK_HEADING}\nold work\n\n"
    f"{_SUMMARY_END_MARKER}"
)


def _merged_carrier(prior_content: str, summary: str = STANDALONE_SUMMARY) -> str:
    return (
        f"{_MERGED_PRIOR_CONTEXT_HEADER}\n"
        f"{prior_content}\n\n"
        f"{_MERGED_SUMMARY_DELIMITER}\n\n"
        f"{summary}"
    )


class TestSessionHistoryCompactionSteerPreservation:
    def test_merged_compaction_carrier_steer_row_is_unmasked_with_display_kind(self):
        """When compaction merges into a steer row, session history must unmask the
        steer text and preserve display_kind='steer' instead of exposing raw out-of-band markers.
        """
        user_words = "stop the build and run git status"
        steer = steer_user_row(user_words)
        assert steer["display_kind"] == STEER_DISPLAY_KIND
        assert user_words in steer["content"]
        assert "[OUT-OF-BAND USER MESSAGE" in steer["content"]

        carrier = {
            "role": "user",
            "content": _merged_carrier(steer["content"]),
            "display_kind": steer["display_kind"],
            COMPRESSED_SUMMARY_METADATA_KEY: True,
            "tool_calls": [{"id": "stale-call"}],
            "reasoning": "internal compaction reasoning",
        }

        messages = server._history_to_messages([carrier])
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        assert msg["text"] == user_words
        assert msg["display_kind"] == STEER_DISPLAY_KIND
        assert "[OUT-OF-BAND USER MESSAGE" not in msg["text"]

    def test_uncompacted_steer_row_is_unmasked(self):
        """Uncompacted steer turn preserves display_kind and extracts user words."""
        user_words = "pause execution"
        steer = steer_user_row(user_words)
        messages = server._history_to_messages([steer])
        assert len(messages) == 1
        assert messages[0] == {
            "role": "user",
            "text": user_words,
            "display_kind": STEER_DISPLAY_KIND,
        }

    def test_legacy_end_marker_only_merged_steer_row_is_unmasked(self):
        """When compaction appends an ask after the end marker, steer turn is unmasked."""
        user_words = "switch branch to main"
        steer = steer_user_row(user_words)
        carrier = {
            "role": "user",
            "content": f"{STANDALONE_SUMMARY}\n\n{steer['content']}",
            "display_kind": "steer",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
        messages = server._history_to_messages([carrier])
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["text"] == user_words
        assert messages[0]["display_kind"] == "steer"


class TestSessionHistoryPrefixCollisionSafety:
    def test_user_message_starting_with_legacy_summary_prefix_is_not_dropped(self):
        """A normal user message starting with '[CONTEXT SUMMARY]:' must not be dropped."""
        user_content = f"{LEGACY_SUMMARY_PREFIX} here is what we discussed with the client"
        history = [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": "Got it, I will keep that in mind."},
        ]
        messages = server._history_to_messages(history)
        assert len(messages) == 2
        assert messages[0] == {"role": "user", "text": user_content}
        assert messages[1] == {"role": "assistant", "text": "Got it, I will keep that in mind."}

    def test_tool_message_with_summary_prefix_is_not_dropped(self):
        """Tool outputs that happen to start with summary prefix must not be dropped."""
        history = [
            {"role": "tool", "content": "[CONTEXT SUMMARY]: 14 passed, 0 failed", "tool_name": "pytest"},
        ]
        messages = server._history_to_messages(history)
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["name"] == "pytest"

    def test_assistant_message_with_summary_prefix_is_not_dropped(self):
        """Assistant replies mentioning summary prefix without delimiter end markers survive."""
        reply = "[CONTEXT SUMMARY]: The issue was resolved in the database layer."
        history = [{"role": "assistant", "content": reply}]
        messages = server._history_to_messages(history)
        assert len(messages) == 1
        assert messages[0] == {"role": "assistant", "text": reply}


class TestSessionHistoryCompactionProjection:
    def test_standalone_summary_is_dropped(self):
        """A pure standalone compaction handoff message produces no client-facing messages."""
        history = [
            {"role": "user", "content": STANDALONE_SUMMARY},
            {"role": "assistant", "content": "hello there"},
        ]
        messages = server._history_to_messages(history)
        assert len(messages) == 1
        assert messages[0] == {"role": "assistant", "text": "hello there"}

    def test_merged_assistant_carrier_unwraps_prior_content_and_strips_scaffolding(self):
        """Merged assistant carrier preserves authentic prior response and drops internal fields."""
        carrier = {
            "role": "assistant",
            "content": _merged_carrier("Prior response text."),
            "tool_calls": [{"id": "stale_tc"}],
            "reasoning": "provider thinking",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
        messages = server._history_to_messages([carrier])
        assert len(messages) == 1
        assert messages[0] == {"role": "assistant", "text": "Prior response text."}

    def test_full_conversation_with_compacted_steer_carrier(self):
        """Full transcript projection: user ask, assistant turn, steer turn merged into
        compaction summary carrier, followed by assistant and user turns.
        """
        user_steer = "abort the download and clean temp files"
        steer = steer_user_row(user_steer)
        carrier = {
            "role": "user",
            "content": _merged_carrier(steer["content"]),
            "display_kind": steer["display_kind"],
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
        history = [
            {"role": "user", "content": "Please start the download"},
            {"role": "assistant", "content": "Starting download..."},
            carrier,
            {"role": "assistant", "content": "Download aborted and cleaned."},
            {"role": "user", "content": "Thank you!"},
        ]
        messages = server._history_to_messages(history)
        assert len(messages) == 5
        assert messages[0] == {"role": "user", "text": "Please start the download"}
        assert messages[1] == {"role": "assistant", "text": "Starting download..."}
        assert messages[2] == {"role": "user", "text": user_steer, "display_kind": "steer"}
        assert messages[3] == {"role": "assistant", "text": "Download aborted and cleaned."}
        assert messages[4] == {"role": "user", "text": "Thank you!"}

    def test_direct_session_history_import_does_not_raise_name_error(self):
        """Importing _history_to_messages directly from tui_gateway.session_history
        resolves project_compaction_message_for_display without NameError.
        """
        history = [
            {"role": "user", "content": "[CONTEXT SUMMARY]: prefix query"},
            {"role": "assistant", "content": "response"},
        ]
        # Calling via server's rebound function
        server_messages = server._history_to_messages(history)
        assert len(server_messages) == 2

        # Verify that project_compaction_message_for_display is present in session_history module namespace
        import tui_gateway.session_history as sh
        assert hasattr(sh, "project_compaction_message_for_display")
        assert callable(sh.project_compaction_message_for_display)

    def test_inflight_task_replay_header_is_hidden_from_display(self):
        """Synthetic continuation rows restated after compaction for model consumption
        must not leak into client history or shift display ordinals.
        """
        from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER

        history = [
            {"role": "user", "content": "Lets setup kanban"},
            {"role": "assistant", "content": "Working on it..."},
            {
                "role": "user",
                "content": f"{_INFLIGHT_TASK_REPLAY_HEADER}\nLets setup kanban",
            },
            {"role": "assistant", "content": "Done setting up kanban."},
        ]
        messages = server._history_to_messages(history)
        assert len(messages) == 3
        assert messages[0] == {"role": "user", "text": "Lets setup kanban"}
        assert messages[1] == {"role": "assistant", "text": "Working on it..."}
        assert messages[2] == {"role": "assistant", "text": "Done setting up kanban."}
