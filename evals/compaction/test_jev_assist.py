"""Tests for the eval-only Jev-assisted compaction arm."""
from pathlib import Path
from typing import Any
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evals.compaction.jev_arm import JevOptions, fake_asker  # noqa: E402
from evals.compaction.jev_assist_arm import JevAssistCompactor  # noqa: E402


def _messages():
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Fix the bug; do not edit generated files."},
    ]
    for i in range(8):
        call_id = f"call-{i}"
        messages.append({
            "role": "assistant", "content": f"step {i}",
            "tool_calls": [{"id": call_id, "function": {"name": "terminal", "arguments": json_args(i)}}],
        })
        messages.append({
            "role": "tool", "tool_call_id": call_id,
            "content": f"important result {i}: exact-error-{i} path=src/module_{i}.py",
        })
    messages.append({"role": "user", "content": "What remains?"})
    return messages


def json_args(i):
    return '{"command":"pytest tests/test_%d.py"}' % i


def _summary_response():
    response = MagicMock()
    response.choices[0].message.content = "## Goal\nContinue the work.\n## Critical Context\nAnchors preserved."
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=30)
    return response


def test_jev_assist_passes_selected_anchors_to_existing_summary():
    captured: list[str] = []

    def fake_summary(messages=None, **kwargs):
        captured.append(str(messages[0].get("content", "")) if messages else "")
        return _summary_response()

    comp = JevAssistCompactor(
        asker=fake_asker(keep_call=0.9, keep_result=0.9),
        jev_options=JevOptions(result_budget_tokens=4000, preserve_recent_messages=2),
        max_anchor_chars=8000,
        max_anchors=3,
    )
    with patch("agent.context_compressor.call_llm", side_effect=fake_summary), \
         patch("agent.auxiliary_client.call_llm", side_effect=fake_summary):
        output = comp.compress(_messages(), current_tokens=100_000, force=True)

    assert output
    assert captured
    prompt = "\n".join(captured)
    assert "JEV RETENTION ANCHORS" in prompt
    assert "exact-error-" in prompt
    assert comp.stats["jev_requests"] >= 1
    assert comp.stats["anchors"] >= 1


def test_jev_failure_falls_back_to_normal_summary():
    def broken_asker(state, questions):
        raise RuntimeError("synthetic Jev outage")

    comp = JevAssistCompactor(asker=broken_asker)
    with patch("agent.context_compressor.call_llm", side_effect=lambda **kwargs: _summary_response()), \
         patch("agent.auxiliary_client.call_llm", side_effect=lambda **kwargs: _summary_response()):
        output = comp.compress(_messages(), current_tokens=100_000, force=True)

    assert output
    assert comp.stats["jev_error"] == "synthetic Jev outage"
    assert comp.stats["jev_requests"] == 0


if __name__ == "__main__":
    test_jev_assist_passes_selected_anchors_to_existing_summary()
    test_jev_failure_falls_back_to_normal_summary()
    print("jev-assisted compaction tests: ALL PASS")
