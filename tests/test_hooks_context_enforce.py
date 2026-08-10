"""Tests for jing_meta.hooks context/enforce (transcript-inspecting hooks).

Builds synthetic messages.jsonl transcripts and verifies the deny/passthrough
decisions of ``jing-context-bootstrap`` and ``jing-enforce-memory``, plus the
shared transcript-parsing helpers in ``jing_meta.hooks.common``.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jing_meta.hooks import common, context, enforce


def _write_transcript(tmp_path: Path, messages: list[dict]) -> tuple[Path, Path]:
    """Write messages.jsonl (+ a meta.json) into tmp_path; return both paths."""
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
        encoding="utf-8",
    )
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"agent_profile": {"name": "orchestrator"}}), encoding="utf-8")
    return msg_path, meta


def _user(text: str = "hello") -> dict:
    return {"role": "user", "content": text}


def _assistant(tool: str | None = None, content: str = "thinking...") -> dict:
    m = {"role": "assistant", "content": content}
    if tool:
        m["tool_calls"] = [{"id": "call_1", "type": "function",
                            "function": {"name": tool, "arguments": "{}"}}]
    return m


def _payload(transcript_path: Path) -> dict:
    return {"hook_event_name": "post_agent", "transcript_path": str(transcript_path)}


def _run(fn, payload: dict) -> str:
    """Run a hook main() with stdout captured (deterministic; avoids capsys
    interfering with setup_logging's force=True reconfig of the root logger)."""
    buf = io.StringIO()
    with patch.object(common, "read_payload", return_value=payload), redirect_stdout(buf):
        fn()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared transcript helpers
# ---------------------------------------------------------------------------


def test_last_user_index():
    msgs = [_user("a"), _assistant("bash"), _user("b"), _assistant("grep")]
    assert common.last_user_index(msgs) == 2


def test_tool_call_names():
    msgs = [_assistant("bash"), _assistant("read_file")]
    assert common.tool_call_names(msgs) == ["bash", "read_file"]


def test_current_turn():
    msgs = [_user("a"), _assistant("bash"), _user("b"), _assistant("grep")]
    turn = common.current_turn(msgs, common.last_user_index(msgs))
    assert common.tool_call_names(turn) == ["grep"]


def test_read_agent_profile(tmp_path):
    _, meta = _write_transcript(tmp_path, [_user("hi")])
    assert common.read_agent_profile(tmp_path / "messages.jsonl") == "orchestrator"
    assert meta.exists()


# ---------------------------------------------------------------------------
# context-bootstrap
# ---------------------------------------------------------------------------


def test_context_denies_first_substantive_turn_without_reads(tmp_path):
    # First turn: substantive work (bash) with no context reads -> deny.
    msg, _ = _write_transcript(tmp_path, [_user("do the thing"), _assistant("bash")])
    out = _run(context.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_passes_when_both_reads_present(tmp_path):
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("unified-history_search"),
        _assistant("memory_search_nodes"),
    ])
    out = _run(context.main, _payload(msg))
    assert out.strip() == ""  # no deny


def test_context_passes_non_orchestrator(tmp_path):
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(json.dumps(_assistant("bash")) + "\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    assert out.strip() == ""  # coder not enforced


# ---------------------------------------------------------------------------
# enforce-memory
# ---------------------------------------------------------------------------


def test_enforce_denies_substantive_without_memory_write(tmp_path):
    msg, _ = _write_transcript(tmp_path, [_user("fix the bug"), _assistant("bash")])
    out = _run(enforce.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"
    assert "memory" in decision["reason"]


def test_enforce_passes_inspection_only(tmp_path):
    msg, _ = _write_transcript(tmp_path, [_user("look around"), _assistant("read_file")])
    out = _run(enforce.main, _payload(msg))
    assert out.strip() == ""


def test_enforce_passes_when_memory_written(tmp_path):
    msg, _ = _write_transcript(tmp_path, [
        _user("fix the bug"), _assistant("bash"), _assistant("memory_add_observations"),
    ])
    out = _run(enforce.main, _payload(msg))
    assert out.strip() == ""
