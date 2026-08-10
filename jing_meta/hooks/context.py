"""Context-bootstrap hook — force the orchestrator to gather context up front.

Console script: ``jing-context-bootstrap``. Wired as a Vibe ``post_agent`` hook.

The orchestrator is supposed to restore context (unified-history across ALL
domains + memory knowledge graph) before doing substantive work. Flash models
often skip this unless told. This hook catches the very first assistant turn of
a session: if that turn performs substantive work WITHOUT first calling BOTH a
unified-history read AND a memory read, it emits a ``post_agent`` deny so Vibe
injects a retry message forcing the context pass before proceeding.

Pure-inspection turns and later turns are never denied, so it only fires once,
up front.
"""

from __future__ import annotations

import json
import sys

from jing_meta.hooks import common
from jing_meta.log import setup_logging

# Only enforce for the top-level orchestrator agent. Subagents (coder,
# pro-coder, explorer, advisor, reviewer) receive self-contained tasks and are
# not expected to restore global context; admin/default/plan are also skipped.
ORCHESTRATOR_AGENT = "orchestrator"

# Context-read tools, split by requirement. BOTH a prior-work (unified-history)
# read AND a memory knowledge-graph read are required to satisfy the
# "gathered context" gate; a memory-only (or history-only) pass does not count.
HISTORY_READ_TOOLS = {
    "unified-history_search", "unified-history_list_domain",
    "unified-history_read", "unified-history_summary",
    "unified-history_search_history", "unified-history_search_log",
}
MEMORY_READ_TOOLS = {
    "memory_search_nodes", "memory_open_nodes", "memory_read_graph",
    "memory_traverse", "memory_recent", "memory_search_similar",
    "memory-stats_graph_stats", "memory-stats_entity_summary",
}
CONTEXT_READ_TOOLS = HISTORY_READ_TOOLS | MEMORY_READ_TOOLS

# Pure inspection / bookkeeping — doing only these is not "substantive work"
# and never triggers the requirement.
INSPECTION_ONLY_TOOLS = {
    "read_file", "grep", "todo", "skill", "ask_user_question",
} | CONTEXT_READ_TOOLS

# Direction to include in the injected retry message.
INSTRUCTION = (
    "You started substantive work without first restoring context. Stop and "
    "run the mandatory context pass BEFORE continuing:\n"
    "1. Search PRIOR WORK ACROSS ALL DOMAINS (sessions, web-archive, "
    "transcripts, notifications — not just sessions) with "
    "unified-history_search using domain=\"all\" (start broad, then narrow only "
    "if needed). Read relevant summaries/entries via unified-history_summary "
    "and unified-history_read.\n"
    "2. Recall prior facts from the knowledge graph with memory_search_nodes, "
    "memory_open_nodes, memory_read_graph, or memory-stats_graph_stats.\n"
    "3. For prior commands/runtime context, use unified-history_search_history "
    "and unified-history_search_log.\n"
    "Then continue with the work you already started. Do not redo anything — "
    "just perform the context pass and carry on."
)


def main() -> int:
    setup_logging()
    payload = common.read_payload()

    if payload.get("hook_event_name") != "post_agent":
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return 0

    # Scope to the orchestrator.
    if common.read_agent_profile(transcript_path) != ORCHESTRATOR_AGENT:
        return 0  # not the orchestrator — do not enforce

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

    # Only enforce on the FIRST assistant turn of the session (no earlier
    # assistant tool calls exist before the current turn's user message).
    prior_assistant_tool_use = any(
        msg.get("role") == "assistant" and msg.get("tool_calls")
        for msg in messages[:last_user]
    )
    if prior_assistant_tool_use:
        return 0  # not the first turn — skip (avoid nagging)

    gathered_history = any(c in HISTORY_READ_TOOLS for c in calls)
    gathered_memory = any(c in MEMORY_READ_TOOLS for c in calls)
    # BOTH a prior-work (unified-history) read AND a memory read are required.
    if gathered_history and gathered_memory:
        return 0  # already restored context

    did_substantive = any(c not in INSPECTION_ONLY_TOOLS for c in calls)
    if did_substantive:
        print(json.dumps({"decision": "deny", "reason": INSTRUCTION}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
