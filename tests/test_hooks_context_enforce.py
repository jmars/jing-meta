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

from jing_meta.hooks import common, context, enforce, report


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


def _pre_tool_payload(transcript_path: Path, tool_name: str, tool_input: dict | None = None) -> dict:
    """Build a pre_tool hook payload. The transcript reflects state BEFORE the
    tool executes (the in-flight tool is NOT in the transcript yet)."""
    return {
        "hook_event_name": "pre_tool",
        "transcript_path": str(transcript_path),
        "tool_name": tool_name,
        "tool_input": tool_input or {},
    }


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
    # First turn: about to run bash with no context reads -> deny the tool call.
    msg, _ = _write_transcript(tmp_path, [_user("do the thing")])
    out = _run(context.main, _pre_tool_payload(msg, "bash"))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_passes_when_memory_semantic_search_present(tmp_path):
    # Memory SEMANTIC search already ran this turn -> heavy tool allowed.
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_semantic"),
        _assistant("unified-history_search"),  # ideal addition, still fine
    ])
    out = _run(context.main, _pre_tool_payload(msg, "bash"))
    assert out.strip() == ""  # no deny


def test_context_denies_when_only_history_search(tmp_path):
    # Unified-history + heavy work but NO semantic search -> deny (semantic
    # search is the mandatory gate; history alone does not satisfy it).
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("unified-history_search"),
    ])
    out = _run(context.main, _pre_tool_payload(msg, "bash"))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_denies_when_only_non_semantic_memory_read(tmp_path):
    # A lexical memory read + heavy work but NO semantic search -> deny.
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_nodes"),
    ])
    out = _run(context.main, _pre_tool_payload(msg, "bash"))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_denies_when_shell_sandbox_without_semantic_search(tmp_path):
    # Shell/sandbox execution is heavy work: denied on the first turn unless a
    # semantic search already ran. No prior tool calls -> deny.
    for tool in ("shell-sandbox_shell_run", "shell-sandbox_shell_job_start",
                 "shell-sandbox_shell_job_kill"):
        msg, _ = _write_transcript(tmp_path, [_user("do the thing")])
        out = _run(context.main, _pre_tool_payload(msg, tool))
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", tool


def test_context_shell_sandbox_passes_with_semantic_search(tmp_path):
    # Shell sandbox + semantic search earlier this turn -> gate satisfied.
    msg, _ = _write_transcript(tmp_path, [
        _user("do the thing"),
        _assistant("memory_search_semantic"),
    ])
    out = _run(context.main, _pre_tool_payload(msg, "shell-sandbox_shell_run"))
    assert out.strip() == ""


def test_context_denies_other_heavy_tools(tmp_path):
    # write_file and edit are also heavy work gated on the first turn.
    for tool in ("write_file", "edit"):
        msg, _ = _write_transcript(tmp_path, [_user("do the thing")])
        out = _run(context.main, _pre_tool_payload(msg, tool))
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", tool


def test_context_ignores_non_heavy_tools(tmp_path):
    # A non-heavy tool (e.g. a memory read) is never gated by the initial gate.
    msg, _ = _write_transcript(tmp_path, [_user("do the thing")])
    out = _run(context.main, _pre_tool_payload(msg, "memory_search_nodes"))
    assert out.strip() == ""


def test_context_enforces_any_top_level_agent(tmp_path):
    # Any transcript at the session root is a top-level agent and IS enforced,
    # regardless of profile name (not just orchestrator).
    for name in ("admin", "orchestrator", "plan", "operator", "coder"):
        msg_path = tmp_path / f"msg_{name}.jsonl"
        msg_path.write_text(
            json.dumps({"role": "user", "content": "do the thing"}) + "\n",
            encoding="utf-8")
        (tmp_path / f"meta_{name}.json").write_text(
            json.dumps({"agent_profile": {"name": name}}), encoding="utf-8")
        out = _run(context.main, _pre_tool_payload(msg_path, "bash"))
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", name


def test_context_initial_gate_enforces_subagents_too(tmp_path):
    # The INITIAL gate applies to ALL agents — including subagents. A subagent
    # transcript (under an ``agents/`` dir) about to run bash on its first turn
    # without a semantic search is DENIED.
    agents_dir = tmp_path / "agents" / "coder_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        json.dumps({"role": "user", "content": "do the thing"}) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "bash"))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_context_initial_gate_subagent_passes_with_semantic(tmp_path):
    # A subagent about to run bash WITH a first-turn semantic search passes.
    agents_dir = tmp_path / "agents" / "coder_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in [
            _user("do the thing"),
            _assistant("memory_search_semantic"),
        ]) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "bash"))
    assert out.strip() == ""


def test_context_skips_later_turns(tmp_path):
    # After the first assistant turn has done tool calls, the initial gate no
    # longer fires (no nagging on later turns).
    msg, _ = _write_transcript(tmp_path, [
        _user("earlier"),
        _assistant("bash"),
        _user("now again"),
    ])
    out = _run(context.main, _pre_tool_payload(msg, "bash"))
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
    # Late-turn dispatch to a planner with NO semantic search in the turn -> the
    # task call is denied, even though this is not the first assistant turn and
    # not the orchestrator.
    for agent in ("consultant", "advisor", "advisor-offpeak"):
        msg_path = tmp_path / f"msg_{agent}.jsonl"
        msg_path.write_text(
            "\n".join(json.dumps(m, ensure_ascii=False)
                      for m in _later_turn_transcript([])) + "\n",
            encoding="utf-8")
        (tmp_path / f"meta_{agent}.json").write_text(
            json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
        out = _run(context.main, _pre_tool_payload(msg_path, "task", {"agent": agent}))
        decision = json.loads(out) if out.strip() else None
        assert decision is not None and decision["decision"] == "deny", agent
        assert "planner" in decision["reason"]


def test_planner_dispatch_passes_with_prior_semantic_same_turn(tmp_path):
    # Semantic search BEFORE the task dispatch in the same turn -> passes.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript(
                      [_assistant("memory_search_semantic")]))
        + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "task", {"agent": "advisor"}))
    assert out.strip() == ""


def test_planner_dispatch_denied_semantic_not_yet_run(tmp_path):
    # Semantic search NOT yet run in the turn (it would come after the dispatch,
    # or not at all) -> the task call is denied.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript([])) + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "task", {"agent": "consultant"}))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_planner_dispatch_passes_non_planner_agent(tmp_path):
    # Dispatching a non-planner subagent (coder) does not trip the planner gate.
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False)
                  for m in _later_turn_transcript([])) + "\n",
        encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "admin"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "task", {"agent": "coder"}))
    assert out.strip() == ""


def test_planner_dispatch_on_first_turn_still_enforced(tmp_path):
    # Planner dispatch on the very first turn, no semantic search -> deny.
    msg, _ = _write_transcript(tmp_path, [_user("do it")])
    out = _run(context.main, _pre_tool_payload(msg, "task", {"agent": "advisor"}))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"
    assert "planner" in decision["reason"]


def test_planner_dispatch_passes_missing_agent_arg(tmp_path):
    # A task call with no/invalid agent arg is not a planner dispatch -> no deny.
    msg, _ = _write_transcript(tmp_path, [_user("do it")])
    out = _run(context.main, _pre_tool_payload(msg, "task", {}))
    assert out.strip() == ""


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
                  for m in _later_turn_transcript([])) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "advisor"}}), encoding="utf-8")
    out = _run(context.main, _pre_tool_payload(msg_path, "task", {"agent": "advisor"}))
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


# ---------------------------------------------------------------------------
# enforce-report
# ---------------------------------------------------------------------------


def _task_spawn(agent: str = "coder") -> dict:
    return _assistant("task", args=json.dumps({"agent": agent, "task": "implement X"}))


def _report_write(entity: str = "handoff-x-result") -> dict:
    args = json.dumps({
        "observations": [
            {"entityName": entity,
             "contents": [
                 "stage=report: Task summary: implement X.",
                 "stage=report: Subagents used: coder (implementer).",
                 "stage=report: Outcome: done, tests pass.",
                 "stage=report: Escalations: none.",
                 "stage=report: Failures: none.",
             ]}
        ]
    })
    return _assistant("memory_add_observations", args=args)


def test_report_denies_task_spawn_without_report(tmp_path):
    # Top-level turn spawned a subagent but wrote no stage=report: -> deny.
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"), _task_spawn("coder"), _assistant("bash"),
    ])
    out = _run(report.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"
    assert "stage=report:" in decision["reason"]


def test_report_passes_when_report_written(tmp_path):
    # Spawned a subagent AND appended a stage=report: observation -> pass.
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"), _task_spawn("coder"), _report_write(),
    ])
    out = _run(report.main, _payload(msg))
    assert out.strip() == ""


def test_report_passes_no_subagent_spawn(tmp_path):
    # Did work directly with no task tool -> no handoff, no report required.
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"), _assistant("bash"), _assistant("memory_add_observations"),
    ])
    out = _run(report.main, _payload(msg))
    assert out.strip() == ""


def test_report_denies_task_with_non_report_memory_write(tmp_path):
    # Spawned a subagent and wrote SOME memory, but not a stage=report: -> deny.
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"), _task_spawn("coder"),
        _assistant("memory_add_observations",
                   args=json.dumps({"observations": [
                       {"entityName": "handoff-x-result",
                        "contents": ["stage=implement: changed files"]}]})),
    ])
    out = _run(report.main, _payload(msg))
    decision = json.loads(out) if out.strip() else None
    assert decision is not None and decision["decision"] == "deny"


def test_report_passes_report_via_create_entities(tmp_path):
    # A stage=report: observation may come via memory_create_entities too.
    args = json.dumps({
        "entities": [
            {"name": "handoff-x-report", "entityType": "report",
             "observations": ["stage=report: Summary", "stage=report: Outcome"]}
        ]
    })
    msg, _ = _write_transcript(tmp_path, [
        _user("do it"), _task_spawn("advisor"),
        _assistant("memory_create_entities", args=args),
    ])
    out = _run(report.main, _payload(msg))
    assert out.strip() == ""


def test_report_skips_subagent_transcript(tmp_path):
    # Subagent transcripts are not top-level; the report gate does not apply.
    agents_dir = tmp_path / "agents" / "coder_20260810_120000_abcd"
    agents_dir.mkdir(parents=True)
    msg_path = agents_dir / "messages.jsonl"
    msg_path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in [
            _user("implement X"), _assistant("bash"),
        ]) + "\n",
        encoding="utf-8")
    (agents_dir / "meta.json").write_text(
        json.dumps({"agent_profile": {"name": "coder"}}), encoding="utf-8")
    out = _run(report.main, _payload(msg_path))
    assert out.strip() == ""
