"""Shared helpers for jing-meta's Vibe maintenance hooks.

Both hooks (graph-gardener, memory-archiver) are thin ``post_agent`` wrappers
around the dreamer/archiver engines. They share:
  * throttle / growth-gate state persisted under ``$VIBE_HOME/state``
  * read-only entity/observation counts from the memory DB
  * a structured JSONL maintenance digest
  * draining the Vibe hook stdin payload (which the cadence logic ignores)

These modules are Vibe-agnostic in the sense that they only read env vars and
stdin per Vibe's hook wire protocol; they never hardcode user paths. Storage
paths come from ``jing_meta.config`` (``JING_HOME`` / env overrides) and the
state dir from ``VIBE_HOME``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from jing_meta import config as _config
from jing_meta.log import get_logger

logger = get_logger(__name__)


def vibe_home() -> Path:
    """Return the Vibe home dir (default ``~/.vibe``)."""
    return Path(os.environ.get("VIBE_HOME", Path.home() / ".vibe"))


def state_dir() -> Path:
    return vibe_home() / "state"


def drain_stdin() -> None:
    """Read (and discard) the Vibe hook JSON payload on stdin.

    The cadence/growth decisions do not depend on the payload; this just
    prevents a half-read pipe. Never raises.
    """
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 - best effort
        pass


def read_payload() -> dict:
    """Read and parse the Vibe hook JSON payload on stdin.

    Returns {} on malformed/missing input (never raises). Hooks that inspect
    the payload (context-bootstrap, enforce-memory) use this instead of
    ``drain_stdin``.
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def read_session_messages(path: str | Path) -> list[dict]:
    """Load a Vibe session messages.jsonl into a list of message dicts.

    Skips blank/invalid lines. Returns [] on missing/unreadable file.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    messages: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            msg = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            messages.append(msg)
    return messages


def read_agent_profile(transcript_path: str | Path) -> str | None:
    """Return the agent profile name from the sibling meta.json, or None."""
    meta_path = Path(transcript_path).parent / "meta.json"
    try:
        with open(meta_path, encoding="utf-8", errors="replace") as f:
            meta = json.load(f)
        return (meta.get("agent_profile") or {}).get("name")
    except (OSError, json.JSONDecodeError):
        return None


def last_user_index(messages: list[dict]) -> int:
    """Return the index of the last user message, or -1."""
    last = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last = i
    return last


def tool_call_names(messages: list[dict]) -> list[str]:
    """Flatten the tool-call names across a set of messages (current turn)."""
    names: list[str] = []
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            name = tc.get("function", {}).get("name", "")
            if name:
                names.append(name)
    return names


def current_turn(messages: list[dict], last_user: int) -> list[dict]:
    """Return the messages of the current turn (everything after last user)."""
    return messages[last_user + 1:] if last_user >= 0 else messages


def memory_db() -> Path:
    """Resolve the memory DB path (env override respected)."""
    return Path(os.environ.get("MEMORY_DB_PATH", str(_config.memory_db())))


def archive_dir() -> Path:
    """Resolve the archive dir (env override respected)."""
    return Path(
        os.environ.get("MEMORY_ARCHIVE_DIR", str(_config.archive_dir()))
    )


# ---------------------------------------------------------------------------
# Throttle / growth-gate state
# ---------------------------------------------------------------------------


def load_state(name: str) -> dict:
    path = state_dir() / f"{name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(name: str, state: dict) -> None:
    path = state_dir() / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def graph_counts(db: Path | None = None) -> tuple[int, int] | None:
    """Return (entity_count, observation_count) from the memory DB, or None."""
    db = db or memory_db()
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            return int(n_entities), int(n_obs)
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("graph_counts failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Maintenance digest
# ---------------------------------------------------------------------------

_DIGEST_NAME = "maintenance-digest.jsonl"
_MAX_DIGEST_ENTRIES = 500


def _digest_path() -> Path:
    return state_dir() / _DIGEST_NAME


def digest_append(record: dict) -> None:
    """Append one digest record (JSONL) and trim the file to a cap."""
    record = dict(record)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    path = _digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("digest append failed: %s", e)
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_DIGEST_ENTRIES:
            path.write_text("\n".join(lines[-_MAX_DIGEST_ENTRIES:]) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("digest trim failed: %s", e)


def digest_read(limit: int = 20) -> list[dict]:
    """Return the most recent digest records, newest first."""
    path = _digest_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-limit:][::-1]


def print_digest(limit: int = 20) -> None:
    """CLI body: print the recent maintenance digest."""
    records = digest_read(limit)
    if not records:
        print("No maintenance activity recorded yet.")
        return
    print(f"Recent maintenance activity ({len(records)} record(s)):")
    for r in records:
        ts = str(r.get("ts", "?"))[:19]
        system = r.get("system", "?")
        action = r.get("action", "")
        detail = r.get("detail", "")
        print(f"  [{ts}] {system}: {action} — {detail}")


def digest_cli() -> int:
    """``jing-maintenance-digest`` console-script entry point."""
    limit = int(os.environ.get("MAINTENANCE_DIGEST_LIMIT", "20"))
    print_digest(limit)
    return 0


# ---------------------------------------------------------------------------
# Small shared utility used by both hooks
# ---------------------------------------------------------------------------


def monotonic_now() -> float:
    return time.time()
