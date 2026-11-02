"""Sanitizer for untrusted issue text (spec E4).

Removes content that is invisible to a human reviewer but visible to a model (Unicode tag characters,
zero-width and bidi-control characters, HTML comments), neutralises markdown images, and caps the length.
Whatever is removed is reported: hidden content in an issue is a prompt-injection signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CHARS = 6000
_HEAD_CHARS = 4000


def _char_class(*ranges: tuple[int, int]) -> str:
    """Regex character class from code point ranges (keeps invisible characters out of the source)."""
    return "[" + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in ranges) + "]"


_TAG_RANGE = re.compile(_char_class((0xE0000, 0xE007F)))
_ZERO_WIDTH = re.compile(_char_class((0x200B, 0x200F), (0x2060, 0x2060), (0xFEFF, 0xFEFF)))
_BIDI = re.compile(_char_class((0x202A, 0x202E), (0x2066, 0x2069)))
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MANY_BLANKS = re.compile(r"\n{4,}")


@dataclass
class SanitizeReport:
    text: str
    removed: dict[str, int] = field(default_factory=dict)
    hidden_text: list[str] = field(default_factory=list)  # decoded tag-char messages / comment bodies
    truncated: bool = False

    @property
    def suspicious(self) -> bool:
        """True when invisible content was stripped (classic prompt-injection carrier)."""
        return any(self.removed.get(k, 0) for k in ("unicode_tags", "html_comments", "bidi", "zero_width"))


def _decode_tags(match_text: str) -> str:
    """Unicode tag characters mirror ASCII at U+E0000 + ord(c); decode them for the audit trail."""
    return "".join(chr(ord(c) - 0xE0000) for c in match_text if 0xE0020 <= ord(c) <= 0xE007E)


def sanitize_report(text: str) -> SanitizeReport:
    removed: dict[str, int] = {}
    hidden: list[str] = []

    def bump(key: str, n: int = 1) -> None:
        if n:
            removed[key] = removed.get(key, 0) + n

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    def comment(m: re.Match[str]) -> str:
        bump("html_comments")
        hidden.append(m.group(0)[:500])
        return ""

    text = _HTML_COMMENT.sub(comment, text)

    tags = _TAG_RANGE.findall(text)
    if tags:
        bump("unicode_tags", len(tags))
        decoded = _decode_tags("".join(tags))
        if decoded:
            hidden.append(decoded[:500])
        text = _TAG_RANGE.sub("", text)

    for key, pat in (("zero_width", _ZERO_WIDTH), ("bidi", _BIDI), ("control", _CONTROL)):
        found = pat.findall(text)
        if found:
            bump(key, len(found))
            text = pat.sub("", text)

    def image(_: re.Match[str]) -> str:
        bump("images")
        return "[image removed]"

    text = _MD_IMAGE.sub(image, text)
    text = _MANY_BLANKS.sub("\n\n\n", text).strip()

    truncated = False
    if len(text) > MAX_CHARS:
        truncated = True
        tail = MAX_CHARS - _HEAD_CHARS
        text = f"{text[:_HEAD_CHARS]}\n[... {len(text) - MAX_CHARS} chars omitted ...]\n{text[-tail:]}"
        bump("truncated_chars", 1)
    return SanitizeReport(text=text, removed=removed, hidden_text=hidden, truncated=truncated)


def sanitize(text: str) -> str:
    return sanitize_report(text).text
