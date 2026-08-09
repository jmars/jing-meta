"""Extractors — produce text entries for FST indexing.

Each extractor takes raw file text and returns a list of entry strings.
"""

import json


def extract_jsonl(text: str) -> list[str]:
    """Extract content from JSONL. For each line, returns the 'content' field if JSON, else the line."""
    entries: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
            content = msg.get("content", "")
            if content:
                entries.append(str(content)[:1000])
                continue
            tcs = msg.get("tool_calls", [])
            if tcs:
                parts = []
                for tc in tcs:
                    fn = tc.get("function", {})
                    parts.append(f"{fn.get('name', '?')}(...)")
                entries.append(" | ".join(parts))
                continue
            entries.append("(no content)")
        except json.JSONDecodeError:
            entries.append(stripped[:1000])
    return entries


def extract_txt(text: str) -> list[str]:
    """Each non-empty line is one entry."""
    return [line for line in text.splitlines() if line.strip()]


def extract_transcript(text: str) -> list[str]:
    """Parse Tactiq transcript, return one entry per turn: 'speaker: text'."""
    from .transcript import parse_transcript

    parsed = parse_transcript(text)
    entries: list[str] = []
    for turn in parsed.get("turns", []):
        speaker = turn.get("speaker", "?")
        turn_text = turn.get("text", "")
        entries.append(f"{speaker}: {turn_text[:2000]}")
    return entries


def extract_notification(text: str) -> list[str]:
    """For each JSONL line, extract summary + body."""
    entries: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
            parts: list[str] = []
            summary = msg.get("summary", "")
            if summary:
                parts.append(str(summary))
            body = msg.get("body", "")
            if body:
                parts.append(str(body))
            if not parts:
                parts.append(str(msg))
            entries.append(" | ".join(parts)[:2000])
        except json.JSONDecodeError:
            entries.append(stripped[:2000])
    return entries
