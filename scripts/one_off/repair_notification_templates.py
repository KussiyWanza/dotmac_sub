#!/usr/bin/env python3
"""Repair bad notification_templates rows through their owning service.

Background
----------
Three template sources disagreed on placeholder syntax. The standalone seeder
``scripts/seed/seed_notification_templates.py`` wrote rows using DOUBLE-brace
``{{var}}`` plus variables the live renderers never produce (``payment_link``,
``company_name``, ``customer_name``, ``days_overdue``, ``short_link`` ...). The
live event renderer (``events/handlers/notification.py:_render_text``) only
fills SINGLE-brace ``{var}`` and only for variables the render context supplies,
so those rows went out with literal ``{{amount}}`` / blank placeholders and
alarming all-caps debt copy.

This one-off normalizes the STORED rows (the data is the problem, not the
renderer):

* Known billing / collections / device codes are replaced wholesale with
  approved, softened, single-brace copy (``APPROVED`` below).
* Any other row is mechanically normalized: supported ``{{x}}`` -> ``{x}``,
  UNSUPPORTED placeholders are removed (never left blank/literal), and a few
  alarming phrases are softened.
* Orphan rows are deactivated through the notification owner, not physically
  deleted.

Safe by default: prints a dry-run diff. Pass ``--apply`` to write + commit.

Usage (inside the app container)::

    python scripts/one_off/repair_notification_templates.py            # dry-run
    python scripts/one_off/repair_notification_templates.py --apply    # write
    python scripts/one_off/repair_notification_templates.py --code dunning_notice
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models.notification import NotificationTemplate  # noqa: E402
from app.schemas.notification import NotificationTemplateUpdate  # noqa: E402
from app.services import notification as notification_service  # noqa: E402
from app.services.notification_template_renderer import (  # noqa: E402
    KNOWN_PLACEHOLDERS,
)

# A placeholder is kept (converted {{x}}->{x}) if SOME send context can supply
# it; only genuinely-unknown names (payment_link, company_name, days_overdue,
# short_link ...) are stripped. Context-strict validity (event vs bulk) is
# enforced at save time by validate_template_text and reported by the audit
# script; this one-off only removes the definitely-broken syntax/variables.
SUPPORTED = set(KNOWN_PLACEHOLDERS)

# Orphan debt/billing templates from the standalone script that have NO event
# mapping — they are reachable only via admin bulk-send, whose context cannot
# supply {amount}/{invoice_number}, so they can never render a correct balance.
# They duplicate the automated invoice_overdue / suspension_warning / payment
# flows and were the bulk-blast vector for the incident. Disposition: deactivate.
DELETE_CODES: frozenset[str] = frozenset(
    {
        "invoice_issued",
        "invoice_due_7d",
        "invoice_due_1d",
        "invoice_due_1d_sms",
        "invoice_overdue_sms",
        "dunning_notice",
        "dunning_notice_sms",
        "suspension_warning_sms",
        "service_suspended",
        "service_suspended_sms",
    }
)

# Approved, softened, single-brace copy — ONLY for AUTOMATED event codes, whose
# event render context supplies these variables. Replaces the stored
# subject+body wholesale. Keyed by (code, channel).
APPROVED: dict[tuple[str, str], dict[str, str | None]] = {
    ("invoice_overdue", "email"): {
        "subject": "Invoice #{invoice_number} is now due",
        "body": (
            "Dear {subscriber_name},\n\n"
            "Our records show invoice #{invoice_number} for {amount} is now past "
            "its due date of {due_date}.\n\n"
            "If you've already paid, please disregard this message — thank you. "
            "Otherwise you can pay anytime from your account: {portal_url}/billing\n\n"
            "If you have any questions or would like to discuss payment options, "
            "please contact our support team."
        ),
    },
    ("invoice_overdue", "sms"): {
        "subject": None,
        "body": (
            "Invoice #{invoice_number} for {amount} is now past due. If you've "
            "already paid, please ignore this. Pay anytime: {portal_url}/billing"
        ),
    },
    ("payment_received", "email"): {
        "subject": "Payment receipt {receipt_number}",
        "body": (
            "Dear {subscriber_name},\n\n"
            "We have received your payment of {amount}. Thank you!\n\n"
            "Receipt: {receipt_number}\n"
            "View or download: {receipt_url}\n\n"
            "You can review how the payment was applied in your billing "
            "history.\n\n"
            "If you have questions about your billing, please contact support."
        ),
    },
    ("payment_received", "sms"): {
        "subject": None,
        "body": (
            "We received your payment of {amount}. Receipt {receipt_number}: "
            "{receipt_url}"
        ),
    },
    ("suspension_warning", "email"): {
        "subject": "A reminder about invoice #{invoice_number}",
        "body": (
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} for {amount} is currently unpaid. To "
            "avoid any interruption to your service, please arrange payment when "
            "you can.\n\n"
            "Pay anytime: {portal_url}/billing\n\n"
            "If you've already paid, please disregard this message. For any "
            "questions, please contact our support team."
        ),
    },
    ("suspension_warning", "sms"): {
        "subject": None,
        "body": (
            "Hi {subscriber_name}, invoice #{invoice_number} for {amount} is "
            "unpaid. Please pay to avoid any service interruption: "
            "{portal_url}/billing"
        ),
    },
    # Device / NOC alerts: customers should never see raw serials or internal
    # "investigate" copy. Generic, reassuring, no {device_serial}.
    ("ont_offline", "email"): {
        "subject": "We've noticed an issue with your connection",
        "body": (
            "Dear {subscriber_name},\n\n"
            "We've detected that your service may currently be offline, and our "
            "team is looking into it.\n\n"
            "If your equipment has lost power, please check that it is switched "
            "on. If the issue continues, please contact our support team."
        ),
    },
    ("ont_online", "email"): {
        "subject": "Your connection is back online",
        "body": (
            "Dear {subscriber_name},\n\n"
            "Good news — your service is back online. Thank you for your "
            "patience.\n\n"
            "If you continue to experience any issues, please contact our "
            "support team."
        ),
    },
    ("ont_signal_degraded", "email"): {
        "subject": "We're checking on your connection quality",
        "body": (
            "Dear {subscriber_name},\n\n"
            "Our monitoring suggests your connection quality may be reduced, and "
            "our team is looking into it. There is nothing you need to do right "
            "now.\n\n"
            "If you notice any problems, please contact our support team."
        ),
    },
}

# Light softening for rows NOT covered by APPROVED (orphan/custom codes).
SOFTEN = [
    (re.compile(r"\bURGENT\b:?\s*", re.I), ""),
    (re.compile(r"\bOVERDUE\b:?\s*", re.I), ""),
    (re.compile(r"immediate payment required", re.I), "payment due"),
    (re.compile(r"immediately", re.I), "as soon as you can"),
    (re.compile(r"WILL BE SUSPENDED", re.I), "may be suspended"),
    (re.compile(r"ACTION REQUIRED:?\s*", re.I), ""),
    (re.compile(r"WARNING:?\s*", re.I), ""),
]

DOUBLE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
ANY_PH_RE = re.compile(r"\{\{?\s*([\w.]+)\s*\}?\}")


def normalize(text: str | None) -> str | None:
    """Convert supported {{x}}->{x}; drop unsupported placeholders; soften."""
    if not text:
        return text
    # supported double -> single
    text = DOUBLE_RE.sub(
        lambda m: "{" + m.group(1) + "}" if m.group(1) in SUPPORTED else m.group(0),
        text,
    )
    # drop any remaining unsupported placeholders (double or single)
    text = ANY_PH_RE.sub(
        lambda m: m.group(0) if m.group(1) in SUPPORTED else "",
        text,
    )
    for pat, repl in SOFTEN:
        text = pat.sub(repl, text)
    # tidy whitespace left by removals
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def channel_of(t: NotificationTemplate) -> str:
    return getattr(t.channel, "value", str(t.channel))


def plan(t: NotificationTemplate) -> tuple[str | None, str | None]:
    """Return the (subject, body) we want this row to have, or (sentinel) None."""
    key = (t.code, channel_of(t))
    if key in APPROVED:
        return APPROVED[key]["subject"], APPROVED[key]["body"]
    return normalize(t.subject), normalize(t.body)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    ap.add_argument("--code", help="restrict to a single template code")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(NotificationTemplate)
        if args.code:
            q = q.filter(NotificationTemplate.code == args.code)
        rows = q.order_by(NotificationTemplate.code, NotificationTemplate.channel).all()

        changed = 0
        deactivated = 0
        for t in rows:
            chan = channel_of(t)
            # Disposition 1: deactivate orphan debt templates with no event mapping.
            if t.code in DELETE_CODES:
                if not t.is_active:
                    continue
                deactivated += 1
                print(
                    f"\n=== {t.code} [{chan}] (DEACTIVATE — orphan debt template) ==="
                )
                print(f"  subject: {t.subject!r}")
                if args.apply:
                    notification_service.templates.delete(db=db, template_id=str(t.id))
                continue

            new_subject, new_body = plan(t)
            if new_subject == t.subject and new_body == t.body:
                continue
            changed += 1
            key = (t.code, chan)
            tag = "APPROVED-COPY" if key in APPROVED else "normalized"
            print(f"\n=== {t.code} [{chan}] ({tag}) ===")
            if new_subject != t.subject:
                print(f"  subject: {t.subject!r}\n        -> {new_subject!r}")
            if new_body != t.body:
                print("  body:")
                print("    --- before ---")
                print("    " + (t.body or "").replace("\n", "\n    "))
                print("    --- after ----")
                print("    " + (new_body or "").replace("\n", "\n    "))
            if args.apply:
                notification_service.templates.update(
                    db=db,
                    template_id=str(t.id),
                    payload=NotificationTemplateUpdate(
                        subject=new_subject,
                        body=new_body,
                    ),
                )

        if args.apply and (changed or deactivated):
            print(
                f"\nAPPLIED: {changed} updated, {deactivated} deactivated "
                "through the notification owner."
            )
        elif changed or deactivated:
            print(
                f"\nDRY-RUN: {changed} row(s) would change, {deactivated} would "
                "be deactivated. Re-run with --apply."
            )
        else:
            print("No rows need changes.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
