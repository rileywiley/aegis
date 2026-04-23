"""Aegis — AI Chief of Staff."""


def safe_preview(text: str | None, max_len: int = 80) -> str:
    """Truncate text for safe logging — no PII leakage of full content.

    Returns a truncated version of the text suitable for log messages.
    Replaces newlines with spaces and appends '...' if truncated.
    Returns '<empty>' for None or empty strings.
    """
    if not text:
        return "<empty>"
    cleaned = text.replace("\n", " ").replace("\r", "").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."
