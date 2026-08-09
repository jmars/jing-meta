"""Unified History MCP Server — config-driven cross-domain search.

Replaces vibe-history and teams-logs — single server, domain-based architecture.

Domains:
  sessions      — Vibe session message logs (JSONL), with AI-generated summaries
  transcripts   — Tactiq meeting transcripts (TXT/DOCX), with AI-generated summaries
  notifications — Teams notification logs (JSONL)

Tools:
  search(domain, query, ...)          — full-text search with FST fast path
  list_domain(domain, date_from, ...) — list available files with metadata
  read(domain, id, max_entries, ...)  — read entries from a file
  summary(domain, id)                 — get AI-generated summary
  rebuild(domain)                     — rebuild FST indexes
  search_history(query, ...)          — search vibe command history
  search_log(query, level, ...)       — search vibe runtime log
"""

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config, DomainConfig, load_config
from .renderers import render_list_entry, render_read_entry
from .transcript import parse_transcript_file, read_transcript_text
from .indexer import build_index, search_fst, resolve_file_idx, _iter_domain_files

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_config: Config | None = None


def _get_config() -> Config:
    """Lazy-load config once."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "unified-history",
    instructions="Search and read agent sessions, meeting transcripts, and notification logs",
)

# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_matches_cwd(session_dir: Path, cwd: str | None) -> bool:
    """Return True if a session was created in ``cwd`` or a subdirectory of it.

    Uses the session's ``meta.json`` -> ``environment.working_directory`` with a
    prefix match ("cwd and everything under it"). Sessions with no recorded
    working directory are kept (fail-open) so no data is silently hidden.
    Only meaningful for the ``sessions`` domain.
    """
    if not cwd:
        return True
    try:
        cwd = os.path.abspath(os.path.expanduser(cwd))
    except (TypeError, ValueError):
        return True
    meta = _load_json(session_dir / "meta.json") or {}
    wd = (meta.get("environment") or {}).get("working_directory")
    if not wd:
        return True  # no wd recorded — don't hide it
    try:
        wd = os.path.abspath(wd)
    except (TypeError, ValueError):
        return True
    if wd == cwd:
        return True
    return wd.startswith(cwd + os.sep)


def _msg_snippet(line: str) -> str:
    """Extract readable snippet from a JSONL message or raw line."""
    try:
        msg = json.loads(line)
        content = msg.get("content", "")
        if content:
            return str(content)[:300]
        tcs = msg.get("tool_calls", [])
        if tcs:
            parts = []
            for tc in tcs:
                fn = tc.get("function", {})
                parts.append(f"{fn.get('name', '?')}(...)")
            return " | ".join(parts)
        return "(no content)"
    except json.JSONDecodeError:
        return line[:300]


# ---------------------------------------------------------------------------
# Domain helpers — date extraction
# ---------------------------------------------------------------------------


def _session_date_from_dir(d: Path) -> date | None:
    meta = _load_json(d / "meta.json")
    if meta:
        st = meta.get("start_time")
        if st:
            try:
                return datetime.fromisoformat(st).date()
            except (ValueError, TypeError):
                pass
    return None


def _transcript_date(p: Path) -> date | None:
    raw = read_transcript_text(p)
    if not raw:
        return None
    m = re.search(r"^Date:\s*(\d{4}-\d{2}-\d{2})", raw, re.MULTILINE)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _notify_date(p: Path) -> date | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


_DATE_EXTRACTORS = {
    "sessions": _session_date_from_dir,
    "transcripts": _transcript_date,
    "notifications": _notify_date,
    "web-archive": _notify_date,
    "dns-whois": _notify_date,
    "image-analysis": _notify_date,
    "pdf-extract": _notify_date,
}


# ---------------------------------------------------------------------------
# Domain helpers — list metadata
# ---------------------------------------------------------------------------


def _list_session_meta(d: Path) -> dict:
    meta = _load_json(d / "meta.json") or {}
    lines = []
    msg_path = d / "messages.jsonl"
    if msg_path.exists():
        try:
            lines = [
                ln
                for ln in msg_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if ln.strip()
            ]
        except OSError:
            pass
    title = (meta.get("title") or "")[:80]
    return {
        "title": title + ("..." if len(meta.get("title") or "") > 80 else ""),
        "started": meta.get("start_time", "?"),
        "ended": meta.get("end_time", ""),
        "message_count": len(lines),
        "tokens": meta.get("stats", {}).get("session_total_llm_tokens", 0),
        "cost": meta.get("stats", {}).get("session_cost", 0),
    }


def _list_transcript_meta(p: Path) -> dict:
    try:
        parsed = parse_transcript_file(p)
    except OSError:
        return {"title": p.stem, "turns": 0, "participants": []}
    return {
        "title": parsed.get("meeting") or p.stem,
        "date": parsed.get("meeting_date"),
        "duration": parsed.get("duration", "?"),
        "turns": len(parsed.get("turns", [])),
        "participants": parsed.get("participants", []),
        "size_kb": round(p.stat().st_size / 1024, 1),
    }


def _list_notify_meta(p: Path) -> dict:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            count = sum(1 for ln in f if ln.strip())
    except OSError:
        count = 0
    return {
        "title": p.stem,
        "entries": count,
        "size_kb": round(p.stat().st_size / 1024, 1),
    }


_LIST_META = {
    "sessions": _list_session_meta,
    "transcripts": _list_transcript_meta,
    "notifications": _list_notify_meta,
    "web-archive": _list_notify_meta,
    "dns-whois": _list_notify_meta,
    "image-analysis": _list_notify_meta,
    "pdf-extract": _list_notify_meta,
}


# ---------------------------------------------------------------------------
# Domain helpers — read entries
# ---------------------------------------------------------------------------


def _read_session_messages(d: Path, max_n: int, role: str | None) -> list[dict]:
    msg_path = d / "messages.jsonl"
    try:
        lines = [
            ln.strip()
            for ln in msg_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if ln.strip()
        ]
    except OSError:
        return []
    lines.reverse()
    lines = lines[:max_n]
    entries = []
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_role = msg.get("role", "?")
        if role and msg_role.lower() != role.lower():
            continue
        entries.append(msg)
    return entries


def _read_transcript_turns(p: Path, max_n: int, speaker: str | None) -> list[dict]:
    try:
        parsed = parse_transcript_file(p)
    except OSError:
        return []
    turns = parsed.get("turns", [])
    if speaker:
        turns = [t for t in turns if t.get("speaker", "").lower() == speaker.lower()]
    return turns[:max_n]


def _read_notify_entries(p: Path, max_n: int, _filter: str | None = None) -> list[dict]:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []
    lines.reverse()
    lines = lines[:max_n]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"raw": line})
    return entries


def _read_jsonl_entries(p: Path, max_n: int, _filter: str | None = None) -> list[dict]:
    """Generic JSONL read: each line is a JSON object, newest first."""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []
    lines.reverse()
    lines = lines[:max_n]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"raw": line})
    return entries


_READ_ENTRIES = {
    "sessions": _read_session_messages,
    "transcripts": _read_transcript_turns,
    "notifications": _read_notify_entries,
    "web-archive": _read_jsonl_entries,
    "dns-whois": _read_jsonl_entries,
    "image-analysis": _read_jsonl_entries,
    "pdf-extract": _read_jsonl_entries,
}


# ---------------------------------------------------------------------------
# Domain helpers — search lines (for slow path)
# ---------------------------------------------------------------------------


def _session_search_lines(d: Path) -> list[str]:
    msg_path = d / "messages.jsonl"
    try:
        return [
            ln.strip()
            for ln in msg_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if ln.strip()
        ]
    except OSError:
        return []


def _transcript_search_lines(p: Path) -> list[str]:
    text = read_transcript_text(p)
    return text.splitlines() if text else []


def _notify_search_lines(p: Path) -> list[str]:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []


def _jsonl_search_lines(p: Path) -> list[str]:
    """Generic JSONL search: extract 'content' field if present, else raw line."""
    try:
        lines = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                    content = obj.get("content", "")
                    lines.append(content if content else ln)
                except json.JSONDecodeError:
                    lines.append(ln)
        return lines
    except OSError:
        return []


_SEARCH_LINES = {
    "sessions": _session_search_lines,
    "transcripts": _transcript_search_lines,
    "notifications": _notify_search_lines,
    "web-archive": _jsonl_search_lines,
    "dns-whois": _jsonl_search_lines,
    "image-analysis": _jsonl_search_lines,
    "pdf-extract": _jsonl_search_lines,
}


# ---------------------------------------------------------------------------
# Domain helpers — load summary
# ---------------------------------------------------------------------------


def _load_session_summary(d: Path) -> dict | None:
    return _load_json(d / "summary.json")


def _load_transcript_summary(p: Path) -> dict | None:
    return _load_json(p.parent / (p.stem + ".summary.json"))


_LOAD_SUMMARY = {
    "sessions": _load_session_summary,
    "transcripts": _load_transcript_summary,
    "notifications": lambda p: None,
}


# ---------------------------------------------------------------------------
# Domain resolution helpers
# ---------------------------------------------------------------------------


def _resolve_cfg(name: str) -> DomainConfig | None:
    """Resolve a domain name to its config. Returns None if unknown."""
    cfg = _get_config()
    return cfg.domains.get(name)


def _resolve_file(cfg: DomainConfig, id: str) -> Path | None:
    """Resolve a file/directory ID to a path. Rejects path traversal.

    The ``id`` parameter comes from the MCP client (typically an LLM)
    and must not escape the configured domain directory.
    """
    import glob as _glob

    # Reject empty IDs and path traversal sequences
    if not id or id.isspace():
        return None
    if ".." in Path(id).parts or id.startswith("/"):
        return None
    if cfg.type == "dirs":
        candidates = sorted(cfg.dir.glob(_glob.escape(id) + "*"))
        if not candidates:
            candidates = sorted(cfg.dir.glob("*" + _glob.escape(id)))
    else:
        # Build exact-extension set from the domain config
        if cfg.extensions:
            exact_extensions = set(cfg.extensions)
        else:
            exact_extensions = {".txt", ".jsonl", ".docx"}
        if any(id.endswith(ext) for ext in exact_extensions):
            candidates = sorted(cfg.dir.rglob(_glob.escape(id)))
        else:
            candidates = sorted(cfg.dir.rglob(_glob.escape(id) + "*"))
        candidates = [c for c in candidates if c.is_file() and c.exists()]
    if not candidates:
        return None
    # Verify candidate stays within the domain directory
    resolved = candidates[0].resolve()
    if not resolved.is_relative_to(cfg.dir.resolve()):
        return None
    return candidates[0]


# ---------------------------------------------------------------------------
# Domain -> extractor pattern name used for FST
# ---------------------------------------------------------------------------

_DOMAIN_EXTRACTOR_MAP = {
    "sessions": "jsonl",
    "transcripts": "transcript",
    "notifications": "notification",
}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search(
    domain: str = "all",
    query: str = "",
    max_results: int = 20,
    date_from: str | None = None,
    date_to: str | None = None,
    regex: bool = False,
    context_lines: int = 2,
    case_sensitive: bool = False,
    max_matches_per_file: int = 5,
    role: str | None = None,
    speaker: str | None = None,
    cwd: str | None = None,
) -> str:
    """Search across domains with FST-backed full-text search.

    Domains:
      sessions      — Vibe coding session conversations
      transcripts   — Meeting transcripts (supports speaker filter)
      notifications — Teams notification logs
      all           — Search across all three domains at once (default)

    Args:
        domain:               Domain to search, or "all" for cross-domain (default: all)
        query:                Search text (or regex pattern if regex=True)
        max_results:          Maximum total matches to return (default 20)
        date_from:            Optional start of date range (YYYY-MM-DD)
        date_to:              Optional end of date range (YYYY-MM-DD, inclusive)
        regex:                If True, treat query as a regex pattern
        context_lines:        Lines of surrounding context per match (default 2)
        case_sensitive:       If True, match case-sensitively (default False)
        max_matches_per_file: Max matches from any single file (default 5)
        role:                 [sessions] Filter by role: user, assistant, tool
        speaker:              [transcripts] Filter by speaker name
        cwd:                  [sessions] Only include sessions whose working
                              directory is ``cwd`` or a subdirectory of it
                              (prefix match). Ignored for other domains.
    """
    cfg = _get_config()

    if domain not in cfg.domains and domain != "all":
        valid = ", ".join(cfg.domains)
        return f"Unknown domain '{domain}'. Valid: all, {valid if valid else '(no domains configured)'}"

    if not query.strip():
        return "Empty query - refine your search."

    domains_to_search = list(cfg.domains.keys()) if domain == "all" else [domain]

    # Validate domain-specific filters
    if role and not any("role" in cfg.domains[d].filters for d in domains_to_search):
        return f"'role' filter not supported for domain '{domain}'"
    if speaker and not any(
        "speaker" in cfg.domains[d].filters for d in domains_to_search
    ):
        return f"'speaker' filter not supported for domain '{domain}'"

    # --- Fast path: try FST for each domain ---
    all_fst_results: list[dict] = []
    for d_name in domains_to_search:
        d_cfg = cfg.domains[d_name]
        fst_results = _search_via_fst(d_cfg, query, max_results * 5)
        if fst_results:
            for r in fst_results:
                r["_domain"] = d_name
            all_fst_results.extend(fst_results)

    if all_fst_results:
        return _format_search_results(
            all_fst_results,
            domain,
            query,
            max_results,
            date_from,
            date_to,
            context_lines,
            max_matches_per_file,
            regex,
            case_sensitive,
            role,
            speaker,
            cwd,
        )

    # --- Slow path: line-by-line scan ---
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        if re.search(r"(\+\s*\)|\*\s*\)|\}\s*\))\s*[+*]", query):
            return "Potentially unsafe regex - nested quantifiers detected."
        try:
            pattern = re.compile(query, flags)
        except re.error as e:
            return f"Invalid regex: {e}"
    else:
        pattern = re.compile(re.escape(query), flags)

    d_from = date.fromisoformat(date_from) if date_from else None
    d_to = date.fromisoformat(date_to) if date_to else None

    all_matches: list[dict] = []

    for d_name in domains_to_search:
        d_cfg = cfg.domains[d_name]
        files = _iter_domain_files(d_cfg)
        date_fn = _DATE_EXTRACTORS.get(d_name, lambda p: None)
        search_fn = _SEARCH_LINES.get(d_name, lambda p: [])

        for f in files:
            if not f.exists():
                continue
            if cwd and d_name == "sessions" and not _session_matches_cwd(f, cwd):
                continue
            fd = date_fn(f)
            if d_from and fd and fd < d_from:
                continue
            if d_to and fd and fd > d_to:
                continue

            lines = search_fn(f)
            if not lines:
                continue

            file_matches = []
            for line_no, line in enumerate(lines, 1):
                if not pattern.search(line):
                    continue

                # Role filter (sessions only)
                if role and d_name == "sessions":
                    try:
                        msg = json.loads(line)
                        if msg.get("role", "").lower() != role.lower():
                            continue
                    except json.JSONDecodeError:
                        if role.lower() != "user":
                            continue

                # Speaker filter (transcripts only)
                if speaker and d_name == "transcripts":
                    parsed = parse_transcript_file(f)
                    if parsed:
                        turn_by_line = {}
                        for t in parsed["turns"]:
                            for ln in range(
                                t.get("line_start", 0), t.get("line_end", 0) + 1
                            ):
                                turn_by_line[ln] = t
                        turn = turn_by_line.get(line_no - 1)
                        if (
                            not turn
                            or turn.get("speaker", "").lower() != speaker.lower()
                        ):
                            continue

                if len(file_matches) >= max_matches_per_file:
                    break

                ctx_before = lines[max(0, line_no - 1 - context_lines) : line_no - 1]
                ctx_after = lines[line_no : line_no + context_lines]
                display = _msg_snippet(line)

                file_matches.append(
                    {
                        "file_id": f.name,
                        "source": d_name,
                        "date": fd.isoformat() if fd else "?",
                        "line": line_no,
                        "match": display,
                        "context_before": [l[:300] for l in ctx_before],
                        "context_after": [l[:300] for l in ctx_after],
                    }
                )
            all_matches.extend(file_matches)

    if not all_matches:
        return f"No matching entries found for '{query}' in {domain}."

    all_matches.sort(key=lambda m: (m["date"], m["line"]), reverse=False)
    all_matches.sort(key=lambda m: m["date"], reverse=True)
    return _render_matches(
        all_matches[:max_results],
        domain,
        query,
        date_from,
        date_to,
        regex,
        role,
        speaker,
    )


@mcp.tool()
def list_domain(
    domain: str,
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = 50,
    cwd: str | None = None,
) -> str:
    """List available files in a domain with metadata and summaries.

    Args:
        domain:      Domain to list (sessions, transcripts, notifications)
        date_from:   Optional start of date range (YYYY-MM-DD)
        date_to:     Optional end of date range (YYYY-MM-DD, inclusive)
        max_results: Maximum entries to show (default 50)
        cwd:         [sessions] Only list sessions whose working directory is
                     ``cwd`` or a subdirectory of it (prefix match).
    """
    cfg = _get_config()
    d_cfg = cfg.domains.get(domain)
    if d_cfg is None:
        valid = ", ".join(cfg.domains)
        return f"Unknown domain '{domain}'. Valid: {valid if valid else '(no domains configured)'}"

    d_from = date.fromisoformat(date_from) if date_from else None
    d_to = date.fromisoformat(date_to) if date_to else None

    files = _iter_domain_files(d_cfg)
    if not files:
        return f"No {d_cfg.label}s found."

    date_fn = _DATE_EXTRACTORS.get(domain, lambda p: None)
    list_meta_fn = _LIST_META.get(domain, lambda p: {})
    load_summary_fn = _LOAD_SUMMARY.get(domain, lambda p: None)

    label = f"filtered: {date_from} -> {date_to}" if date_from or date_to else "all"
    output = [f"{d_cfg.label.title()}s ({label}):"]

    count = 0
    for f in files:
        if count >= max_results:
            output.append(f"\n... (truncated at {max_results})")
            break

        if cwd and domain == "sessions" and not _session_matches_cwd(f, cwd):
            continue

        fd = date_fn(f)
        if d_from and fd and fd < d_from:
            continue
        if d_to and fd and fd > d_to:
            continue

        meta = list_meta_fn(f)
        summary = load_summary_fn(f)

        parts = [f"  {f.name}"]
        parts.extend(render_list_entry(domain, meta))

        if summary:
            goal_or_topic = summary.get("goal") or summary.get("topic") or ""
            if goal_or_topic:
                parts.append(f"  Summary: {goal_or_topic}")
            status = summary.get("status", "")
            if status:
                parts.append(f"  Status: {status}")
            tags = summary.get("tags", [])
            if tags:
                parts.append(f"  Tags: {', '.join(tags)}")

        output.append("\n".join(parts) + "\n")
        count += 1

    if count == 0:
        return f"No {d_cfg.label}s match the filter."
    return "\n".join(output)


@mcp.tool()
def read(
    domain: str,
    id: str,
    max_entries: int = 50,
    role: str | None = None,
    speaker: str | None = None,
    cwd: str | None = None,
) -> str:
    """Read entries from a domain file.

    Args:
        domain:      Domain (sessions, transcripts, notifications)
        id:          File/directory name or unique prefix
        max_entries: Maximum entries to return, newest first (default 50)
        role:        [sessions] Filter by role: user, assistant, tool
        speaker:     [transcripts] Filter by speaker name
        cwd:         [sessions] Only read sessions whose working directory is
                     ``cwd`` or a subdirectory of it (prefix match).
    """
    cfg = _get_config()
    d_cfg = cfg.domains.get(domain)
    if d_cfg is None:
        valid = ", ".join(cfg.domains)
        return f"Unknown domain '{domain}'. Valid: {valid if valid else '(no domains configured)'}"

    target = _resolve_file(d_cfg, id)
    if target is None:
        return f"{d_cfg.label.title()} not found: {id}"

    if cwd and domain == "sessions" and not _session_matches_cwd(target, cwd):
        return f"{d_cfg.label.title()} not found in {cwd}: {id}"

    list_meta_fn = _LIST_META.get(domain, lambda p: {})
    load_summary_fn = _LOAD_SUMMARY.get(domain, lambda p: None)
    read_fn = _READ_ENTRIES.get(domain, lambda p, n, _: [])

    meta = list_meta_fn(target)
    summary = load_summary_fn(target)
    filter_val = role if domain == "sessions" else speaker
    entries = read_fn(target, max_entries, filter_val)

    title = meta.get("title") or meta.get("meeting", target.name)
    output = [f"{d_cfg.label.title()}: {target.name}", f"  Title: {title}"]

    if domain == "sessions":
        output.append(f"  Started: {meta.get('started', '?')}")
    elif domain == "transcripts":
        output.append(f"  Date: {meta.get('date', '?')}")
        output.append(f"  Duration: {meta.get('duration', '?')}")
        if meta.get("participants"):
            output.append(f"  Participants: {', '.join(meta['participants'])}")

    if summary:
        goal_or_topic = summary.get("goal") or summary.get("topic")
        if goal_or_topic:
            output.append(f"  Summary: {goal_or_topic}")

    output.append(f"  Showing {len(entries)} entries (newest first):")

    for entry in entries:
        output.extend(render_read_entry(domain, entry))

    return "\n".join(output)


@mcp.tool()
def summary(domain: str, id: str, cwd: str | None = None) -> str:
    """Get the AI-generated summary for a domain entry.

    Args:
        domain: Domain (sessions, transcripts)
        id:     File/directory name or unique prefix
        cwd:    [sessions] Only allow sessions whose working directory is
                ``cwd`` or a subdirectory of it (prefix match).
    """
    cfg = _get_config()
    d_cfg = cfg.domains.get(domain)
    if d_cfg is None:
        valid = ", ".join(cfg.domains)
        return f"Unknown domain '{domain}'. Valid: {valid if valid else '(no domains configured)'}"
    if domain not in ("sessions", "transcripts"):
        return f"Summaries not available for domain '{domain}'."

    target = _resolve_file(d_cfg, id)
    if target is None:
        return f"{d_cfg.label.title()} not found: {id}"

    if cwd and domain == "sessions" and not _session_matches_cwd(target, cwd):
        return f"{d_cfg.label.title()} not found in {cwd}: {id}"

    load_summary_fn = _LOAD_SUMMARY.get(domain, lambda p: None)
    list_meta_fn = _LIST_META.get(domain, lambda p: {})

    s = load_summary_fn(target)
    if s is None:
        meta = list_meta_fn(target)
        return (
            f"No summary available for {target.name}\n"
            f"  Title: {meta.get('title', '?')}\n"
            f"This {d_cfg.label} has not been summarized yet."
        )

    if domain == "sessions":
        return _format_session_summary(s, target.name)
    else:
        return _format_transcript_summary(s, target.name)


@mcp.tool()
def rebuild(domain: str = "all") -> str:
    """Rebuild FST indexes for configured domains.

    Args:
        domain: Domain to rebuild, or "all" for all configured domains
    """
    cfg = _get_config()

    if domain != "all" and domain not in cfg.domains:
        valid = ", ".join(cfg.domains)
        return f"Unknown domain '{domain}'. Valid: all, {valid if valid else '(no domains configured)'}"

    domains_to_build = list(cfg.domains.keys()) if domain == "all" else [domain]

    results: list[str] = []
    for d_name in domains_to_build:
        d_cfg = cfg.domains[d_name]
        success, msg = build_index(d_cfg)
        results.append(f"  {d_name}: {'✓' if success else '✗'} {msg}")

    return "Rebuild results:\n" + "\n".join(results)


@mcp.tool()
def search_history(
    query: str,
    max_results: int = 30,
    regex: bool = False,
    case_sensitive: bool = False,
) -> str:
    """Search the Vibe command history (vibehistory file).

    Args:
        query:          Search text (or regex pattern if regex=True)
        max_results:    Maximum matches to return (default 30)
        regex:          If True, treat query as a regex pattern
        case_sensitive: If True, match case-sensitively (default False)
    """
    if not query.strip():
        return "Empty query."

    cfg = _get_config()
    if not cfg.history_file or not cfg.history_file.is_file():
        return "No history file found."

    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        if re.search(r"(\+\s*\)|\*\s*\)|\}\s*\))\s*[+*]", query):
            return "Potentially unsafe regex - nested quantifiers detected."
    try:
        pattern = re.compile(query, flags) if regex else re.compile(
            re.escape(query), flags
        )
    except re.error as e:
        return f"Invalid regex: {e}"

    try:
        lines = cfg.history_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"Error: {e}"

    matches = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped and pattern.search(stripped):
            matches.append({"line": i, "text": stripped.strip('"')[:300]})

    if not matches:
        return "No matching commands found."
    capped = matches[:max_results]
    return (
        f"Found {len(capped)} match(es) for '{query}':\n"
        + "\n".join(f"  L{m['line']}: {m['text']}" for m in capped)
    )


@mcp.tool()
def search_log(
    query: str,
    max_results: int = 30,
    regex: bool = False,
    case_sensitive: bool = False,
    level: str | None = None,
) -> str:
    """Search the Vibe runtime log (vibe.log).

    Args:
        query:          Search text (or regex pattern if regex=True)
        max_results:    Maximum matches to return (default 30)
        regex:          If True, treat query as a regex pattern
        case_sensitive: If True, match case-sensitively (default False)
        level:          Filter by log level: WARNING, INFO, ERROR, DEBUG
    """
    if not query.strip():
        return "Empty query."

    cfg = _get_config()
    if not cfg.log_file or not cfg.log_file.is_file():
        return "No log file found."

    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        if re.search(r"(\+\s*\)|\*\s*\)|\}\s*\))\s*[+*]", query):
            return "Potentially unsafe regex - nested quantifiers detected."
    try:
        pattern = re.compile(query, flags) if regex else re.compile(
            re.escape(query), flags
        )
    except re.error as e:
        return f"Invalid regex: {e}"

    try:
        lines = cfg.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"Error: {e}"

    matches = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if level:
            parts = stripped.split()
            if len(parts) < 4 or parts[3].upper() != level.upper():
                continue
        if pattern.search(stripped):
            matches.append({"line": i, "text": stripped[:400]})

    if not matches:
        return f"No matching entries found for '{query}'."
    capped = matches[:max_results]
    return (
        f"Found {len(capped)} match(es) for '{query}':\n"
        + "\n".join(f"  L{m['line']}: {m['text']}" for m in capped)
    )


# ---------------------------------------------------------------------------
# FST search helpers
# ---------------------------------------------------------------------------


def _search_via_fst(cfg: DomainConfig, query: str, max_results: int) -> list[dict] | None:
    """Use the FST indexer for fast full-text search. Returns None if unavailable."""
    return search_fst(cfg, query, max_results)


def _format_search_results(
    fst_results: list[dict],
    domain: str,
    query: str,
    max_results: int,
    date_from: str | None,
    date_to: str | None,
    context_lines: int,
    max_matches_per_file: int,
    regex: bool,
    case_sensitive: bool,
    role: str | None,
    speaker: str | None,
    cwd: str | None = None,
) -> str:
    """Format FST search results with domain-specific post-filtering."""
    cfg = _get_config()
    d_from = date.fromisoformat(date_from) if date_from else None
    d_to = date.fromisoformat(date_to) if date_to else None

    if regex or case_sensitive:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            post_pat = (
                re.compile(query, flags)
                if regex
                else re.compile(re.escape(query), flags)
            )
        except re.error as e:
            return f"Invalid regex: {e}"
    else:
        post_pat = None

    all_matches: list[dict] = []

    # Group results by domain for file list resolution
    for r in fst_results:
        r_domain = r.get("_domain", domain)
        d_cfg = cfg.domains.get(r_domain)
        if d_cfg is None:
            continue

        file_idx = r.get("file_idx")
        entry_idx = r.get("entry_idx")
        if file_idx is None or entry_idx is None:
            continue

        # Map file_idx to filename
        idx_dir = d_cfg.effective_index_dir
        fname = resolve_file_idx(idx_dir, file_idx)
        if not fname:
            continue

        rdate_str = r.get("date", "?")

        # Date filter
        if rdate_str and rdate_str != "?":
            try:
                sd = date.fromisoformat(rdate_str)
                if d_from and sd < d_from:
                    continue
                if d_to and sd > d_to:
                    continue
            except ValueError:
                pass

        # Resolve file path
        if r_domain == "sessions":
            sess_dir = d_cfg.dir / fname
            if cwd and not _session_matches_cwd(sess_dir, cwd):
                continue
            file_path = sess_dir / "messages.jsonl"
        else:
            file_path = d_cfg.dir / fname

        if not file_path.exists():
            continue

        # --- Resolve entry_idx to actual content ---
        # Transcripts: entry_idx is a turn index (re-parse and index into turns)
        # Everything else: entry_idx is a 0-based line number
        if r_domain == "transcripts":
            try:
                parsed = parse_transcript_file(file_path)
            except OSError:
                continue
            turns = parsed.get("turns", [])
            if entry_idx < 0 or entry_idx >= len(turns):
                continue
            turn = turns[entry_idx]
            matched_line = turn.get("text", "")
            turn_speaker = turn.get("speaker", "")

            # Speaker filter (check actual turn speaker)
            if speaker and speaker.lower() not in turn_speaker.lower():
                continue

            # Context: first line of turn text
            turn_lines = matched_line.splitlines() if matched_line else []
            display = (turn_lines[0] if turn_lines else "")[:300]
            ctx_before = []
            ctx_after = turn_lines[1:1 + context_lines] if len(turn_lines) > 1 else []
        else:
            lineno = entry_idx + 1  # Convert to 1-based line number
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue

            if not lines or lineno < 1 or lineno > len(lines):
                continue

            matched_line = lines[lineno - 1].strip()

            if post_pat and not post_pat.search(matched_line):
                continue

            # Role filter (sessions only)
            if role and r_domain == "sessions":
                try:
                    msg = json.loads(matched_line)
                    if msg.get("role", "").lower() != role.lower():
                        continue
                except json.JSONDecodeError:
                    if role.lower() != "user":
                        continue

            ctx_before = lines[max(0, lineno - 1 - context_lines) : lineno - 1]
            ctx_after = lines[lineno : lineno + context_lines]
            display = _msg_snippet(matched_line)

        all_matches.append(
            {
                "file_id": fname,
                "source": r_domain,
                "date": rdate_str,
                "line": entry_idx if r_domain == "transcripts" else lineno,
                "match": display,
                "context_before": [l[:300] for l in ctx_before],
                "context_after": [l[:300] for l in ctx_after],
            }
        )

    if not all_matches:
        return f"No matching entries found for '{query}' in {domain}."

    # Per-file cap
    per_file: dict[str, int] = {}
    capped: list[dict] = []
    for m in all_matches:
        key = m["file_id"]
        n = per_file.get(key, 0)
        if n >= max_matches_per_file:
            continue
        per_file[key] = n + 1
        capped.append(m)

    capped.sort(key=lambda m: (m["date"], m["line"]))
    capped.sort(key=lambda m: m["date"], reverse=True)
    capped = capped[:max_results]

    return _render_matches(
        capped, domain, query, date_from, date_to, regex, role, speaker
    )


def _render_matches(
    matches: list[dict],
    domain: str,
    query: str,
    date_from: str | None,
    date_to: str | None,
    regex: bool,
    role: str | None,
    speaker: str | None,
) -> str:
    """Render search matches with summary snippets for each file."""
    cfg = _get_config()
    dr = ""
    if date_from:
        dr += f" from {date_from}"
    if date_to:
        dr += f" to {date_to}"
    extra = ""
    if regex:
        extra += " (regex)"
    if role:
        extra += f" [role: {role}]"
    if speaker:
        extra += f" [speaker: {speaker}]"

    domain_label = domain if domain != "all" else "all domains"
    output = [
        f"Found {len(matches)} match(es) for '{query}' in {domain_label}{extra}{dr}:"
    ]

    last_file = None
    for m in matches:
        if m["file_id"] != last_file:
            m_domain = m.get("source", domain)
            d_cfg = cfg.domains.get(m_domain)

            if d_cfg and m_domain == "sessions":
                file_path = d_cfg.dir / m["file_id"]
                summary_data = _load_session_summary(file_path)
                meta = _load_json(file_path / "meta.json") or {}
                title = (meta.get("title") or m["file_id"])[:80]
            elif d_cfg and m_domain == "transcripts":
                file_path = d_cfg.dir / m["file_id"]
                summary_data = _load_transcript_summary(file_path)
                title = m["file_id"]
            else:
                summary_data = None
                title = m["file_id"]

            header = f"\n--- {title} ({m['date']}) [{m['file_id']}] [{m_domain}] ---"
            if summary_data:
                goal_or_topic = summary_data.get("goal") or summary_data.get("topic")
                if goal_or_topic:
                    header += f"\n    {goal_or_topic}"
            output.append(header)
            last_file = m["file_id"]

        output.append(f"  line {m['line']}:")
        for ctx in m["context_before"]:
            output.append(f"    {ctx[:200]}")
        output.append(f"  > {m['match'][:200]}")
        for ctx in m["context_after"]:
            output.append(f"    {ctx[:200]}")

    return "\n".join(output)


# ---------------------------------------------------------------------------
# Summary formatters
# ---------------------------------------------------------------------------


def _format_session_summary(s: dict, name: str) -> str:
    out = [
        f"Summary for {name}:",
        f"  Generated: {s.get('generated_at', '?')}",
        f"  Model: {s.get('model', '?')}",
        "",
        f"  Goal: {s.get('goal', '?')}",
        f"  Outcome: {s.get('outcome', '?')}",
        f"  Status: {s.get('status', '?')}",
    ]
    if s.get("tags"):
        out.append(f"  Tags: {', '.join(s['tags'])}")
    if s.get("key_decisions"):
        out.append("  Key Decisions:")
        for d in s["key_decisions"]:
            out.append(f"    - {d}")
    if s.get("files_touched"):
        out.append("  Files Touched:")
        for f in s["files_touched"]:
            out.append(f"    - {f}")
    return "\n".join(out)


def _format_transcript_summary(s: dict, name: str) -> str:
    out = [
        f"Summary for {name}:",
        f"  Generated: {s.get('generated_at', '?')}",
        "",
        f"  Topic: {s.get('topic', '?')}",
    ]
    if s.get("key_points"):
        out.append("  Key Points:")
        for p in s["key_points"]:
            out.append(f"    - {p}")
    if s.get("decisions"):
        out.append("  Decisions:")
        for d in s["decisions"]:
            out.append(f"    - {d}")
    if s.get("action_items"):
        out.append("  Action Items:")
        for a in s["action_items"]:
            out.append(f"    - {a.get('who', '?')}: {a.get('what', '?')}")
    if s.get("tags"):
        out.append(f"  Tags: {', '.join(s['tags'])}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
