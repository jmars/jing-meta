"""Context-bootstrap hook — force agents to gather context up front.

Console script: ``jing-context-bootstrap``. Wired as a Vibe ``post_agent`` hook.

There are two gates:

* INITIAL gate (ALL agents — top-level AND subagents): on the very first
  assistant turn of a session, if the agent performs substantive work WITHOUT
  first running a memory SEMANTIC search (``memory_search_semantic``), the hook
  emits a ``post_agent`` deny so Vibe injects a retry forcing the context pass.
  Memory semantic search is the MANDATORY gate; unified-history (prior-work)
  search is an IDEAL ADDITION (recommended, not required). Pure-inspection
  turns and later turns are never denied, so this fires only once, up front.

* PLANNER gate (TOP-LEVEL agents only, on ANY turn): before dispatching a
  planner subagent (consultant, advisor, or the offpeak advisor variant) via the
  ``task`` tool, the agent MUST first run the memory SEMANTIC search in the
  SAME turn, before the dispatch. Only top-level agents hold the ``task`` tool,
  so subagents never hit this gate.

``common.is_top_level_agent`` distinguishes the two: top-level transcripts live
at the session root; subagent transcripts live under an ``agents/`` subdir.
"""

from __future__ import annotations

import json
import sys

from jing_meta.hooks import common
from jing_meta.log import setup_logging

# Two gates with different scopes:
#  * INITIAL gate — ALL agents (top-level and subagents) on the first turn.
#  * PLANNER gate — TOP-LEVEL agents only (identified via
#    ``common.is_top_level_agent``: transcripts at the session root), on any
#    turn, before dispatching a planner via the ``task`` tool.

# Planner subagents: dispatching one of these via the ``task`` tool requires the
# memory SEMANTIC search to have been run in the same turn, before the dispatch.
# Includes the offpeak advisor variant.
PLANNER_AGENTS = {"consultant", "advisor", "advisor-offpeak"}

# The ``task`` tool name used to dispatch subagents.
TASK_TOOL = "task"

# Context-read tools, split by requirement. Memory SEMANTIC search
# (memory_search_semantic) is the MANDATORY gate: the orchestrator MUST run it
# on its first substantive turn. Unified-history (prior-work) search is an IDEAL
# ADDITION — strongly encouraged but NOT required for the gate to pass. A
# history-only pass (or memory lexical read without semantic search) does not
# satisfy the gate.
REQUIRED_MEMORY_TOOLS = {
    "memory_search_semantic",
}
# Ideal-addition reads (recommended, not required): prior-work unified-history
# reads and other memory knowledge-graph reads.
HISTORY_READ_TOOLS = {
    "unified-history_search", "unified-history_list_domain",
    "unified-history_read", "unified-history_summary",
    "unified-history_search_history", "unified-history_search_log",
}
OTHER_MEMORY_READ_TOOLS = {
    "memory_search_nodes", "memory_open_nodes", "memory_read_graph",
    "memory_traverse", "memory_recent", "memory_search_similar",
    "memory-stats_graph_stats", "memory-stats_entity_summary",
}
MEMORY_READ_TOOLS = REQUIRED_MEMORY_TOOLS | OTHER_MEMORY_READ_TOOLS
CONTEXT_READ_TOOLS = HISTORY_READ_TOOLS | MEMORY_READ_TOOLS

# Pure inspection / bookkeeping — doing only these is not "substantive work"
# and never triggers the requirement.
INSPECTION_ONLY_TOOLS = {
    "read_file", "grep", "todo", "skill", "ask_user_question",
} | CONTEXT_READ_TOOLS

# Tools that are ALWAYS substantive work — they execute or mutate system state.
# Listed explicitly (and as a prefix) so shell/sandbox execution can never be
# treated as inspection-only, even if INSPECTION_ONLY_TOOLS grows.
SUBSTANTIVE_TOOLS = {
    "bash", "write_file", "edit",
}
# Shell/sandbox family: any tool under these prefixes counts as substantive.
SUBSTANTIVE_TOOL_PREFIXES = (
    "shell-sandbox_shell",
    "shell-sandbox_",
)


def _is_substantive(call: str) -> bool:
    if call in SUBSTANTIVE_TOOLS:
        return True
    if any(call.startswith(p) for p in SUBSTANTIVE_TOOL_PREFIXES):
        return True
    # Anything that is not pure-inspection/bookkeeping is substantive.
    return call not in INSPECTION_ONLY_TOOLS


def _ordered_tool_calls(turn: list[dict]) -> list[tuple[str, dict]]:
    """Return ``(name, parsed_arguments)`` for the turn's tool calls, in order.

    Arguments are parsed from the tool-call JSON string; unparseable args become
    an empty dict. Mirrors ``common.tool_call_names`` ordering but keeps the
    per-call arguments (needed to see which subagent a ``task`` call targets).
    """
    out: list[tuple[str, dict]] = []
    for msg in turn:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            if not name:
                continue
            args: dict = {}
            try:
                parsed = json.loads(fn.get("arguments") or "{}")
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError):
                args = {}
            out.append((name, args))
    return out


def _dispatched_planner_without_semantic(turn: list[dict]) -> bool:
    """True if the turn dispatches a planner via ``task`` before running
    ``memory_search_semantic`` in the SAME turn.

    Iterates the turn's tool calls in order: once ``memory_search_semantic`` is
    seen it is satisfied for the rest of the turn; if a ``task`` call targeting a
    planner agent is seen first (or before any semantic search), we flag a deny.
    """
    semantic_seen = False
    for name, args in _ordered_tool_calls(turn):
        if name == "memory_search_semantic":
            semantic_seen = True
        elif name == TASK_TOOL and args.get("agent") in PLANNER_AGENTS:
            if not semantic_seen:
                return True
    return False

# Direction to include in the injected retry message.
INSTRUCTION = (
    "You started substantive work without first running the mandatory memory "
    "semantic search. Stop and run the required context pass BEFORE "
    "continuing:\n"
    "1. (REQUIRED) Search your memory knowledge graph with "
    "memory_search_semantic — the embedding-based semantic search that "
    "recalls prior facts/concepts from memory (even when the query shares no "
    "words with the stored text). This is mandatory before substantive work.\n"
    "2. (IDEAL ADDITION — strongly recommended, not required) Also search "
    "PRIOR WORK ACROSS ALL DOMAINS (sessions, web-archive, transcripts, "
    "notifications — not just sessions) with unified-history_search using "
    "domain=\"all\" (start broad, then narrow only if needed). Read relevant "
    "summaries/entries via unified-history_summary and unified-history_read.\n"
    "3. (IDEAL ADDITION) For prior commands/runtime context, use "
    "unified-history_search_history and unified-history_search_log.\n"
    "Then continue with the work you already started. Do not redo anything — "
    "just perform the context pass and carry on."
)

# Direction to include in the injected retry when a planner is dispatched
# without a preceding same-turn memory semantic search.
PLANNER_INSTRUCTION = (
    "You dispatched a planner subagent (consultant/advisor) without first "
    "running the mandatory memory semantic search. Stop and run the required "
    "context pass BEFORE dispatching the planner:\n"
    "1. (REQUIRED) Search your memory knowledge graph with "
    "memory_search_semantic — the embedding-based semantic search that "
    "recalls prior facts/concepts from memory (even when the query shares no "
    "words with the stored text). Run this FIRST, before the task dispatch.\n"
    "2. (IDEAL ADDITION — strongly recommended, not required) Also search "
    "PRIOR WORK ACROSS ALL DOMAINS (sessions, web-archive, transcripts, "
    "notifications) with unified-history_search using domain=\"all\", and read "
    "relevant summaries via unified-history_summary / unified-history_read.\n"
    "Then dispatch the planner with the gathered context. Do not dispatch it "
    "until the semantic memory search has been run this turn."
)


def main() -> int:
    setup_logging()
    payload = common.read_payload()

    if payload.get("hook_event_name") != "post_agent":
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return 0

    messages = common.read_session_messages(transcript_path)
    if not messages:
        return 0

    last_user = common.last_user_index(messages)
    if last_user < 0:
        return 0

    # Current turn: everything after the last user message.
    turn = common.current_turn(messages, last_user)
    calls = common.tool_call_names(turn)

    if not calls:
        return 0  # no tools this turn — nothing to enforce

    # Planner-dispatch gate: TOP-LEVEL agents only, on ANY turn. Only top-level
    # agents hold the ``task`` tool, so the task->planner dispatch can only
    # originate there. If a planner is dispatched before a same-turn memory
    # semantic search, deny. Subagents do NOT get this gate.
    if common.is_top_level_agent(transcript_path) and _dispatched_planner_without_semantic(turn):
        print(json.dumps({"decision": "deny", "reason": PLANNER_INSTRUCTION}))
        return 0

    # Initial context gate: applies to ALL agents (top-level and subagents) on
    # the FIRST assistant turn of the session.
    # Only enforce on the FIRST assistant turn of the session (no earlier
    # assistant tool calls exist before the current turn's user message).
    prior_assistant_tool_use = any(
        msg.get("role") == "assistant" and msg.get("tool_calls")
        for msg in messages[:last_user]
    )
    if prior_assistant_tool_use:
        return 0  # not the first turn — skip (avoid nagging)

    # Memory SEMANTIC search is the mandatory gate. Unified-history is an ideal
    # addition (encouraged) but does NOT affect whether the gate is satisfied.
    gathered_memory_semantic = any(c in REQUIRED_MEMORY_TOOLS for c in calls)
    if gathered_memory_semantic:
        return 0  # already ran the required memory semantic search

    did_substantive = any(_is_substantive(c) for c in calls)
    if did_substantive:
        print(json.dumps({"decision": "deny", "reason": INSTRUCTION}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
