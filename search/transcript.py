"""Transcript/docx parsing — ported from the original unified-history-mcp-server.py."""

import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
SPEAKER_HEADER_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<ts>\d{2}:\d{2}:\d{2})\)\s*$")


def _extract_docx_text(p: Path) -> str:
    """Extract plain text from a Tactiq-synced .docx transcript.

    Note: ET.parse is used on user-owned .docx files from a trusted local
    sync directory. Not exposed to untrusted network input.
    """
    try:
        with ZipFile(p) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
    except (OSError, ET.ParseError, KeyError, BadZipFile):
        return ""
    paras = []
    for para in tree.iterfind(".//w:p", _DOCX_NS):
        texts = []
        for t in para.iterfind(".//w:t", _DOCX_NS):
            if t.text:
                texts.append(t.text)
        paras.append("".join(texts))
    return "\n".join(paras)


def _docx_to_standard_txt(p: Path) -> str:
    """Convert a Tactiq .docx transcript to the standard .txt header+turn format."""
    raw = _extract_docx_text(p)
    if not raw:
        return ""

    lines = raw.split("\n")

    # Derive meeting name and date from filename
    stem = p.stem  # e.g. "fixed sales MSA tech catchup - 2026-07-27T08-39-57"
    meeting_name = stem
    meeting_date_str = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})", stem)
    if m:
        meeting_date_str = f"{m.group(1)} {m.group(2)}:{m.group(3)}"
        # Strip timestamp suffix from meeting name
        meeting_name = re.sub(r"\s*-\s*\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d+$", "", stem).strip()

    # Find the transcript body (after a standalone "Transcript" heading line)
    transcript_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^Transcript$", stripped, re.IGNORECASE):
            transcript_start = i + 1
            break

    if transcript_start is None:
        transcript_start = 0

    body = lines[transcript_start:]
    # Clean blank lines at the start
    while body and not body[0].strip():
        body = body[1:]

    # Convert speaker format: "00:00 Phil Miller:" or "00:00 Phil Miller: text"
    # to "Phil Miller (00:00:00)" on its own line, text on following lines
    turn_re = re.compile(r"^(\d{2}:\d{2})\s+(.+?):(?:\s*(.*))?$")
    out_lines: list[str] = []
    header_written = False
    participants: set[str] = set()

    for line in body:
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        tm = turn_re.match(stripped)
        if tm:
            hhmm = tm.group(1)
            speaker = tm.group(2).strip()
            rest = tm.group(3) or ""
            participants.add(speaker)
            if not header_written:
                out_lines.append(f"Meeting: {meeting_name}")
                out_lines.append(f"Date: {meeting_date_str}")
                out_lines.append("Duration: ?")
                out_lines.append("Platform: MS_TEAMS")
                out_lines.append(f"Participants: {', '.join(sorted(participants))}")
                out_lines.append("=" * 60)
                header_written = True
            out_lines.append("")
            out_lines.append(f"{speaker} ({hhmm}:00)")
            if rest:
                out_lines.append(rest)
        else:
            out_lines.append(stripped)

    # Update participants list in header (now that we've collected them all)
    if participants:
        for i, line in enumerate(out_lines):
            if line.startswith("Participants:"):
                out_lines[i] = f"Participants: {', '.join(sorted(participants))}"
                break

    return "\n".join(out_lines)


def read_transcript_text(p: Path) -> str:
    """Read text content from a transcript file (.txt or .docx)."""
    if p.suffix.lower() == ".docx":
        return _docx_to_standard_txt(p)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_transcript(text: str) -> dict:
    """Parse a Tactiq TXT transcript into structured data."""
    lines = text.split("\n")
    result = {
        "meeting": None, "meeting_date": None, "duration": None,
        "platform": None, "participants": [], "turns": [], "raw_lines": lines,
    }
    header_end = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("==="):
            header_end = i
            break
    if header_end >= 0:
        for line in lines[:header_end]:
            line = line.strip()
            if line.startswith("Meeting:"):
                result["meeting"] = line[len("Meeting:"):].strip()
            elif line.startswith("Date:") and not result["meeting_date"]:
                ds = line[len("Date:"):].strip()
                try:
                    result["meeting_date"] = datetime.strptime(ds, "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            elif line.startswith("Duration:"):
                result["duration"] = line[len("Duration:"):].strip()
            elif line.startswith("Platform:"):
                result["platform"] = line[len("Platform:"):].strip()
            elif line.startswith("Participants:"):
                result["participants"] = [p.strip() for p in line[len("Participants:"):].strip().split(",") if p.strip()]
    current_speaker = current_ts = None
    current_text: list[str] = []
    current_line_start = 0
    for i in range(header_end + 1, len(lines)):
        line = lines[i]
        m = SPEAKER_HEADER_RE.match(line.strip())
        if m:
            if current_speaker is not None:
                result["turns"].append({
                    "speaker": current_speaker,
                    "timestamp": current_ts,
                    "text": "\n".join(current_text).strip(),
                    "line_start": current_line_start,
                    "line_end": i - 1,
                })
            current_speaker = m.group("name")
            current_ts = m.group("ts")
            current_text = []
            current_line_start = i
        elif current_speaker is not None:
            current_text.append(line)
    if current_speaker is not None:
        result["turns"].append({
            "speaker": current_speaker,
            "timestamp": current_ts,
            "text": "\n".join(current_text).strip(),
            "line_start": current_line_start,
            "line_end": len(lines) - 1,
        })
    return result


@lru_cache(maxsize=128)
def _parse_transcript_cached(path_str: str, mtime_ns: int) -> dict:
    return parse_transcript(read_transcript_text(Path(path_str)))


def parse_transcript_file(p: Path) -> dict:
    """Parse a transcript file with caching."""
    cached = _parse_transcript_cached(str(p), p.stat().st_mtime_ns)
    return {
        **cached,
        "participants": list(cached["participants"]),
        "turns": [dict(t) for t in cached["turns"]],
        "raw_lines": list(cached["raw_lines"]),
    }
