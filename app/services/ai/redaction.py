"""Best-effort PII redaction for text sent to an external LLM provider.

Deliberately simple and predictable: this is a coarse guard against the
obvious identifiers leaving the estate, not a full PII scrubber. Ported
verbatim from dotmac_crm.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_TOKEN_RE = re.compile(r"\b(?:sk|api|token|key)[A-Za-z0-9_-]{6,}\b", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(password|passcode|pin|otp|auth(?:entication)?[-_ ]?token|api[-_ ]?key)"
    r"\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)


def redact_text(value: str, *, max_chars: int = 1200) -> str:
    # Keep it simple and predictable. This is not a full PII scrubber.
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1: [redacted-secret]", value or "")
    redacted = _EMAIL_RE.sub("[redacted-email]", redacted)
    redacted = _PHONE_RE.sub("[redacted-phone]", redacted)
    redacted = _TOKEN_RE.sub("[redacted-token]", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[: max(100, max_chars)]
