from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SENSITIVE = re.compile(r"(?i)(password|passwd|token|secret|api[-_]?key|authorization|credential)")
URI_CREDENTIAL = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@", re.I)


@dataclass(frozen=True, slots=True)
class RedactedCommand:
    text: str
    digest: str
    truncated: bool
    status: str


def _safe_truncate(value: str, max_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def redact_cmdline(raw: bytes, max_bytes: int = 4096) -> RedactedCommand:
    """Redact before truncation; never return or hash the original command."""
    try:
        args = [part.decode("utf-8", errors="strict") for part in raw.split(b"\0") if part]
        output: list[str] = []
        redacted = False
        hide_next = False
        for arg in args:
            if hide_next:
                output.append("[REDACTED]")
                redacted = True
                hide_next = False
                continue
            if arg.startswith("-") and "=" in arg:
                key, _ = arg.split("=", 1)
                if SENSITIVE.search(key):
                    output.append(f"{key}=[REDACTED]")
                    redacted = True
                    continue
            if arg.startswith("-") and SENSITIVE.search(arg):
                output.append(arg)
                hide_next = True
                continue
            cleaned, count = URI_CREDENTIAL.subn(r"\g<scheme>[REDACTED]@", arg)
            redacted |= count > 0
            output.append(cleaned)
        if hide_next:
            output.append("[REDACTED]")
            redacted = True
        normalized = " ".join(output)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        text, truncated = _safe_truncate(normalized, max_bytes)
        return RedactedCommand(text, digest, truncated, "redacted" if redacted else "no_sensitive_value")
    except Exception:
        fallback = "[REDACTION_FAILED]"
        return RedactedCommand(fallback, hashlib.sha256(fallback.encode()).hexdigest(), False, "failed_closed")
