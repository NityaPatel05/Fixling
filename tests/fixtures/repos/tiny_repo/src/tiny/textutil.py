"""Small text helpers."""

import re


def slugify(text: str) -> str:
    """Lowercase ``text`` and join its alphanumeric words with single dashes."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)
