"""Renderers — format domain entries for human-readable display."""


def render_list_entry(domain_type: str, meta: dict) -> list[str]:
    """Return lines for list_domain output for one entry."""
    parts: list[str] = []
    if meta.get("title"):
        parts.append(f"  Title: {meta['title']}")

    if domain_type == "sessions":
        parts.append(f"  Started: {meta.get('started', '?')}")
        parts.append(f"  Messages: {meta.get('message_count', '?')}")
        if meta.get("tokens"):
            parts.append(f"  Tokens: {meta['tokens']}")
        if meta.get("cost"):
            parts.append(f"  Cost: ${meta['cost']:.4f}")

    elif domain_type == "transcripts":
        if meta.get("date"):
            parts.append(f"  Date: {meta['date']}")
        parts.append(
            f"  Turns: {meta.get('turns', '?')}, Duration: {meta.get('duration', '?')}"
        )
        participants = meta.get("participants", [])
        if participants:
            shown = participants[:5]
            label = ", ".join(shown)
            if len(participants) > 5:
                label += f" (+{len(participants) - 5})"
            parts.append(f"  Participants: {label}")

    elif domain_type == "notifications":
        parts.append(
            f"  Entries: {meta.get('entries', '?')}, Size: {meta.get('size_kb', '?')} KB"
        )

    elif domain_type == "web-archive":
        parts.append(
            f"  Entries: {meta.get('entries', '?')}, Size: {meta.get('size_kb', '?')} KB"
        )

    return parts


def render_read_entry(domain_type: str, entry: dict) -> list[str]:
    """Return lines for read output for one entry."""
    lines: list[str] = []

    if domain_type == "sessions":
        msg_role = entry.get("role", "?")
        msg_id = entry.get("message_id", "")[:8]
        content = entry.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        lines.append(f"\n--- {msg_role} [{msg_id}] ---")
        if content:
            for line_text in str(content).split("\n")[:10]:
                lines.append(f"  {line_text[:200]}")
        tcs = entry.get("tool_calls")
        if tcs:
            for tc in tcs:
                fn = tc.get("function", {})
                lines.append(f"  [tool: {fn.get('name', '?')}]")

    elif domain_type == "transcripts":
        speaker_name = entry.get("speaker", "?")
        ts = entry.get("timestamp", "?")
        text = entry.get("text", "")
        lines.append(f"\n  {speaker_name} ({ts}):")
        for line_text in text.split("\n")[:8]:
            lines.append(f"    {line_text[:200]}")

    elif domain_type == "notifications":
        ts = entry.get("timestamp", "?")
        app = entry.get("app", "?")
        summary_text = entry.get("summary", "")
        body = entry.get("body", "")
        lines.append(f"\n[{ts}] ({app})")
        if summary_text:
            lines.append(f"  Summary: {summary_text[:200]}")
        if body:
            lines.append(f"  Body: {body[:200]}")

    elif domain_type == "web-archive":
        ts = entry.get("timestamp", "?")
        title = entry.get("title", "")
        src = entry.get("source", "?")
        content = entry.get("content", "")
        lines.append(f"\n[{ts}] {title}")
        lines.append(f"  Source: {src}")
        if content:
            for line_text in str(content).split("\n")[:20]:
                lines.append(f"  {line_text[:300]}")

    return lines
