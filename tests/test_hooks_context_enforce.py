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


def _assistant(tool: str | None = None, content: str = "thinking...", args: str = "{}") -> dict:
    m = {"role": "assistant", "content": content}
    if tool:
        m["tool_calls"] = [{"id": "call_1", "type": "function",
                            "function": {"name": tool, "arguments": args}}]
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


def test_context_passes_when_memory_semantic_search_present(tmp_path):
    # Memory SEMANTIC search satisfies the mandatory gate (history optional).
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_semantic"),
        _assistant("unified-history_search"),  # ideal addition, still fine
    ])
    out = _run(context.main, _payload(msg))
    assert out.strip() == ""  # no deny


def test_context_denies_when_only_history_search(tmp_path):
    # Unified-history + substantive work but NO semantic search -> deny
    # (semantic search is the mandatory gate; history alone does not satisfy it).
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("unified-history_search"),
        _assistant("bash"),
    ])
    out = _run(context.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_denies_when_only_non_semantic_memory_read(tmp_path):
    # A lexical memory read + substantive work but NO semantic search -> deny.
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_nodes"),
        _assistant("bash"),
    ])
    out = _run(context.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_denies_when_shell_sandbox_without_semantic_search(tmp_path):
    # Shell/sandbox execution is substantive: it must trip the deny even when
    # the turn otherwise looks like inspection. Semantic search still required.
    for tool in ("shell-sandbox_shell_run", "shell-sandbox_shell_job_start",
                 "shell-sandbox_shell_job_kill"):
        msg, _ = _write_transcript(tmp_path, [
            _user("do the thing"),
            _assistant(tool),
        ])
        out = _run(context.main, _payload(msg))
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", tool


def test_context_shell_sandbox_passes_with_semantic_search(tmp_path):
    # Shell sandbox + semantic search -> gate satisfied (no deny).
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_semantic"),
        _assistant("shell-sandbox_shell_run"),
    ])
    out = _run(context.main, _payload(msg))
    assert out.strip() == ""


def test_context_enforces_any_top_level_agent(tmp_path):
    # Any transcript at the session root is a top-level agent and IS enforced,
    # regardless of profile name (not just orchestrator).
    for name in ("admin", "orchestrator", "plan", "operator", "coder"):
        msg_path = tmp_path / f"msg_{name}.jsonl"
        msg_path.write_text(
            "\n".join(json.dumps(m, ensure_ascii=False)
                      for m in [_user("do the thing"), _assistant("bash")]) + "\n",
            encoding="utf-8")
        (tmp_path / f"meta_{name}.json").write_text(
            json.dumps({"agent_profile": {"name": name}}), encoding="utf-8")
        out = _run(context.main, {
            "hook_event_name": "post_agent", "transcript_path": str(msg_path)})
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", name


def test_context_initial_gate_enforces_subagents_too(tmp_path):
    # The INITIAL gate applies to ALL agents — including subagents. A subagent
    # transcript (under an ``agents/`` dir) doing substantive work on its first
    # turn without a semantic search is DENIED.
    agents_dir = tmp_path / "agents" / "coder_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in [_user("do the thing"), _assistant("bash")]) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_initial_gate_subagent_passes_with_semantic(tmp_path):
    # A subagent doing substantive work WITH a first-turn semantic search passes
    # the initial gate (no deny).
    agents_dir = tmp_path / "agents" / "coder_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in [
                      _user("do the thing"),
                      _assistant("memory_search_semantic"),
                      _assistant("bash"),
                  ]) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    assert out.strip() == ""


def test_common_is_top_level_agent(tmp_path):
    top = tmp_path / "messages.jsonl"
    assert common.is_top_level_agent(top) is True
    sub = tmp_path / "agents" / "coder_x" / "messages.jsonl"
    assert common.is_top_level_agent(sub) is False


# ---------------------------------------------------------------------------
# context-bootstrap — planner-dispatch gate (any top-level agent, any turn)
# ---------------------------------------------------------------------------


def _task(agent: str) -> dict:
    return _assistant("task", args=json.dumps({"agent": agent, "task": "plan it"}))


def _later_turn_transcript(messages: list[dict]) -> list[dict]:
    """Prefix with an earlier completed turn so the turn under test is NOT the
    first assistant turn (proves the planner gate fires regardless of turn)."""
    return [
        _user("earlier"),
        _assistant("bash"),
        _user("now dispatch a planner"),
    ] + messages


def test_planner_dispatch_denied_without_prior_semantic(tmp_path):
    # Late-turn dispatch to a planner with NO semantic search in the turn -> deny,
    # even though this is not the first assistant turn and not the orchestrator.
    for agent in ("consultant", "advisor", "advisor-offpeak"):
        msg_path = tmp_path / f"msg_{agent}.jsonl"
        msg_path.write_text(
            "\n".join(json.dumps(m, ensure_ascii=False)
                      for m in _later_turn_transcript([_task(agent)])) + "\n",
            encoding="utf-8")
        (tmp_path / f"meta_{agent}.json").write_text(
            json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
        payload = {"hook_event_name": "post_agent", "transcript_path": str(msg_path)}
        out = _run(context.main, payload)
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", agent
        assert "planner" in decision["reason"]


def test_planner_dispatch_passes_with_prior_semantic_same_turn(tmp_path):
    # Semantic search BEFORE the task dispatch in the same turn -> passes.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript(
                      [_assistant("memory_search_semantic"), _task("advisor")]))
        + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    assert out.strip() == ""


def test_planner_dispatch_passes_semantic_after_in_turn(tmp_path):
    # Semantic search AFTER the dispatch in the same turn does NOT satisfy the
    # "before" ordering requirement -> deny.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript(
                      [_task("consultant"), _assistant("memory_search_semantic")]))
        + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_planner_dispatch_passes_non_planner_agent(tmp_path):
    # Dispatching a non-planner subagent (coder) does not trip the planner gate.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript([_task("coder")])) + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    # Not first turn + not orchestrator -> no deny from the planner gate.
    assert out.strip() == ""


def test_planner_dispatch_on_first_turn_still_enforced(tmp_path):
    # Planner dispatch on the very first turn, no semantic search -> deny.
    msg, _ = _write_transcript(tmp_path, [_user("do it"), _task("advisor")])
    out = _run(context.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"
    assert "planner" in decision["reason"]


def test_planner_dispatch_passes_missing_agent_arg(tmp_path):
    # A task call with no/invalid agent arg is not a planner dispatch -> no deny.
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"),
        _assistant("task", args="{}"),
    ])
    out = _run(context.main, _payload(msg))
    # Not a planner dispatch, but IS a substantive first turn without semantic
    # search -> the general first-turn gate denies it.
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"
    assert "planner" not in decision["reason"]


def test_planner_gate_skips_subagent_transcripts(tmp_path):
    # The PLANNER gate is TOP-LEVEL only. A subagent transcript (under an
    # ``agents/`` dir) dispatching a planner with no semantic search is NOT
    # denied by the planner gate. (If it's the subagent's first substantive
    # turn, it would still be caught by the INITIAL gate with INSTRUCTION — so
    # use a later turn here to isolate the planner gate.)
    agents_dir = tmp_path / "agents" / "advisor_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript([_task("advisor")])) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "advisor"}}), encoding="utf-8")
    out = _run(context.main, _payload(msg_path))
    assert out.strip() == ""  # planner gate not applied to subagents


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
