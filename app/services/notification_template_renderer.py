"""Template rendering helpers for notification channels.

ONE contract across editor, preview, test-send and the live event renderer:
SINGLE-brace ``{variable}`` placeholders, and only variables the live event
render context can actually supply (see ``KNOWN_PLACEHOLDERS``). Double-brace
``{{variable}}`` is rejected at save time — it is the syntax that previously
leaked literal ``{{amount}}`` to customers because the live renderer
(events/handlers/notification.py:_render_text) only fills single braces.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Single-brace only. Keep this aligned with the live renderer's contract.
_PLACEHOLDER_RE = re.compile(r"\{\s*([a-zA-Z0-9_]+)\s*\}")

# Catch double-brace tokens for save-time validation (not for rendering).
_DOUBLE_BRACE_RE = re.compile(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}")
# Single-brace placeholder names (ignoring any double-brace ones).
_SINGLE_NAME_RE = re.compile(r"(?<!\{)\{\s*([a-zA-Z0-9_]+)\s*\}(?!\})")

# Variables some render context can supply. Two contexts feed templates, so a
# placeholder is valid if EITHER can fill it:
#   * event-driven sends: events/handlers/notification.py:_build_render_context
#   * admin bulk message:  web_customer_actions.py:_notification_template_variables
# Editing this set is a code change — a context must actually produce the value.
# (A placeholder one context supplies but the other doesn't renders literally on
# the other path; that's why save-time validation blocks unknown names + double
# braces, which is what actually leaked to customers.)
_EVENT_VARIABLES: frozenset[str] = frozenset(
    {
        "subscriber_name",
        "offer_name",
        "plan_name",
        "invoice_number",
        "amount",
        "due_date",
        "portal_url",
        "device_serial",
        "location",
        "updated_fields",
        "service_order_id",
        "old_offer_name",
        "new_offer_name",
        "grace_hours",
        "usage_percent",
        "total_amount",
        "receipt_number",
        "receipt_url",
    }
)
_BULK_VARIABLES: frozenset[str] = frozenset(
    {
        "customer_name",
        "account_number",
        "subscriber_number",
        "email",
        "phone",
        "status",
        "pppoe_login",
        "ipv4_address",
        "nas_name",
        "location",
    }
)
KNOWN_PLACEHOLDERS: frozenset[str] = _EVENT_VARIABLES | _BULK_VARIABLES
PAYMENT_RECEIPT_TEMPLATE_CODE = "payment_received"
PAYMENT_RECEIPT_REQUIRED_PLACEHOLDERS = frozenset({"receipt_number", "receipt_url"})

# Customer-facing variables surfaced as chips in the editor, with sample values
# used for preview/test-send. (name, sample, description)
TEMPLATE_VARIABLES: tuple[tuple[str, str, str], ...] = (
    ("subscriber_name", "Jane Doe", "Customer's name"),
    ("invoice_number", "INV-2026-0001", "Invoice number"),
    ("amount", "₦12,500.00", "Amount (currency-formatted)"),
    ("due_date", "Mar 01, 2026", "Invoice due date"),
    ("offer_name", "Fibre 100Mbps", "Plan / offer name"),
    ("portal_url", "/portal", "Customer portal base URL"),
    ("usage_percent", "85", "Data usage percentage"),
    ("service_order_id", "SO-1042", "Service order reference"),
    ("receipt_number", "#RCP-1A2B3C4D", "Payment receipt reference"),
    (
        "receipt_url",
        "/portal/billing/payments/example/receipt",
        "Authorized receipt URL",
    ),
)

# Sample values for every KNOWN placeholder so previews never show blanks.
_PREVIEW_SAMPLES: dict[str, str] = {
    "subscriber_name": "Jane Doe",
    "offer_name": "Fibre 100Mbps",
    "plan_name": "Fibre 100Mbps",
    "invoice_number": "INV-2026-0001",
    "amount": "₦12,500.00",
    "due_date": "Mar 01, 2026",
    "portal_url": "/portal",
    "device_serial": "HWTC12345678",
    "location": "Lekki Phase 1",
    "updated_fields": "email, phone",
    "service_order_id": "SO-1042",
    "old_offer_name": "Fibre 50Mbps",
    "new_offer_name": "Fibre 100Mbps",
    "grace_hours": "48",
    "usage_percent": "85",
    "total_amount": "₦20,000.00",
    "receipt_number": "#RCP-1A2B3C4D",
    "receipt_url": "/portal/billing/payments/example/receipt",
}


def render_template_text(
    text: str | None, variables: Mapping[str, object] | None = None
) -> str:
    """Render single-brace ``{variable}`` placeholders in text.

    Mirrors the live event renderer: known placeholders are substituted,
    unknown ones are left unchanged (so mistakes are visible, not silently
    blanked). Double-brace tokens are NOT a supported syntax.
    """
    if not text:
        return ""
    values = {
        str(key): "" if value is None else str(value)
        for key, value in (variables or {}).items()
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values[key] if key in values else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def default_preview_variables() -> dict[str, str]:
    """Sample values for template previews/tests (single-brace contract)."""
    return dict(_PREVIEW_SAMPLES)


def template_placeholder_names(*texts: str | None) -> frozenset[str]:
    """Return the supported single-brace names present in template text."""

    return frozenset(
        name for text in texts if text for name in _SINGLE_NAME_RE.findall(text)
    )


def automated_template_codes() -> frozenset[str]:
    """Codes sent by the automated event path (events/handlers/notification.py).

    A template with one of these codes is delivered by the event sender, whose
    render context only supplies ``_EVENT_VARIABLES``. Anything else is reached
    only by the admin bulk-message path (``_BULK_VARIABLES``). Imported lazily to
    avoid a module import cycle.
    """
    try:
        from app.services.events.handlers.notification import (
            EVENT_NOTIFICATION_SPECS,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not load event notification specs", exc_info=True)
        return frozenset()
    return frozenset(spec.template_code for spec in EVENT_NOTIFICATION_SPECS.values())


def allowed_variables_for_code(code: str | None) -> tuple[frozenset[str], str]:
    """Return (allowed placeholder names, human context label) for a code.

    Automated event codes are validated against the event context only; every
    other code is reachable only via bulk message, so it is validated against
    the bulk context only. This is what prevents an automated template from
    saving a bulk-only ``{customer_name}`` that the event sender leaves literal.
    """
    if code and code in automated_template_codes():
        return _EVENT_VARIABLES, "automated (event-driven)"
    return _BULK_VARIABLES, "bulk message"


def validate_template_text(*texts: str | None, code: str | None = None) -> None:
    """Reject unsafe placeholder syntax at save time.

    Raises ``ValueError`` if any text contains double-brace ``{{var}}`` tokens or
    single-brace placeholders the template's actual send context cannot supply
    (event context for automated codes, bulk context otherwise).
    """
    allowed, context_label = allowed_variables_for_code(code)
    double: set[str] = set()
    unknown: set[str] = set()
    for text in texts:
        if not text:
            continue
        double.update(_DOUBLE_BRACE_RE.findall(text))
        for name in _SINGLE_NAME_RE.findall(text):
            if name not in allowed:
                unknown.add(name)
    if not double and not unknown:
        return
    problems: list[str] = []
    if double:
        problems.append(
            "double braces are not supported — use single braces like "
            "{subscriber_name}: " + ", ".join(sorted(double))
        )
    if unknown:
        problems.append(
            f"variables not available to {context_label} templates "
            "(they would be left blank/literal): "
            + ", ".join("{" + n + "}" for n in sorted(unknown))
        )
    allowed_list = ", ".join("{" + n + "}" for n in sorted(allowed))
    raise ValueError(
        "Template placeholder problem(s): "
        + "; ".join(problems)
        + f". Allowed for {context_label} templates: {allowed_list}."
    )


def validate_template_activation_text(
    *,
    subject: str | None,
    body: str,
    code: str,
) -> None:
    """Validate the complete contract required before a template is active."""

    validate_template_text(subject, body, code=code)
    if code != PAYMENT_RECEIPT_TEMPLATE_CODE:
        return
    body_placeholders = template_placeholder_names(body)
    missing = PAYMENT_RECEIPT_REQUIRED_PLACEHOLDERS - body_placeholders
    if missing:
        formatted = ", ".join("{" + name + "}" for name in sorted(missing))
        raise ValueError(
            "Active payment receipt templates require these body fields: " + formatted
        )
