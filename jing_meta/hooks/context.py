"""Context-bootstrap hook — force the orchestrator to gather context up front.

Console script: ``jing-context-bootstrap``. Wired as a Vibe ``post_agent`` hook.

The orchestrator is supposed to restore context (memory knowledge graph +
unified-history across ALL domains) before doing substantive work. Flash models
often skip this unless told. This hook catches the very first assistant turn of
a session: if that turn performs substantive work WITHOUT first running a
memory SEMANTIC search (``memory_search_semantic``), it emits a ``post_agent``
deny so Vibe injects a retry message forcing the required context pass before
proceeding.

Memory semantic search is the MANDATORY gate. Unified-history (prior-work)
search is an IDEAL ADDITION — strongly recommended but not required for the
gate to pass.

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
