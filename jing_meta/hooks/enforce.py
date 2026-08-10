"""Enforce-memory hook — require substantive turns to record learnings.

Console script: ``jing-enforce-memory``. Wired as a Vibe ``post_agent`` hook.

Parses the session transcript and, for the current turn (messages after the
last user message), checks whether the agent did substantive work but made no
memory write. If so, emits a ``post_agent`` deny so Vibe injects a retry
message forcing the agent to record its learnings.

Pure-inspection turns (read/grep/search/todo/skill/questions) are never denied.
"""

from __future__ import annotations

import json
import sys

from jing_meta.hooks import common
from jing_meta.log import setup_logging

MEMORY_WRITE_TOOLS = {
    "memory_create_entities",
    "memory_create_relations",
    "memory_add_observations",
}

# Tool calls that are pure inspection / bookkeeping — doing only these is not
# "substantive work" and never triggers the memory requirement.
INSPECTION_TOOLS = {
    "read_file", "grep", "todo", "skill", "ask_user_question",
    # memory reads
    "memory_search_nodes", "memory_open_nodes", "memory_read_graph",
    "memory_recent", "memory_search_similar",
    "memory-stats_graph_stats", "memory-stats_entity_summary",
    # unified-history reads
    "unified-history_search", "unified-history_list_domain",
    "unified-history_read", "unified-history_summary",
    "unified-history_search_history", "unified-history_search_log",
    # web-archive reads
    "web-archive_archive_list", "web-archive_archive_read",
    "web-archive_rebuild",
}


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
    turn = common.current_turn(messages, last_user)
    calls = common.tool_call_names(turn)

    if not calls:
        return 0  # no tools used this turn — nothing to enforce

    did_substantive = any(c not in INSPECTION_TOOLS for c in calls)
    wrote_memory = any(c in MEMORY_WRITE_TOOLS for c in calls)

    if did_substantive and not wrote_memory:
        reason = (
            "You performed substantive work this turn but did not record any "
            "learning to memory. Before finishing, persist the durable facts, "
            "decisions, or findings from this turn using "
            "memory_create_entities, memory_create_relations, or "
            "memory_add_observations (into the memory knowledge graph). "
            "Do not repeat the work — just record the learning and conclude."
        )
        print(json.dumps({"decision": "deny", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
