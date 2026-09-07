"""Customer-facing payment receipt rendering helpers."""

from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Payment, PaymentAllocation, PaymentStatus
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services import email as email_service
from app.services.billing.payment_receipt_identity import payment_receipt_reference
from app.services.billing_invoice_pdf import (
    _build_simple_pdf,
    _ensure_weasyprint_pydyf_compat,
    _truncate_text,
)
from app.services.common import coerce_uuid
from app.services.customer_portal_context import get_allowed_account_ids

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentReceiptPdfDownload:
    """Typed payment-receipt document boundary for HTTP and other adapters."""

    filename: str
    content: bytes
    content_type: str = "application/pdf"


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or "0.00"))


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _receipt_number(payment: Payment) -> str:
    return payment_receipt_reference(payment.id, payment.receipt_number)


def _safe_receipt_number(payment: Payment) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", _receipt_number(payment).lstrip("#")).strip(
        "-"
    )


def _account_display(account: Subscriber | None) -> str:
    if not account:
        return "Customer"
    for value in (
        getattr(account, "name", None),
        getattr(account, "business_name", None),
        getattr(account, "username", None),
        " ".join(
            part
            for part in (
                str(getattr(account, "first_name", "") or "").strip(),
                str(getattr(account, "last_name", "") or "").strip(),
            )
            if part
        ),
    ):
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return "Customer"


def _account_email(account: Subscriber | None) -> str:
    if not account:
        return ""
    return str(
        getattr(account, "email", None) or getattr(account, "billing_email", None) or ""
    ).strip()


def _payment_method(payment: Payment) -> str:
    if payment.payment_channel and payment.payment_channel.name:
        return payment.payment_channel.name
    if payment.payment_method and payment.payment_method.method_type:
        return _enum_value(payment.payment_method.method_type).replace("_", " ").title()
    if payment.provider and payment.provider.name:
        return payment.provider.name
    return "Payment"


def get_payment_receipt_context(
    db: Session,
    payment_id: str,
    *,
    allowed_account_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    payment = (
        db.query(Payment)
        .options(
            selectinload(Payment.account),
            selectinload(Payment.payment_channel),
            selectinload(Payment.payment_method),
            selectinload(Payment.provider),
            selectinload(Payment.settlement),
            selectinload(Payment.allocations).selectinload(PaymentAllocation.invoice),
        )
        .filter(Payment.id == coerce_uuid(payment_id))
        .filter(Payment.is_active.is_(True))
        .first()
    )
    if not payment or payment.status != PaymentStatus.succeeded:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if payment.account_id is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if (
        allowed_account_ids is not None
        and str(payment.account_id) not in allowed_account_ids
    ):
        raise HTTPException(status_code=404, detail="Receipt not found")

    allocations = [item for item in payment.allocations if item.is_active]
    application = billing_service.payments.application_summary(db, payment)
    amount_applied = application.invoice_amount_applied
    amount_received = application.amount_received
    amount_credited = application.amount_credited
    prepaid_amount_applied = application.prepaid_amount_applied
    unallocated_credit = application.unallocated_credit
    occurred_at = payment.paid_at or payment.created_at or datetime.now(UTC)
    account = payment.account

    allocation_rows = []
    for allocation in allocations:
        invoice = allocation.invoice
        invoice_status = (
            _enum_value(getattr(invoice, "status", "")).replace("_", " ").title()
        )
        allocation_rows.append(
            SimpleNamespace(
                invoice_id=str(allocation.invoice_id),
                invoice_number=(invoice.invoice_number if invoice else None)
                or f"INV-{str(allocation.invoice_id)[:8].upper()}",
                status=invoice_status or "Applied",
                amount=_money(allocation.amount),
            )
        )

    from app.models.catalog import CatalogOffer, Subscription
    from app.services.prepaid_service_renewals import renewal_outcomes_for_payment

    renewal_rows = []
    for outcome in renewal_outcomes_for_payment(db, payment.id):
        subscription = db.get(Subscription, outcome.subscription_id)
        offer = (
            db.get(CatalogOffer, subscription.offer_id)
            if subscription is not None
            else None
        )
        renewal_rows.append(
            SimpleNamespace(
                subscription_id=str(outcome.subscription_id),
                offer_name=getattr(offer, "name", None) or "Service",
                amount=outcome.amount,
                renewed_through=outcome.renewed_through,
            )
        )

    return {
        "payment": payment,
        "receipt_number": _receipt_number(payment),
        "receipt_date": occurred_at,
        "account_number": getattr(account, "account_number", None)
        or getattr(account, "splynx_customer_id", None)
        or str(payment.account_id)[:8],
        "received_from": _account_display(account),
        "received_email": _account_email(account),
        "amount_received": amount_received,
        "amount_credited": amount_credited,
        "amount_applied": amount_applied,
        "prepaid_amount_applied": prepaid_amount_applied,
        "unallocated_credit": unallocated_credit,
        "settlement_evidenced": application.settlement_evidenced,
        "currency": payment.currency or "NGN",
        "status": _enum_value(payment.status).replace("_", " ").title(),
        "transaction_ref": payment.external_id
        or payment.receipt_number
        or str(payment.id),
        "method": _payment_method(payment),
        "allocations": allocation_rows,
        "renewals": renewal_rows,
    }


def get_customer_payment_receipt_context(
    db: Session, customer: dict, payment_id: str
) -> dict[str, Any]:
    allowed = get_allowed_account_ids(customer, db)
    return get_payment_receipt_context(db, payment_id, allowed_account_ids=allowed)


def _format_amount(currency: str, amount: Decimal) -> str:
    symbol = "₦" if currency == "NGN" else f"{currency} "
    return f"{symbol}{amount:,.2f}"


def render_receipt_document_html(context: dict[str, Any]) -> str:
    amount_credited = _money(
        context.get("amount_credited", context.get("amount_received"))
    )
    prepaid_amount_applied = _money(context.get("prepaid_amount_applied"))
    rows = []
    for allocation in context["allocations"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(allocation.invoice_number)}</td>"
            f"<td>{html.escape(allocation.status)}</td>"
            f"<td class='right'>{html.escape(_format_amount(context['currency'], allocation.amount))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='3' class='muted empty'>No invoice allocation recorded.</td></tr>"
        )

    renewal_section = ""
    if context.get("renewals"):
        renewal_items = "".join(
            "<div>"
            f"{html.escape(renewal.offer_name)}: renewed through "
            f"{html.escape(renewal.renewed_through.strftime('%B %d, %Y'))} "
            f"({_format_amount(context['currency'], renewal.amount)})"
            "</div>"
            for renewal in context["renewals"]
        )
        renewal_section = (
            "<div class='details'>"
            "<div class='details-title'>SERVICE RENEWAL CONFIRMED</div>"
            f"{renewal_items}</div>"
        )

    date_value = context["receipt_date"].strftime("%Y-%m-%d")
    payment_date = context["receipt_date"].strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4; margin: 14mm; }}
body {{ margin: 0; background: #fff; color: #111827; font-family: Inter, Arial, sans-serif; }}
.receipt {{ border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; }}
.header {{ background: #176f37; color: white; padding: 24px 28px 18px; min-height: 116px; position: relative; }}
.logo {{ display: inline-block; background: white; color: #176f37; font-weight: 900; font-size: 25px; letter-spacing: .5px; padding: 8px 14px; }}
.company {{ position: absolute; left: 28px; bottom: 16px; font-size: 17px; font-weight: 800; }}
.receipt-no {{ position: absolute; right: 24px; top: 22px; width: 190px; border-radius: 12px; background: white; color: #111827; padding: 16px; }}
.receipt-no .label {{ color: #176f37; font-size: 11px; font-weight: 800; }}
.receipt-no .number {{ margin-top: 8px; font-size: 18px; font-weight: 900; }}
.receipt-no .meta {{ margin-top: 8px; font-size: 11px; line-height: 1.7; }}
.body {{ padding: 22px 22px 28px; }}
.cards {{ display: flex; }}
.card {{ flex: 1; margin-right: 12px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 13px; min-height: 58px; }}
.card:last-child {{ margin-right: 0; }}
.card.highlight {{ border-color: #7fd0a0; background: #edfff5; }}
.kicker {{ color: #176f37; font-size: 11px; font-weight: 900; }}
.value {{ margin-top: 10px; font-size: 13px; font-weight: 700; }}
.big {{ color: #176f37; font-size: 20px; font-weight: 900; }}
.sub {{ margin-top: 7px; color: #6b7280; font-size: 11px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 30px; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
thead {{ background: #176f37; color: white; }}
th {{ padding: 13px 14px; font-size: 11px; text-align: left; }}
td {{ padding: 14px; font-size: 13px; height: 38px; border-top: 1px solid #f3f4f6; }}
.right {{ text-align: right; }}
.summary {{ margin: 30px 0 30px auto; width: 205px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; }}
.summary-row {{ display: flex; justify-content: space-between; font-size: 12px; line-height: 2.1; }}
.summary-row strong {{ font-weight: 900; }}
.green {{ color: #176f37; }}
.details {{ border-left: 6px solid #22c55e; border-radius: 0 8px 8px 0; background: #edfff5; padding: 14px 16px; font-size: 12px; }}
.details-title {{ color: #176f37; font-weight: 900; margin-bottom: 8px; }}
.footer {{ display: flex; justify-content: space-between; margin-top: 62px; color: #6b7280; font-size: 11px; }}
.muted {{ color: #6b7280; }}
.empty {{ text-align: center; padding: 34px; }}
</style>
</head>
<body>
<div class="receipt">
  <div class="header">
    <div class="logo">DOTMAC</div>
    <div class="company">{html.escape(context["received_from"])}</div>
    <div class="receipt-no">
      <div class="label">RECEIPT</div>
      <div class="number">{html.escape(context["receipt_number"])}</div>
      <div class="meta">Date: {html.escape(date_value)}<br>Account: {html.escape(str(context["account_number"]))}</div>
    </div>
  </div>
  <div class="body">
    <div class="cards">
      <div class="card"><div class="kicker">RECEIVED FROM</div><div class="value">{html.escape(context["received_from"])}</div><div class="sub">{html.escape(context["received_email"])}</div></div>
      <div class="card highlight"><div class="kicker">AMOUNT RECEIVED</div><div class="big">{html.escape(_format_amount(context["currency"], context["amount_received"]))}</div><div class="sub">Status: {html.escape(context["status"])}</div></div>
      <div class="card"><div class="kicker">TRANSACTION REF</div><div class="value">{html.escape(context["transaction_ref"])}</div><div class="sub">Method: {html.escape(context["method"])}</div></div>
    </div>
    <table>
      <thead><tr><th>INVOICE</th><th>STATUS</th><th class="right">AMOUNT APPLIED</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div class="summary">
      <div class="summary-row"><span>Amount Received</span><strong>{html.escape(_format_amount(context["currency"], context["amount_received"]))}</strong></div>
      <div class="summary-row"><span>Amount Credited</span><strong>{html.escape(_format_amount(context["currency"], amount_credited))}</strong></div>
      <div class="summary-row"><span>Applied to Invoices</span><strong>{html.escape(_format_amount(context["currency"], context["amount_applied"]))}</strong></div>
      <div class="summary-row"><span>Applied to Service</span><strong>{html.escape(_format_amount(context["currency"], prepaid_amount_applied))}</strong></div>
      <div class="summary-row green"><span>Account Credit Remaining</span><strong>{html.escape(_format_amount(context["currency"], context["unallocated_credit"]))}</strong></div>
    </div>
    {renewal_section}
    <div class="details">
      <div class="details-title">PAYMENT DETAILS</div>
      <div>Payment Date: {html.escape(payment_date)}</div>
      <div>Payment ID: {html.escape(str(context["payment"].id))}</div>
    </div>
    <div class="footer"><span>Prepared by Dotmac Selfcare</span><span>Thank you for your business.</span></div>
  </div>
</div>
</body>
</html>"""


def _build_weasyprint_receipt_pdf(context: dict[str, Any]) -> bytes:
    _ensure_weasyprint_pydyf_compat()
    from weasyprint import HTML

    return HTML(string=render_receipt_document_html(context)).write_pdf()


def _receipt_text_lines(context: dict[str, Any]) -> list[str]:
    amount_credited = _money(
        context.get("amount_credited", context.get("amount_received"))
    )
    prepaid_amount_applied = _money(context.get("prepaid_amount_applied"))
    lines = [
        f"Payment Receipt {context['receipt_number']}",
        f"Received From: {context['received_from']}",
        f"Email: {context['received_email'] or '-'}",
        f"Account: {context['account_number']}",
        f"Date: {context['receipt_date'].strftime('%Y-%m-%d %H:%M UTC')}",
        f"Amount Received: {_format_amount(context['currency'], context['amount_received'])}",
        f"Amount Credited: {_format_amount(context['currency'], amount_credited)}",
        f"Applied to Invoices: {_format_amount(context['currency'], context['amount_applied'])}",
        f"Applied to Service: {_format_amount(context['currency'], prepaid_amount_applied)}",
        f"Account Credit Remaining: {_format_amount(context['currency'], context['unallocated_credit'])}",
        f"Status: {context['status']}",
        f"Transaction Ref: {context['transaction_ref']}",
        f"Method: {context['method']}",
        "",
        "Allocations:",
    ]
    allocations = context.get("allocations") or []
    if not allocations:
        lines.append("- No invoice allocation recorded.")
    else:
        for allocation in allocations:
            lines.append(
                "- "
                f"{allocation.invoice_number} | {allocation.status} | "
                f"{_format_amount(context['currency'], allocation.amount)}"
            )
    lines.extend(["", f"Payment ID: {context['payment'].id}"])
    renewals = context.get("renewals") or []
    if renewals:
        lines.extend(["", "Service Renewals:"])
        for renewal in renewals:
            lines.append(
                "- "
                f"{renewal.offer_name} renewed through "
                f"{renewal.renewed_through.strftime('%B %d, %Y')} | "
                f"{_format_amount(context['currency'], renewal.amount)}"
            )
    lines.append("Thank you for your business.")
    return lines


def _build_receipt_fallback_pdf(context: dict[str, Any]) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return _build_simple_pdf(_receipt_text_lines(context))

    def _font_path(bold: bool) -> str | None:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        return None

    def _font(size: int, bold: bool = False):
        path = _font_path(bold)
        if path:
            return ImageFont.truetype(path, size=size)
        return ImageFont.load_default()

    def _display_amount(currency: str, amount: Decimal) -> str:
        if currency == "NGN" and not _font_path(False):
            return f"NGN {amount:,.2f}"
        return _format_amount(currency, amount)

    amount_credited = _money(
        context.get("amount_credited", context.get("amount_received"))
    )
    prepaid_amount_applied = _money(context.get("prepaid_amount_applied"))

    width, height = 1240, 1754
    margin_x = 72
    page = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(page)

    green_900 = "#166534"
    green_700 = "#15803d"
    green_50 = "#f0fdf4"
    slate_900 = "#0f172a"
    slate_700 = "#334155"
    slate_500 = "#64748b"
    slate_200 = "#e2e8f0"

    title_font = _font(42, bold=True)
    heading_font = _font(28, bold=True)
    body_font = _font(22)
    small_font = _font(18)
    label_font = _font(18, bold=True)
    value_font = _font(24, bold=True)

    draw.rounded_rectangle(
        (36, 36, width - 36, height - 36),
        radius=28,
        outline=slate_200,
        width=2,
        fill="#ffffff",
    )
    draw.rounded_rectangle((36, 36, width - 36, 350), radius=28, fill=green_900)
    draw.rectangle((36, 220, width - 36, 350), fill=green_900)

    draw.rounded_rectangle(
        (margin_x, 78, margin_x + 220, 150), radius=14, fill="#ffffff"
    )
    draw.text((margin_x + 26, 94), "DOTMAC", font=title_font, fill=green_900)

    panel_x = width - 420
    draw.rounded_rectangle((panel_x, 82, width - 84, 258), radius=22, fill="#ffffff")
    draw.text((panel_x + 28, 106), "RECEIPT", font=label_font, fill=green_900)
    draw.text(
        (panel_x + 28, 138),
        _truncate_text(str(context["receipt_number"]), 18),
        font=heading_font,
        fill=slate_900,
    )
    draw.text(
        (panel_x + 28, 190),
        f"Date: {context['receipt_date'].strftime('%Y-%m-%d')}",
        font=small_font,
        fill=slate_700,
    )
    draw.text(
        (panel_x + 28, 218),
        f"Account: {_truncate_text(str(context['account_number']), 24)}",
        font=small_font,
        fill=slate_700,
    )
    draw.text(
        (margin_x, 292),
        _truncate_text(str(context["received_from"]), 42),
        font=heading_font,
        fill="#ffffff",
    )

    y = 390
    card_height = 138
    card_gap = 22
    card_width = (width - (margin_x * 2) - card_gap * 2) // 3
    cards = [
        (
            "Received From",
            _truncate_text(str(context["received_from"]), 28),
            _truncate_text(str(context["received_email"] or "-"), 34),
            "#ffffff",
            slate_900,
        ),
        (
            "Amount Received",
            _display_amount(context["currency"], context["amount_received"]),
            f"Status: {context['status']}",
            green_50,
            green_900,
        ),
        (
            "Reference",
            _truncate_text(str(context["transaction_ref"]), 28),
            _truncate_text(str(context["method"]), 34),
            "#ffffff",
            slate_900,
        ),
    ]
    for index, (title, line1, line2, fill, accent) in enumerate(cards):
        left = margin_x + index * (card_width + card_gap)
        draw.rounded_rectangle(
            (left, y, left + card_width, y + card_height),
            radius=20,
            fill=fill,
            outline=slate_200,
        )
        draw.text((left + 20, y + 18), title.upper(), font=label_font, fill=green_700)
        draw.text(
            (left + 20, y + 54),
            line1,
            font=value_font if index == 1 else body_font,
            fill=accent,
        )
        draw.text((left + 20, y + 94), line2, font=small_font, fill=slate_700)

    table_top = 572
    table_left = margin_x
    table_right = width - margin_x
    draw.rounded_rectangle(
        (table_left, table_top, table_right, table_top + 490),
        radius=22,
        fill="#ffffff",
        outline=slate_200,
    )
    draw.rounded_rectangle(
        (table_left, table_top, table_right, table_top + 66), radius=22, fill=green_900
    )
    columns = [table_left + 24, table_left + 620, table_left + 880]
    headers = ["Invoice", "Status", "Amount Applied"]
    for header, col in zip(headers, columns):
        draw.text(
            (col, table_top + 20), header.upper(), font=label_font, fill="#ffffff"
        )

    allocations = context.get("allocations") or []
    row_y = table_top + 88
    row_height = 62
    if not allocations:
        draw.text(
            (columns[0], row_y),
            "No invoice allocation recorded.",
            font=body_font,
            fill=slate_500,
        )
    else:
        for index, allocation in enumerate(allocations[:6]):
            if index % 2 == 1:
                draw.rounded_rectangle(
                    (
                        table_left + 6,
                        row_y - 8,
                        table_right - 6,
                        row_y + row_height - 10,
                    ),
                    radius=12,
                    fill=green_50,
                )
            draw.text(
                (columns[0], row_y),
                _truncate_text(str(allocation.invoice_number), 36),
                font=body_font,
                fill=slate_900,
            )
            draw.text(
                (columns[1], row_y),
                _truncate_text(str(allocation.status), 20),
                font=body_font,
                fill=slate_700,
            )
            draw.text(
                (columns[2], row_y),
                _display_amount(context["currency"], allocation.amount),
                font=body_font,
                fill=slate_900,
            )
            row_y += row_height
        if len(allocations) > 6:
            draw.text(
                (columns[0], row_y),
                f"... and {len(allocations) - 6} more allocations",
                font=small_font,
                fill=slate_500,
            )

    totals_left = width - 460
    totals_top = 1118
    draw.rounded_rectangle(
        (totals_left, totals_top, width - margin_x, totals_top + 230),
        radius=20,
        fill="#ffffff",
        outline=slate_200,
    )
    totals = [
        (
            "Amount Received",
            _display_amount(context["currency"], context["amount_received"]),
        ),
        (
            "Amount Credited",
            _display_amount(context["currency"], amount_credited),
        ),
        (
            "Applied to Invoices",
            _display_amount(context["currency"], context["amount_applied"]),
        ),
        (
            "Applied to Service",
            _display_amount(context["currency"], prepaid_amount_applied),
        ),
        (
            "Account Credit Remaining",
            _display_amount(context["currency"], context["unallocated_credit"]),
        ),
    ]
    for index, (label, value) in enumerate(totals):
        ty = totals_top + 22 + (index * 44)
        draw.text(
            (totals_left + 22, ty),
            label,
            font=body_font,
            fill=green_900 if index == len(totals) - 1 else slate_700,
        )
        draw.text(
            (totals_left + 242, ty),
            value,
            font=value_font if index == len(totals) - 1 else body_font,
            fill=green_900 if index == len(totals) - 1 else slate_900,
        )

    renewals = context.get("renewals") or []
    renewal_line_count = min(len(renewals), 2)
    details_top = 1370
    details_height = 142 + (renewal_line_count * 34)
    draw.rounded_rectangle(
        (margin_x, details_top, width - margin_x, details_top + details_height),
        radius=18,
        fill=green_50,
        outline="#bbf7d0",
    )
    draw.rectangle(
        (margin_x, details_top, margin_x + 12, details_top + details_height),
        fill=green_700,
    )
    draw.text(
        (margin_x + 28, details_top + 20),
        "PAYMENT DETAILS",
        font=label_font,
        fill=green_900,
    )
    draw.text(
        (margin_x + 28, details_top + 58),
        f"Payment Date: {context['receipt_date'].strftime('%Y-%m-%d %H:%M UTC')}",
        font=body_font,
        fill=slate_900,
    )
    draw.text(
        (margin_x + 28, details_top + 94),
        f"Payment ID: {context['payment'].id}",
        font=body_font,
        fill=slate_700,
    )
    for index, renewal in enumerate(renewals[:2]):
        draw.text(
            (margin_x + 28, details_top + 128 + (index * 34)),
            _truncate_text(
                f"{renewal.offer_name} renewed through "
                f"{renewal.renewed_through.strftime('%Y-%m-%d')}",
                76,
            ),
            font=small_font,
            fill=green_900,
        )

    footer_y = 1612
    draw.text(
        (margin_x, footer_y),
        "Prepared by Dotmac Selfcare",
        font=small_font,
        fill=slate_500,
    )
    draw.text(
        (width - 360, footer_y),
        "Thank you for your business.",
        font=small_font,
        fill=slate_500,
    )

    output = io.BytesIO()
    page.save(output, format="PDF", resolution=144.0)
    return output.getvalue()


def build_receipt_pdf(context: dict[str, Any]) -> bytes:
    try:
        return _build_weasyprint_receipt_pdf(context)
    except Exception as exc:
        logger.info(
            "WeasyPrint export failed for payment receipt %s; using branded PDF fallback: %s",
            getattr(context.get("payment"), "id", "unknown"),
            exc,
        )
        return _build_receipt_fallback_pdf(context)


def download_filename(payment: Payment) -> str:
    return f"receipt-{_safe_receipt_number(payment)}.pdf"


def build_customer_receipt_pdf_download(
    db: Session,
    *,
    subscriber_id: str,
    payment_id: str,
) -> PaymentReceiptPdfDownload:
    """Build the canonical receipt after enforcing the customer's account scope."""

    context = get_payment_receipt_context(
        db,
        payment_id,
        allowed_account_ids={subscriber_id},
    )
    return PaymentReceiptPdfDownload(
        filename=download_filename(context["payment"]),
        content=build_receipt_pdf(context),
    )


def send_receipt_email(
    db: Session,
    payment_id: str,
    to_email: str,
    *,
    base_url: str = "https://selfcare.dotmac.ng",
) -> bool:
    context = get_payment_receipt_context(db, payment_id, allowed_account_ids=None)
    receipt_url = f"{base_url.rstrip('/')}/portal/billing/payments/{context['payment'].id}/receipt"
    subject = f"Payment receipt {context['receipt_number']}"
    body_html = (
        f"<p>Payment receipt {html.escape(context['receipt_number'])}</p>"
        f"<p>Amount received: <strong>{html.escape(_format_amount(context['currency'], context['amount_received']))}</strong></p>"
        f'<p>View or download the receipt here: <a href="{html.escape(receipt_url)}">{html.escape(receipt_url)}</a></p>'
    )
    body_text = (
        f"Payment receipt {context['receipt_number']}\n"
        f"Amount received: {_format_amount(context['currency'], context['amount_received'])}\n"
        f"View or download: {receipt_url}"
    )
    return email_service.send_email(
        db,
        to_email,
        subject,
        body_html,
        body_text=body_text,
        activity="billing_payment_receipt",
    )
