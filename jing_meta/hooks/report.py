"""Enforce-report hook — require orchestrator turns that spawned subagents to
write the final handoff report.

Console script: ``jing-enforce-report``. Wired as a Vibe ``post_agent`` hook.

Parses the session transcript and, for the current turn (messages after the last
user message), checks whether a TOP-LEVEL agent spawned at least one subagent via
the ``task`` tool (i.e. a real handoff occurred) but did NOT persist a
``stage=report:`` observation to the memory handoff chain. If so, emits a
``post_agent`` deny so Vibe injects a retry message forcing the orchestrator to
write the final report (subagents used, outcome, escalations, failures, task
summary) to the ``handoff-<slug>-result`` node before concluding.

Scope is deliberately narrow:
  * Top-level agents only (subagent transcripts under an ``agents/`` dir are
    skipped — only the orchestrator owns the final handoff report).
  * Only turns that actually spawned a subagent (used the ``task`` tool).
    Turns where the agent did everything itself with bash/edits are covered by
    ``enforce-memory`` for learnings and are NOT nagged here for a report.
  * Pure-inspection turns are never denied.

A ``stage=report:`` write is detected by scanning the arguments of memory-write
tool calls (``memory_add_observations`` / ``memory_create_entities``) in the
current turn for an observation content prefixed ``stage=report:``.
"""

from __future__ import annotations

import json
import sys

from jing_meta.hooks import common
from jing_meta.log import setup_logging

# The tool used to spawn subagents — the signal that a handoff occurred.
TASK_TOOL = "task"

# Memory-write tools that carry observations; we scan their args for the marker.
MEMORY_WRITE_TOOLS = {
    "memory_create_entities",
    "memory_add_observations",
}

# The marker that identifies a handoff-report observation on the chain.
REPORT_MARKER = "stage=report:"


def _contains_marker(value) -> bool:
    """Recursively search *value* (parsed JSON) for the REPORT_MARKER string."""
    if isinstance(value, str):
        return REPORT_MARKER in value
    if isinstance(value, dict):
        return any(_contains_marker(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_marker(v) for v in value)
    return False


def _report_written_in_turn(turn: list[dict]) -> bool:
    """True if the current turn wrote a ``stage=report:`` observation to memory."""
    for msg in turn:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name", "") not in MEMORY_WRITE_TOOLS:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if _contains_marker(args):
                return True
    return False


def main() -> int:
    setup_logging()
    payload = common.read_payload()

    if payload.get("hook_event_name") != "post_agent":
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return 0

    # Only top-level agents own the final handoff report.
    if not common.is_top_level_agent(transcript_path):
        return 0

    messages = common.read_session_messages(transcript_path)
    if not messages:
        return 0

    last_user = common.last_user_index(messages)
    turn = common.current_turn(messages, last_user)
    calls = common.tool_call_names(turn)

    if not calls:
        return 0  # no tools used this turn — nothing to enforce

    spawned_subagent = TASK_TOOL in calls
    if not spawned_subagent:
        return 0  # no handoff this turn — no report required

    if not _report_written_in_turn(turn):
        reason = (
            "You spawned one or more subagents this turn but did not write the "
            "final handoff report to the memory chain. Before concluding, append "
            "a `stage=report:` observation to the unit's `handoff-<slug>-result` "
            "node (via memory_add_observations, or memory_create_entities if no "
            "node exists yet) covering: task summary, subagents used (in order + "
            "role), outcome (with verification/tests), escalations, and failures. "
            "This is the durable record for whoever picks the task up next. "
            "Do not redo the work — just write the report and conclude."
        )
        print(json.dumps({"decision": "deny", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
