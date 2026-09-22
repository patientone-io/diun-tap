import re
import html
from typing import Optional


def format_markdown_for_telegram(text: Optional[str], max_length: int = 1500) -> str:
    """
    Cleans and formats GitHub release notes Markdown into Telegram-compatible HTML,
    wrapped in an expandable blockquote (<blockquote expandable>).
    """
    if not text or not text.strip():
        return ""

    raw = text.strip()

    # 1. Truncate if excessively long before processing
    truncated = False
    if len(raw) > max_length:
        raw = raw[:max_length]
        truncated = True

    lines = raw.splitlines()
    formatted_lines = []

    for line in lines:
        stripped = line.strip()

        # Skip raw HTML comment tags <!-- ... -->
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue

        # Skip horizontal rules
        if re.match(r"^[-*_]{3,}$", stripped):
            continue

        # Convert markdown headers: ## Header or ### Header -> <b>Header</b>
        header_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if header_match:
            header_text = html.escape(header_match.group(1).strip())
            formatted_lines.append(f"\n<b>{header_text}</b>")
            continue

        # Escape HTML characters first for normal lines
        escaped_line = html.escape(stripped)

        # Convert bullet points: '* ' or '- ' at beginning of line -> '• '
        if re.match(r"^[*+-]\s+", escaped_line):
            escaped_line = re.sub(r"^[*+-]\s+", "• ", escaped_line)

        # Convert markdown links: [label](url) -> <a href="url">label</a>
        # Note: input was html escaped, so url might have &amp;
        escaped_line = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            r'<a href="\2">\1</a>',
            escaped_line
        )

        # Simplify PR links like: in https://github.com/.../pull/123 -> in <a href="...">#123</a>
        escaped_line = re.sub(
            r'in\s+(https?://github\.com/[^/\s]+/[^/\s]+/pull/(\d+))',
            r'in <a href="\1">#\2</a>',
            escaped_line
        )

        # Convert bold: **text** or __text__ -> <b>text</b>
        escaped_line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped_line)
        escaped_line = re.sub(r"__([^_]+)__", r"<b>\1</b>", escaped_line)

        # Convert inline code: `code` -> <code>code</code>
        escaped_line = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped_line)

        formatted_lines.append(escaped_line)

    result = "\n".join(formatted_lines).strip()

    # Consolidate multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)

    if truncated:
        result += "\n\n<i>…(truncated, click header link for full release notes)</i>"

    return f"<blockquote expandable>{result}</blockquote>"
