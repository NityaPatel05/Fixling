"""Secret detection shared by the policy gate (candidate patches) and the pre-push scan (spec Part F)."""

from __future__ import annotations

import re
from collections.abc import Iterable

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    (
        "generic api key assignment",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['"][A-Za-z0-9_\-/+=]{20,}['"]"""),
    ),
]


def scan_secrets(text: str, extra_secrets: Iterable[str] = ()) -> list[str]:
    """Labels of secret-looking content in ``text`` (empty if clean). Never returns the secret itself."""
    found = [label for label, pat in SECRET_PATTERNS if pat.search(text)]
    found += [f"configured secret #{i + 1}" for i, s in enumerate(extra_secrets) if s and s in text]
    return found
