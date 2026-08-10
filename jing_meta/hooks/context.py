"""Context-bootstrap hook — force agents to gather context up front.

Console script: ``jing-context-bootstrap``. Wired as Vibe ``pre_tool`` hooks (it
is registered twice: once with ``match = "task"`` for the planner-dispatch gate,
once with a regex covering the heavy work tools for the initial gate). Because it
is a ``pre_tool`` hook it fires BEFORE the tool runs and can DENY the tool call
itself, so the planner subagent is never dispatched and substantive work is never
executed without the required context pass (unlike the old ``post_agent`` design,
which could only inject a retry AFTER the damage was done).

There are two gates:

* INITIAL gate (ALL agents — top-level AND subagents): on the very first
  assistant turn of a session, if the agent tries to run a HEAVY work tool
  (``bash``, ``write_file``, ``edit``, ``shell-sandbox_*``) WITHOUT having first
  run the memory SEMANTIC search (``memory_search_semantic``) in the same turn,
  the hook denies the tool call so it never executes. Memory semantic search is
  the MANDATORY gate; unified-history (prior-work) search is an IDEAL ADDITION
  (recommended, not required). Pure-inspection turns and later turns are never
  denied, so this fires only once, up front.

* PLANNER gate (TOP-LEVEL agents only, on ANY turn): before dispatching a
  planner subagent (consultant, advisor, or the offpeak advisor variant) via the
  ``task`` tool, the agent MUST first run the memory SEMANTIC search in the
  SAME turn, before the dispatch. Only top-level agents hold the ``task`` tool,
  so subagents never hit this gate. The ``task`` tool call is denied (so the
  planner never runs) unless the semantic search already happened this turn.

``common.is_top_level_agent`` distinguishes top-level vs subagent transcripts:
top-level transcripts live at the session root; subagent transcripts live under
an ``agents/`` subdir.
"""

from __future__ import annotations

import json
import sys

from jing_meta.hooks import common
from jing_meta.log import setup_logging

# Planner subagents: dispatching one of these via the ``task`` tool requires the
# memory SEMANTIC search to have been run in the same turn, before the dispatch.
# Includes the offpeak advisor variant.
PLANNER_AGENTS = {"consultant", "advisor", "advisor-offpeak"}

# The ``task`` tool name used to dispatch subagents.
TASK_TOOL = "task"

# Heavy work tools gated by the INITIAL gate. These execute or mutate system
# state, so they must be pre-tool-denied on the first substantive turn unless the
# memory semantic search has already been run this turn. The planner gate is
# handled separately (match = "task"); the rest are matched by this prefix/set.
HEAVY_TOOLS = {
    "bash", "write_file", "edit",
}
HEAVY_TOOL_PREFIXES = (
    "shell-sandbox_",
)

# Context-read tools, split by requirement. Memory SEMANTIC search
# (memory_search_semantic) is the MANDATORY gate: the agent MUST run it on its
# first substantive turn / before dispatching a planner. Unified-history
# (prior-work) search is an IDEAL ADDITION — strongly encouraged but NOT
# required for the gate to pass. A history-only pass (or memory lexical read
# without semantic search) does not satisfy the gate.
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


def _is_heavy(call: str) -> bool:
    """True if *call* is a heavy work tool gated by the INITIAL gate."""
    if call in HEAVY_TOOLS:
        return True
    return any(call.startswith(p) for p in HEAVY_TOOL_PREFIXES)


def _semantic_seen_in_turn(turn: list[dict]) -> bool:
    """True if the (already-executed part of the) current turn contains the
    mandatory memory semantic search."""
    return any(
        tc.get("function", {}).get("name", "") in REQUIRED_MEMORY_TOOLS
        for msg in turn
        for tc in msg.get("tool_calls") or []
    )

# Direction returned for a denied HEAVY tool call (INITIAL gate).
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

# Direction returned for a denied ``task`` call (PLANNER gate).
PLANNER_INSTRUCTION = (
    "You attempted to dispatch a planner subagent (consultant/advisor) without "
    "first running the mandatory memory semantic search. Run the required "
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


def _deny(reason: str) -> int:
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 0


def main() -> int:
    setup_logging()
    payload = common.read_payload()

    if payload.get("hook_event_name") != "pre_tool":
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    messages = common.read_session_messages(transcript_path)
    if not messages:
        return 0

    last_user = common.last_user_index(messages)
    if last_user < 0:
        return 0

    # Current turn = everything after the last user message, i.e. the
    # already-executed tool calls in this turn (the in-flight tool being
    # pre-tool-checked is NOT yet in the transcript).
    turn = common.current_turn(messages, last_user)
    semantic_seen = _semantic_seen_in_turn(turn)

    # ---- PLANNER gate: TOP-LEVEL agents only, on ANY turn -----------------
    # Fires on the ``task`` tool (hooks.toml match = "task"). If the agent is
    # about to dispatch a planner but has not run the semantic search earlier in
    # this same turn, deny the task call so the planner is never spawned.
    if tool_name == TASK_TOOL:
        # Only top-level agents hold the task tool; guard defensively anyway.
        if common.is_top_level_agent(transcript_path):
            target = tool_input.get("agent")
            if target in PLANNER_AGENTS and not semantic_seen:
                return _deny(PLANNER_INSTRUCTION)
        return 0

    # ---- INITIAL gate: ALL agents, on the FIRST assistant turn only --------
    # Fires on heavy work tools (bash / write_file / edit / shell-sandbox_*).
    if not _is_heavy(tool_name):
        return 0

    # Only enforce on the FIRST assistant turn of the session (no earlier
    # assistant tool calls exist before the current turn's user message).
    prior_assistant_tool_use = any(
        msg.get("role") == "assistant" and msg.get("tool_calls")
        for msg in messages[:last_user]
    )
    if prior_assistant_tool_use:
        return 0  # not the first turn — skip (avoid nagging)

    if not semantic_seen:
        return _deny(INSTRUCTION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
