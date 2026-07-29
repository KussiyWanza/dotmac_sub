"""Authoritative identity normalization helpers."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

IDENTITY_TYPE_EMAIL = "email"
IDENTITY_TYPE_PHONE = "phone"
IDENTITY_TYPE_WHATSAPP = "whatsapp"
IDENTITY_TYPE_SMS = "sms"

PHONE_LIKE_HINTS = {
    IDENTITY_TYPE_PHONE,
    IDENTITY_TYPE_WHATSAPP,
    IDENTITY_TYPE_SMS,
    "chat",
    "tel",
}
DEFAULT_COUNTRY_CODE = "234"
NAME_PLACEHOLDER_TOKENS = {
    "blank",
    "customer",
    "empty",
    "na",
    "n/a",
    "none",
    "null",
    "test",
    "unknown",
}
NAME_PLACEHOLDER_MARKERS = {
    "",
    "-",
    "--",
    "---",
    ".",
    "..",
    "...",
    "anonymous",
    "customer",
    "customer customer",
    "customer unknown",
    "empty",
    "missing",
    "na",
    "n/a",
    "none",
    "null",
    "unknown",
    "unknown customer",
    "unknown unknown",
}
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")

_GENERIC_CUSTOMER_NAME_MARKERS = {
    "n/a",
    "na",
    "nil",
    "none",
    "not applicable",
    "not available",
    "not specified",
    "null",
}
_GENERIC_CUSTOMER_NAME_TOKENS = {"customer", "unknown"}


def normalize_customer_name(value: str | None) -> str:
    """Return a comparison-safe customer name without inventing identity."""

    return " ".join(str(value or "").strip().casefold().split())


def is_placeholder_customer_name(value: str | None) -> bool:
    """Identify CRM's known generic names after conservative normalization."""

    normalized = normalize_customer_name(value)
    if not normalized or normalized in _GENERIC_CUSTOMER_NAME_MARKERS:
        return True
    tokens = set(normalized.split())
    return bool(tokens) and tokens <= _GENERIC_CUSTOMER_NAME_TOKENS


def default_country_code(db=None) -> str:
    if db is None:
        return DEFAULT_COUNTRY_CODE
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        value = resolve_value(db, SettingDomain.subscriber, "default_country_code")
    except Exception:
        value = None
    normalized = re.sub(r"\D", "", str(value or "").strip())
    return normalized or DEFAULT_COUNTRY_CODE


def normalize_email_identifier(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def collapse_whitespace(value: str | None) -> str | None:
    text = str(value or "").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_name_text(value: str | None) -> str | None:
    text = collapse_whitespace(value)
    return text.casefold() if text else None


def is_placeholder_name(value: str | None) -> bool:
    normalized = normalize_name_text(value)
    if normalized is None:
        return True
    if normalized in NAME_PLACEHOLDER_MARKERS:
        return True
    tokens = _NAME_TOKEN_RE.findall(normalized)
    if not tokens:
        return True
    return all(token in NAME_PLACEHOLDER_TOKENS for token in tokens)


def customer_name_signature(
    first_name: str | None,
    last_name: str | None,
    display_name: str | None = None,
) -> str | None:
    display = normalize_name_text(display_name)
    if display:
        return display
    parts = [
        part
        for part in (
            normalize_name_text(first_name),
            normalize_name_text(last_name),
        )
        if part
    ]
    return " ".join(parts) or None


def customer_name_fingerprint(
    *,
    first_name: str | None,
    last_name: str | None,
    display_name: str | None = None,
    party_id: Any = None,
) -> str:
    payload = {
        "display_name": normalize_name_text(display_name),
        "first_name": normalize_name_text(first_name),
        "last_name": normalize_name_text(last_name),
        "party_id": str(party_id) if party_id else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_phone_identifier(
    value: str | None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    raw = raw.replace("\u00a0", " ")
    lowered = raw.lower()
    for prefix in ("whatsapp:", "sms:", "tel:"):
        if lowered.startswith(prefix):
            raw = raw.split(":", 1)[1].strip()
            lowered = raw.lower()
            break

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith(default_country_code):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 10:
        return f"+{default_country_code}{digits[1:]}"
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    return f"+{digits}"


def normalize_identifier(
    value: str | None,
    hint: str | None = None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    text = str(value or "")
    normalized_hint = str(hint or "").strip().lower()
    if normalized_hint == IDENTITY_TYPE_EMAIL or "@" in text:
        return normalize_email_identifier(text)
    return normalize_phone_identifier(text, default_country_code=default_country_code)


def normalize_channel_address(
    channel_type: str | None,
    value: str | None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    normalized_channel = str(channel_type or "").strip().lower()
    if normalized_channel == IDENTITY_TYPE_EMAIL:
        return normalize_email_identifier(value)
    if normalized_channel in PHONE_LIKE_HINTS:
        return normalize_phone_identifier(
            value, default_country_code=default_country_code
        )
    return normalize_identifier(
        value, normalized_channel, default_country_code=default_country_code
    )
