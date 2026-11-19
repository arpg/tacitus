"""Masking for text that leaves the machine's logs: the doctor's `--share`
report and the status light's log lines.

`share_mask` replaces addresses, the account names in home paths, URLs,
host names and long hexadecimal ids with fixed placeholders, so the shape
of a report survives and the identities do not. `redact` does that and
first masks credentials: the token shapes the secrets lint refuses in the
source (tests/secrets_lint.sh), a bearer value, and the value after a name
that reads as a secret (`token=`, `api_key:`, `"password": `). Standard
library only.
"""
from __future__ import annotations

import re

SHARE_RULES = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(/(?:home|Users)/)(?!ea-bot/)[^/\s'\"]+"), r"\1<user>"),
    (re.compile(r"https?://[^\s'\"]+"), "<url>"),
    (re.compile(r"\b[a-z0-9-]+\.(?:ts\.net|local|lan|edu|com|org|net)\b"), "<host>"),
    (re.compile(r"\b[0-9a-f]{24,}\b"), "<id>"),
]

# The credential shapes tests/secrets_lint.sh refuses in committed code,
# and a bearer value. Each is spelled so that the lint itself does not
# match this file (`[X]` is the letter X).
TOKEN_RULES = [
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]+"), "<secret>"),
    (re.compile(r"xapp-[A-Za-z0-9-]+"), "<secret>"),
    (re.compile(r"GOCSP[X]-[A-Za-z0-9_-]+"), "<secret>"),
    (re.compile(r"ya29\.[A-Za-z0-9._-]+"), "<secret>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)*"), "<secret>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<secret>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<secret>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "<secret>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <secret>"),
]
# A value after a name that reads as a secret: `token=...`, `"api_key": "..."`.
_SECRET_VALUE = re.compile(
    r"(?i)(\"?[A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d?|api[_-]?key|credential|private[_-]?key|"
    r"cookie|authorization)[A-Za-z0-9_.-]*\"?\s*[:=]\s*\"?)([^\s\",;}]+)")


def share_mask(text: str) -> str:
    """The text with addresses, home-path user names, URLs, host names and
    long hexadecimal ids replaced by placeholders."""
    for rx, rep in SHARE_RULES:
        text = rx.sub(rep, text)
    return text


def redact(text: str) -> str:
    """share_mask, after every credential shape and secret value is masked."""
    text = str(text or "")
    for rx, rep in TOKEN_RULES:
        text = rx.sub(rep, text)
    text = _SECRET_VALUE.sub(lambda m: m.group(1) + "<secret>", text)
    return share_mask(text)
