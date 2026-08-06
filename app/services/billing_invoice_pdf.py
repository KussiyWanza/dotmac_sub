"""Invoice PDF export service.

Queues invoice PDF exports, tracks status in DB, and renders PDF artifacts.
"""

from __future__ import annotations

import base64
import html
import inspect
import io
import logging
import mimetypes
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoicePdfExport, InvoicePdfExportStatus
from app.models.domain_settings import SettingDomain
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, SubscriberCategory
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import branding_storage as branding_storage_service
from app.services import domain_settings as domain_settings_service
from app.services import invoice_bank_details as invoice_bank_details_service
from app.services import settings_spec
from app.services import web_system_company_info as company_info_service
from app.services.db_session_adapter import db_session_adapter
from app.services.file_storage import file_uploads
from app.services.object_storage import (
    ObjectNotFoundError,
    StreamResult,
    get_s3_storage,
)

if TYPE_CHECKING:
    from app.services.brand_profiles import ResolvedBrand

STALE_EXPORT_SECONDS = 20
# Naira sign (U+20A6). DejaVu Sans (used by both WeasyPrint and the PIL
# fallback) includes the glyph; renderers without it use an "NGN " prefix.
NAIRA_SIGN = "₦"
INVOICE_PDF_CACHE_METRICS_KEY = "invoice_pdf_cache_metrics"
INVOICE_PDF_TEMPLATE_REFRESHED_AT = datetime(2026, 3, 18, 9, 0, tzinfo=UTC)
logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


def _normalize_requested_by_id(db: Session, requested_by_id: str | None) -> str | None:
    candidate = str(requested_by_id or "").strip()
    if not candidate:
        return None
    return candidate if db.get(Subscriber, candidate) else None


def _safe_invoice_number(invoice: Invoice) -> str:
    raw = (invoice.invoice_number or str(invoice.id)).strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or str(invoice.id)


def _display_account_name(invoice: Invoice) -> str:
    account = getattr(invoice, "account", None)
    if not account:
        return "Account"
    account_name = str(getattr(account, "name", "") or "").strip()
    if account_name:
        return account_name
    return "Account"


def _truncate_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _money(value: Decimal | None) -> str:
    amount = Decimal(str(value or Decimal("0.00")))
    return f"{amount:,.2f}"


def _format_date(value: datetime | None) -> str:
    if not value:
        return "N/A"
    return value.strftime("%Y-%m-%d")


def _company_lines(company_info: dict[str, str]) -> list[str]:
    lines = [
        (company_info.get("company_name") or "").strip(),
        (company_info.get("company_address_street1") or "").strip(),
        (company_info.get("company_address_street2") or "").strip(),
    ]
    city_line = " ".join(
        part
        for part in [
            (company_info.get("company_address_city") or "").strip(),
            (company_info.get("company_address_zip") or "").strip(),
        ]
        if part
    ).strip()
    country = (company_info.get("company_address_country") or "").strip()
    if city_line:
        lines.append(city_line)
    if country:
        lines.append(country)
    email = (company_info.get("company_email") or "").strip()
    phone = (company_info.get("company_phone") or "").strip()
    if email:
        lines.append(email)
    if phone:
        lines.append(phone)
    return [line for line in lines if line]


def _branded_company_info(
    company_info: dict[str, str], brand: ResolvedBrand
) -> dict[str, str]:
    address = brand.legal_address or {}
    return {
        **company_info,
        "company_name": brand.legal_name,
        "company_email": brand.support_email,
        "company_phone": brand.support_phone,
        "company_address_street1": address.get("street1", ""),
        "company_address_street2": address.get("street2", ""),
        "company_address_city": address.get("city", ""),
        "company_address_zip": address.get("postal_code", ""),
        "company_address_country": address.get("country", ""),
    }


def _logo_src(db: Session, configured_logo: str | None = None) -> str | None:
    raw_logo = configured_logo or settings_spec.resolve_value(
        db, SettingDomain.comms, "sidebar_logo_url"
    )
    logo_value = str(raw_logo or "").strip()
    if not logo_value:
        return None
    if logo_value.startswith("data:"):
        return logo_value
    if logo_value.startswith("/branding/assets/"):
        file_id = branding_storage_service.file_id_from_branding_url(logo_value)
        if not file_id:
            return None
        record = db.get(StoredFile, file_id)
        if not record or record.is_deleted:
            return None
        stream = file_uploads.stream_file(record)
        content = b"".join(stream.chunks)
        mime = record.content_type or "image/png"
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    if logo_value.startswith("/static/"):
        path = Path(logo_value.lstrip("/"))
        if path.exists():
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return None
    return logo_value


def _decode_logo_image(db: Session, configured_logo: str | None = None):
    try:
        from PIL import Image
    except Exception:
        return None

    logo_src = _logo_src(db, configured_logo)
    if not logo_src:
        return None
    if not logo_src.startswith("data:"):
        return None
    _, _, encoded = logo_src.partition(",")
    if not encoded:
        return None
    try:
        image_bytes = base64.b64decode(encoded)
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        return image.convert("RGBA")
    except Exception:
        return None


def _ensure_weasyprint_pydyf_compat() -> None:
    try:
        import pydyf
    except Exception:
        return

    signature = inspect.signature(pydyf.PDF.__init__)
    if len(signature.parameters) != 1:
        return
    if getattr(pydyf.PDF, "_dotmac_weasyprint_compat", False):
        return

    original_pdf = cast(type[Any], pydyf.PDF)

    def _compat_init(self: Any, version: Any = None, identifier: Any = None) -> None:
        original_pdf.__init__(self)
        self.version = version or b"1.7"
        self.identifier = identifier

    pydyf.PDF = type(
        "CompatPDF",
        (original_pdf,),
        {
            "_dotmac_weasyprint_compat": True,
            "__init__": _compat_init,
        },
    )


def _render_invoice_html(invoice: Invoice, db: Session) -> str:
    lines = [line for line in (invoice.lines or []) if getattr(line, "is_active", True)]
    rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(line.description or 'Line item'))}</td>"
            f"<td class='num'>{html.escape(str(line.quantity or Decimal('0')))}</td>"
            f"<td class='num'>{NAIRA_SIGN}{_money(line.unit_price)}</td>"
            f"<td class='num'>{NAIRA_SIGN}{_money(line.amount)}</td>"
            "</tr>"
        )
        for line in lines
    )
    if not rows:
        rows = "<tr><td colspan='4'>No line items</td></tr>"

    account_name = html.escape(_display_account_name(invoice))
    account_email = html.escape(
        (getattr(invoice.account, "email", None) or "").strip() or "N/A"
    )
    invoice_number = html.escape(invoice.invoice_number or str(invoice.id))
    memo = html.escape((invoice.memo or "").strip() or "-")
    status = html.escape(
        invoice.status.value.replace("_", " ").title() if invoice.status else "Draft"
    )
    from app.services.brand_profiles import resolve_brand

    account_id = getattr(getattr(invoice, "account", None), "id", None)
    brand = resolve_brand(db, subscriber_id=account_id)
    logo_src = _logo_src(db, brand.logo_url)
    company_info = _branded_company_info(
        company_info_service.get_company_info(db), brand
    )
    company_name = html.escape(brand.legal_name)
    accent_invoice_id = invoice.invoice_number or str(invoice.id)
    tax_label = "Tax"
    vat_number = html.escape((company_info.get("company_vat_number") or "").strip())
    if vat_number:
        tax_label = f"Tax / VAT ({vat_number})"
    logo_markup = (
        f'<img src="{html.escape(logo_src)}" alt="{company_name} logo" class="logo">'
        if logo_src
        else f'<div class="logo-fallback">{company_name[:1].upper()}</div>'
    )
    bank_details = invoice_bank_details_service.get_invoice_bank_details(
        db, currency=invoice.currency
    )
    bank_markup = ""
    if bank_details:
        bank_rows = [
            ("Bank Name", bank_details.get("bank_name", "")),
            ("Account Name", bank_details.get("account_name", "")),
            ("Account Number", bank_details.get("account_number", "")),
            ("Sort Code", bank_details.get("sort_code", "")),
        ]
        bank_markup = "".join(
            (
                '<div class="bank-row">'
                f"<span>{html.escape(label)}</span>"
                f"<strong>{html.escape(value)}</strong>"
                "</div>"
            )
            for label, value in bank_rows
            if value
        )
        if bank_markup:
            bank_markup = (
                '<div class="bank-card">'
                '<p class="card-title">Bank Details</p>'
                f'<div class="bank-grid">{bank_markup}</div>'
                "</div>"
            )

    return f"""
<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<title>Invoice {invoice_number}</title>
<style>
  @page {{ size: A4; margin: 14mm; }}
  :root {{
    --green-900: #166534;
    --green-800: #15803d;
    --green-700: #16a34a;
    --green-100: #dcfce7;
    --green-50: #f0fdf4;
    --red-700: #b91c1c;
    --red-100: #fee2e2;
    --slate-900: #0f172a;
    --slate-700: #334155;
    --slate-500: #64748b;
    --slate-300: #cbd5e1;
    --slate-200: #e2e8f0;
    --white: #ffffff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: DejaVu Sans, Arial, sans-serif; color: var(--slate-900); font-size: 12px; margin: 0; background: var(--white); }}
  .page {{ border: 1px solid var(--slate-200); border-radius: 18px; overflow: hidden; }}
  .hero {{
    background: var(--green-900);
    color: var(--white);
    padding: 22px 24px 18px;
  }}
  .hero-top {{ display: flex; justify-content: space-between; align-items: flex-start; width: 100%; }}
  .hero-top > * + * {{ margin-left: 24px; }}
  .brand {{ display: flex; align-items: flex-start; max-width: 58%; }}
  .logo {{ max-height: 52px; max-width: 150px; display: block; object-fit: contain; background: var(--white); border-radius: 12px; padding: 6px 10px; }}
  .logo-fallback {{ width: 54px; height: 54px; border-radius: 14px; background: rgba(255,255,255,0.16); color: var(--white); display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: 700; }}
  .customer-name {{ margin: 42px 0 0; max-width: 360px; color: var(--white); font-size: 20px; font-weight: 700; line-height: 1.25; overflow-wrap: anywhere; text-align: left; }}
  .invoice-panel {{ min-width: 220px; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22); border-radius: 16px; padding: 14px 16px; }}
  .eyebrow {{ margin: 0 0 4px; font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase; opacity: 0.82; }}
  .invoice-title {{ margin: 0; font-size: 28px; font-weight: 800; }}
  .invoice-meta {{ margin-top: 8px; font-size: 11px; line-height: 1.7; }}
  .status-pill {{ display: inline-block; margin-top: 10px; border-radius: 999px; background: rgba(255,255,255,0.16); padding: 4px 10px; font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }}
  .body {{ padding: 22px 24px 24px; background: linear-gradient(180deg, var(--green-50), var(--white) 32%); }}
  .summary-table {{ width: 100%; border-collapse: separate; border-spacing: 14px 0; margin: 0 -14px 18px; table-layout: fixed; }}
  .summary-table td {{ vertical-align: top; padding: 0; border: none; background: transparent; }}
  .card {{ background: var(--white); border: 1px solid var(--slate-200); border-radius: 16px; padding: 14px 16px; min-height: 100px; }}
  .card-title {{ margin: 0 0 8px; color: var(--green-900); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; }}
  .card-copy {{ margin: 0; color: var(--slate-700); line-height: 1.7; }}
  .billed-name {{ display: block; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .highlight-card {{ border-color: var(--green-700); background: linear-gradient(180deg, var(--white), var(--green-50)); }}
  .highlight-value {{ margin: 0; font-size: 22px; font-weight: 800; color: var(--green-900); }}
  .alert-card {{ border-color: var(--red-100); }}
  .alert-card .card-title {{ color: var(--red-700); }}
  .table-shell {{ border: 1px solid var(--slate-200); border-radius: 16px; overflow: hidden; background: var(--white); }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    background: var(--green-900);
    color: var(--white);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 11px 12px;
    text-align: left;
  }}
  tbody td {{ padding: 12px; border-bottom: 1px solid var(--slate-200); vertical-align: top; }}
  tbody tr:nth-child(even) td {{ background: #fafefb; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .totals-wrap {{ display: flex; justify-content: flex-end; margin-top: 16px; }}
  .totals-card {{ width: 320px; border: 1px solid var(--slate-200); border-radius: 16px; padding: 14px 16px; background: var(--white); }}
  .totals-card table td {{ border: none; padding: 5px 0; }}
  .totals-card .grand-total td {{ color: var(--green-900); font-size: 15px; font-weight: 800; padding-top: 10px; border-top: 1px solid var(--slate-200); }}
  .bank-card {{ margin-top: 16px; border: 1px solid var(--slate-200); border-radius: 16px; padding: 14px 16px; background: var(--white); }}
  .bank-grid {{ display: table; width: 100%; }}
  .bank-row {{ display: table-row; }}
  .bank-row span, .bank-row strong {{ display: table-cell; padding: 4px 0; line-height: 1.5; }}
  .bank-row span {{ color: var(--slate-500); width: 34%; }}
  .bank-row strong {{ color: var(--slate-900); font-weight: 700; }}
  .memo {{ margin-top: 18px; border-left: 4px solid var(--red-700); background: #fff8f8; border-radius: 0 14px 14px 0; padding: 14px 16px; }}
  .memo strong {{ color: var(--red-700); }}
  .footer {{ margin-top: 18px; color: var(--slate-500); font-size: 10px; display: flex; justify-content: space-between; }}
  .footer > * + * {{ margin-left: 16px; }}
</style>
</head>
<body>
  <div class=\"page\">
    <div class=\"hero\">
      <div class=\"hero-top\">
        <div class=\"brand\">
          {logo_markup}
        </div>
        <div class=\"invoice-panel\">
          <p class=\"eyebrow\">Invoice</p>
          <p class=\"invoice-title\">#{html.escape(str(accent_invoice_id))}</p>
          <div class=\"invoice-meta\">
            <div>Issued: {_format_date(invoice.issued_at)}</div>
            <div>Due: {_format_date(invoice.due_at)}</div>
            <div>Currency: {html.escape(invoice.currency or "NGN")}</div>
          </div>
          <div class=\"status-pill\">{status}</div>
        </div>
      </div>
      <p class=\"customer-name\">{account_name}</p>
    </div>

    <div class=\"body\">
      <table class=\"summary-table\" role=\"presentation\">
        <tr>
          <td style=\"width: 40%;\">
            <div class=\"card\">
              <p class=\"card-title\">Billed To</p>
              <p class=\"card-copy\"><span class=\"billed-name\">{account_name}</span><br>{account_email}</p>
            </div>
          </td>
          <td style=\"width: 30%;\">
            <div class=\"card highlight-card\">
              <p class=\"card-title\">Balance Due</p>
              <p class=\"highlight-value\">{NAIRA_SIGN}{_money(invoice.balance_due)}</p>
            </div>
          </td>
          <td style=\"width: 30%;\">
            <div class=\"card alert-card\">
              <p class=\"card-title\">Reference</p>
              <p class=\"card-copy\">Invoice {invoice_number}</p>
            </div>
          </td>
        </tr>
      </table>

      <div class=\"table-shell\">
        <table>
          <thead>
            <tr>
              <th>Description</th>
              <th class=\"num\">Qty</th>
              <th class=\"num\">Unit Price</th>
              <th class=\"num\">Amount</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </div>

      <div class=\"totals-wrap\">
        <div class=\"totals-card\">
          <table>
            <tr><td>Subtotal</td><td class=\"num\">{NAIRA_SIGN}{_money(invoice.subtotal)}</td></tr>
            {f'<tr><td>Discount</td><td class="num">-{NAIRA_SIGN}{_money(invoice.discount_amount)}</td></tr>' if invoice.discount_amount else ""}
            <tr><td>{tax_label}</td><td class=\"num\">{NAIRA_SIGN}{_money(invoice.tax_total)}</td></tr>
            <tr class=\"grand-total\"><td>Total</td><td class=\"num\">{NAIRA_SIGN}{_money(invoice.total)}</td></tr>
          </table>
        </div>
      </div>

      {bank_markup}

      <div class=\"memo\">
        <strong>Memo:</strong> {memo}
      </div>

      <div class=\"footer\">
        <div>Prepared by {company_name}</div>
        <div>Thank you for your business.</div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_pdf(lines: list[str]) -> bytes:
    """Build a minimal single-page PDF with plain text lines."""
    safe_lines = [_pdf_escape(line)[:180] for line in lines[:55]]
    y = 800
    content_lines = ["BT", "/F1 11 Tf"]
    for line in safe_lines:
        content_lines.append(f"1 0 0 1 50 {y} Tm ({line}) Tj")
        y -= 14
        if y < 40:
            break
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


_TEXT_FALLBACK_MAX_ITEMS = 30


def _truncation_marker(hidden_count: int) -> str:
    return f"... and {hidden_count} more line items (see itemised statement)"


def _render_invoice_text_lines(
    invoice: Invoice, db: Session | None = None
) -> list[str]:
    # The minimal text PDF is encoded latin-1, so use "NGN" (no naira glyph).
    lines = [line for line in (invoice.lines or []) if getattr(line, "is_active", True)]
    out = [
        f"Invoice {invoice.invoice_number or invoice.id}",
        f"Status: {invoice.status.value if invoice.status else 'draft'}",
        f"Issued: {_format_date(invoice.issued_at)}    Due: {_format_date(invoice.due_at)}",
        f"Billed To: {_display_account_name(invoice)}",
        f"Email: {(getattr(invoice.account, 'email', None) or '').strip() or 'N/A'}",
        "",
        "Items:",
    ]
    if not lines:
        out.append("- No line items")
    else:
        for item in lines[:_TEXT_FALLBACK_MAX_ITEMS]:
            qty = str(item.quantity or Decimal("0"))
            unit = _money(item.unit_price)
            amount = _money(item.amount)
            desc = str(item.description or "Line item")
            out.append(f"- {desc} | qty {qty} | unit NGN {unit} | amount NGN {amount}")
        if len(lines) > _TEXT_FALLBACK_MAX_ITEMS:
            out.append(f"- {_truncation_marker(len(lines) - _TEXT_FALLBACK_MAX_ITEMS)}")
    out.extend(
        [
            "",
            f"Subtotal: NGN {_money(invoice.subtotal)}",
            *(
                [f"Discount: -NGN {_money(invoice.discount_amount)}"]
                if invoice.discount_amount
                else []
            ),
            f"Tax: NGN {_money(invoice.tax_total)}",
            f"Total: NGN {_money(invoice.total)}",
            f"Balance Due: NGN {_money(invoice.balance_due)}",
        ]
    )
    if db is not None:
        bank_details = invoice_bank_details_service.get_invoice_bank_details(
            db, currency=invoice.currency
        )
        if bank_details:
            out.extend(
                [
                    "",
                    "Bank Details:",
                    f"Bank Name: {bank_details.get('bank_name', '')}",
                    f"Account Name: {bank_details.get('account_name', '')}",
                    f"Account Number: {bank_details.get('account_number', '')}",
                ]
            )
            sort_code = (bank_details.get("sort_code") or "").strip()
            if sort_code:
                out.append(f"Sort Code: {sort_code}")
    out.append(f"Memo: {(invoice.memo or '').strip() or '-'}")
    return out


def _build_branded_fallback_pdf(db: Session, invoice: Invoice) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return _build_simple_pdf(_render_invoice_text_lines(invoice, db))

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

    # DejaVu Sans carries the naira glyph; the PIL bitmap default does not.
    naira = NAIRA_SIGN if _font_path(False) else "NGN "

    width, height = 1240, 1754
    margin_x = 72
    page = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(page)

    from app.services.brand_profiles import resolve_brand

    account_id = getattr(getattr(invoice, "account", None), "id", None)
    brand = resolve_brand(db, subscriber_id=account_id)
    green_900 = brand.primary_color
    green_700 = brand.secondary_color
    green_50 = "#f0fdf4"
    red_700 = "#b91c1c"
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

    company_info = _branded_company_info(
        company_info_service.get_company_info(db), brand
    )
    company_name = brand.legal_name
    customer_name = _display_account_name(invoice)
    logo_image = _decode_logo_image(db, brand.logo_url)
    if logo_image is not None:
        logo = logo_image.copy()
        logo.thumbnail((220, 100))
        logo_bg = Image.new(
            "RGBA", (logo.width + 28, logo.height + 20), (255, 255, 255, 255)
        )
        page.paste(logo_bg.convert("RGB"), (margin_x - 10, 78))
        page.paste(logo, (margin_x + 4, 88), logo)
    else:
        draw.rounded_rectangle(
            (margin_x, 78, margin_x + 88, 166), radius=22, fill="#ffffff"
        )
        draw.text(
            (margin_x + 28, 96),
            company_name[:1].upper(),
            font=title_font,
            fill=green_900,
        )

    panel_x = width - 380
    draw.rounded_rectangle((panel_x, 82, width - 84, 252), radius=22, fill="#ffffff")
    invoice_number = invoice.invoice_number or str(invoice.id)
    draw.text((panel_x + 28, 106), "INVOICE", font=label_font, fill=green_900)
    draw.text(
        (panel_x + 28, 138), f"#{invoice_number}", font=heading_font, fill=slate_900
    )
    draw.text(
        (panel_x + 28, 188),
        f"Issued: {_format_date(invoice.issued_at)}",
        font=small_font,
        fill=slate_700,
    )
    draw.text(
        (panel_x + 28, 214),
        f"Due: {_format_date(invoice.due_at)}",
        font=small_font,
        fill=slate_700,
    )
    customer_name = _truncate_text(customer_name, 42)
    draw.text(
        (margin_x, 292),
        customer_name,
        font=heading_font,
        fill="#ffffff",
    )

    y = 390
    card_height = 138
    card_gap = 22
    card_width = (width - (margin_x * 2) - card_gap * 2) // 3
    cards = [
        (
            "Billed To",
            _truncate_text(_display_account_name(invoice), 28),
            getattr(invoice.account, "email", None) or "N/A",
            "#ffffff",
            slate_900,
        ),
        (
            "Balance Due",
            f"{naira}{_money(invoice.balance_due)}",
            f"Currency: {invoice.currency or 'NGN'}",
            green_50,
            green_900,
        ),
        (
            "Reference",
            f"Invoice {invoice_number}",
            invoice.status.value.replace("_", " ").title()
            if invoice.status
            else "Draft",
            "#fff5f5",
            red_700,
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
        draw.text((left + 20, y + 18), title.upper(), font=label_font, fill=accent)
        draw.text(
            (left + 20, y + 54),
            line1,
            font=value_font if index == 1 else body_font,
            fill=slate_900 if index != 1 else green_900,
        )
        draw.text((left + 20, y + 94), line2, font=small_font, fill=slate_700)

    table_top = 572
    table_left = margin_x
    table_right = width - margin_x
    draw.rounded_rectangle(
        (table_left, table_top, table_right, table_top + 560),
        radius=22,
        fill="#ffffff",
        outline=slate_200,
    )
    draw.rounded_rectangle(
        (table_left, table_top, table_right, table_top + 66), radius=22, fill=green_900
    )
    columns = [table_left + 24, table_left + 650, table_left + 800, table_left + 980]
    headers = ["Description", "Qty", "Unit Price", "Amount"]
    for header, col in zip(headers, columns):
        draw.text(
            (col, table_top + 20), header.upper(), font=label_font, fill="#ffffff"
        )

    line_items = [
        line for line in (invoice.lines or []) if getattr(line, "is_active", True)
    ]
    if not line_items:
        line_items = [
            type(
                "FallbackLine",
                (),
                {
                    "description": "No line items",
                    "quantity": "",
                    "unit_price": "",
                    "amount": "",
                },
            )()
        ]
    row_y = table_top + 86
    row_height = 64
    for index, line in enumerate(line_items[:7]):
        if index % 2 == 1:
            draw.rounded_rectangle(
                (table_left + 6, row_y - 8, table_right - 6, row_y + row_height - 10),
                radius=12,
                fill=green_50,
            )
        draw.text(
            (columns[0], row_y),
            str(line.description or "Line item"),
            font=body_font,
            fill=slate_900,
        )
        draw.text(
            (columns[1], row_y),
            str(line.quantity or ""),
            font=body_font,
            fill=slate_700,
        )
        draw.text(
            (columns[2], row_y),
            f"{naira}{_money(getattr(line, 'unit_price', None))}"
            if getattr(line, "unit_price", None) != ""
            else "",
            font=body_font,
            fill=slate_700,
        )
        draw.text(
            (columns[3], row_y),
            f"{naira}{_money(getattr(line, 'amount', None))}"
            if getattr(line, "amount", None) != ""
            else "",
            font=body_font,
            fill=slate_900,
        )
        row_y += row_height
    if len(line_items) > 7:
        draw.text(
            (columns[0], row_y),
            _truncation_marker(len(line_items) - 7),
            font=small_font,
            fill=slate_500,
        )

    totals_left = width - 430
    totals_top = 1180
    draw.rounded_rectangle(
        (totals_left, totals_top, width - margin_x, totals_top + 172),
        radius=20,
        fill="#ffffff",
        outline=slate_200,
    )
    totals = [
        ("Subtotal", f"{naira}{_money(invoice.subtotal)}"),
        *(
            [("Discount", f"-{naira}{_money(invoice.discount_amount)}")]
            if invoice.discount_amount
            else []
        ),
        ("Tax", f"{naira}{_money(invoice.tax_total)}"),
        ("Total", f"{naira}{_money(invoice.total)}"),
    ]
    for index, (label, value) in enumerate(totals):
        ty = totals_top + 22 + (index * 44)
        draw.text(
            (totals_left + 22, ty),
            label,
            font=body_font,
            fill=slate_700 if label != "Total" else green_900,
        )
        draw.text(
            (totals_left + 210, ty),
            value,
            font=value_font if label == "Total" else body_font,
            fill=green_900 if label == "Total" else slate_900,
        )

    bank_details = invoice_bank_details_service.get_invoice_bank_details(
        db, currency=invoice.currency
    )
    if bank_details:
        bank_left = margin_x
        bank_top = totals_top
        bank_right = totals_left - 28
        draw.rounded_rectangle(
            (bank_left, bank_top, bank_right, bank_top + 172),
            radius=20,
            fill="#ffffff",
            outline=slate_200,
        )
        draw.text(
            (bank_left + 22, bank_top + 18),
            "BANK DETAILS",
            font=label_font,
            fill=green_900,
        )
        bank_lines = [
            f"Bank: {bank_details.get('bank_name', '')}",
            f"Account Name: {bank_details.get('account_name', '')}",
            f"Account Number: {bank_details.get('account_number', '')}",
        ]
        sort_code = (bank_details.get("sort_code") or "").strip()
        if sort_code:
            bank_lines.append(f"Sort Code: {sort_code}")
        for index, line in enumerate(bank_lines[:4]):
            draw.text(
                (bank_left + 22, bank_top + 54 + (index * 28)),
                line,
                font=small_font,
                fill=slate_700,
            )

    memo_top = 1388
    draw.rounded_rectangle(
        (margin_x, memo_top, width - margin_x, memo_top + 150),
        radius=18,
        fill="#fff5f5",
        outline="#fecaca",
    )
    draw.rectangle((margin_x, memo_top, margin_x + 12, memo_top + 150), fill=red_700)
    draw.text((margin_x + 28, memo_top + 20), "MEMO", font=label_font, fill=red_700)
    draw.text(
        (margin_x + 28, memo_top + 58),
        (invoice.memo or "").strip() or "-",
        font=body_font,
        fill=slate_900,
    )

    footer_y = 1612
    draw.text(
        (margin_x, footer_y),
        f"Prepared by {company_name}",
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


def get_latest_export(db: Session, invoice_id: str) -> InvoicePdfExport | None:
    stmt = (
        select(InvoicePdfExport)
        .where(InvoicePdfExport.invoice_id == invoice_id)
        .order_by(InvoicePdfExport.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def _is_export_fresh(invoice: Invoice, export: InvoicePdfExport) -> bool:
    if export.status != InvoicePdfExportStatus.completed:
        return False
    if not export.completed_at:
        return False
    if export.completed_at < INVOICE_PDF_TEMPLATE_REFRESHED_AT:
        return False
    invoice_updated = invoice.updated_at or invoice.created_at
    if invoice_updated and export.completed_at < invoice_updated:
        return False
    return True


def is_export_cache_valid(
    db: Session, invoice: Invoice, export: InvoicePdfExport | None
) -> bool:
    if not export:
        return False
    if not _is_export_fresh(invoice, export):
        return False
    return export_file_exists(db, export)


def queue_export(
    db: Session,
    invoice_id: str,
    requested_by_id: str | None = None,
    *,
    force_new: bool = False,
) -> InvoicePdfExport:
    requested_by_id = _normalize_requested_by_id(db, requested_by_id)
    latest = get_latest_export(db, invoice_id)
    invoice = db.get(Invoice, invoice_id)

    if (
        not force_new
        and latest
        and invoice
        and latest.status == InvoicePdfExportStatus.completed
        and is_export_cache_valid(db, invoice, latest)
    ):
        return latest

    if latest and latest.status == InvoicePdfExportStatus.processing:
        return latest

    from app.services.queue_adapter import enqueue_task

    if latest and latest.status == InvoicePdfExportStatus.queued:
        if not latest.celery_task_id:
            dispatch = enqueue_task(
                "app.tasks.invoice_pdf.generate_invoice_pdf_export",
                args=[str(latest.id)],
                correlation_id=f"invoice_pdf_export:{latest.id}",
                source="billing_invoice_pdf",
                actor_id=requested_by_id,
            )
            latest.celery_task_id = dispatch.task_id
            db.commit()
            db.refresh(latest)
        return latest

    export = InvoicePdfExport(
        invoice_id=invoice_id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=requested_by_id,
    )
    db.add(export)
    db.commit()
    db.refresh(export)

    dispatch = enqueue_task(
        "app.tasks.invoice_pdf.generate_invoice_pdf_export",
        args=[str(export.id)],
        correlation_id=f"invoice_pdf_export:{export.id}",
        source="billing_invoice_pdf",
        actor_id=requested_by_id,
    )
    export.celery_task_id = dispatch.task_id
    db.commit()
    db.refresh(export)
    return export


def generate_export_now(
    db: Session,
    *,
    invoice_id: str,
    requested_by_id: str | None = None,
    force_new: bool = False,
) -> InvoicePdfExport:
    requested_by_id = _normalize_requested_by_id(db, requested_by_id)
    invoice = db.get(Invoice, invoice_id)
    latest = get_latest_export(db, invoice_id)

    if (
        not force_new
        and latest
        and invoice
        and latest.status == InvoicePdfExportStatus.completed
        and is_export_cache_valid(db, invoice, latest)
    ):
        return latest

    export = InvoicePdfExport(
        invoice_id=invoice_id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=requested_by_id,
    )
    db.add(export)
    db.commit()
    db.refresh(export)

    process_export(str(export.id))
    db.expire_all()
    refreshed = db.get(InvoicePdfExport, export.id)
    return refreshed or export


def _get_cache_metrics(db: Session) -> dict[str, int]:
    default = {"hits": 0, "misses": 0, "generated": 0, "regenerated": 0}
    try:
        setting = domain_settings_service.billing_settings.get_by_key(
            db, INVOICE_PDF_CACHE_METRICS_KEY
        )
    except Exception:
        return default
    if isinstance(setting.value_json, dict):
        out = default.copy()
        for key in out:
            try:
                out[key] = int(setting.value_json.get(key) or 0)
            except (TypeError, ValueError):
                out[key] = 0
        return out
    return default


def _save_cache_metrics(db: Session, metrics: dict[str, int]) -> None:
    domain_settings_service.billing_settings.upsert_by_key(
        db,
        INVOICE_PDF_CACHE_METRICS_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=metrics,
            value_text=None,
            is_secret=False,
            is_active=True,
        ),
    )


def record_cache_hit(db: Session) -> None:
    metrics = _get_cache_metrics(db)
    metrics["hits"] = int(metrics.get("hits", 0)) + 1
    _save_cache_metrics(db, metrics)


def record_cache_miss(db: Session) -> None:
    metrics = _get_cache_metrics(db)
    metrics["misses"] = int(metrics.get("misses", 0)) + 1
    _save_cache_metrics(db, metrics)


def record_generated(db: Session, *, regenerated: bool = False) -> None:
    metrics = _get_cache_metrics(db)
    metrics["generated"] = int(metrics.get("generated", 0)) + 1
    if regenerated:
        metrics["regenerated"] = int(metrics.get("regenerated", 0)) + 1
    _save_cache_metrics(db, metrics)


def get_cache_dashboard_stats(db: Session) -> dict[str, Any]:
    completed = (
        db.query(InvoicePdfExport)
        .filter(InvoicePdfExport.status == InvoicePdfExportStatus.completed)
        .all()
    )
    unique_invoice_ids = {str(row.invoice_id) for row in completed}
    total_size_bytes = sum(int(row.file_size_bytes or 0) for row in completed)
    oldest_cached = min(
        (row.completed_at for row in completed if row.completed_at), default=None
    )

    durations = [
        (row.completed_at - row.created_at).total_seconds()
        for row in completed
        if row.completed_at and row.created_at and row.completed_at >= row.created_at
    ]
    avg_generation_seconds = (sum(durations) / len(durations)) if durations else 0.0

    metrics = _get_cache_metrics(db)
    hits = int(metrics.get("hits", 0))
    misses = int(metrics.get("misses", 0))
    total_lookups = hits + misses
    hit_rate_pct = (hits / total_lookups * 100.0) if total_lookups > 0 else 0.0

    return {
        "total_cached_invoices": len(unique_invoice_ids),
        "cache_size_bytes": total_size_bytes,
        "oldest_cached_at": oldest_cached,
        "avg_generation_seconds": avg_generation_seconds,
        "hit_rate_pct": hit_rate_pct,
        "hits": hits,
        "misses": misses,
    }


def _invalidate_export_file(db: Session, export: InvoicePdfExport) -> None:
    record = file_uploads.get_active_entity_file(
        db, "invoice_pdf_export", str(export.id)
    )
    if record:
        file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
    export.file_path = None
    export.file_size_bytes = None
    export.completed_at = None
    export.status = InvoicePdfExportStatus.failed
    export.error = "Cache invalidated"


def regenerate_invoice_cache(
    db: Session,
    *,
    invoice_id: str,
    requested_by_id: str | None = None,
) -> InvoicePdfExport:
    exports = (
        db.query(InvoicePdfExport)
        .filter(InvoicePdfExport.invoice_id == invoice_id)
        .order_by(InvoicePdfExport.created_at.desc())
        .all()
    )
    for row in exports:
        if row.status == InvoicePdfExportStatus.completed and row.file_path:
            _invalidate_export_file(db, row)
    db.commit()
    export = queue_export(
        db,
        invoice_id=invoice_id,
        requested_by_id=requested_by_id,
        force_new=True,
    )
    record_generated(db, regenerated=True)
    return export


def clear_cache(
    db: Session,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    account_id: str | None = None,
) -> dict[str, int]:
    query = db.query(InvoicePdfExport).join(
        Invoice, Invoice.id == InvoicePdfExport.invoice_id
    )
    query = query.filter(InvoicePdfExport.status == InvoicePdfExportStatus.completed)
    if date_from:
        query = query.filter(InvoicePdfExport.completed_at >= date_from)
    if date_to:
        query = query.filter(InvoicePdfExport.completed_at < date_to)
    if account_id:
        query = query.filter(Invoice.account_id == account_id)

    rows = query.all()
    invalidated = 0
    bytes_cleared = 0
    for row in rows:
        bytes_cleared += int(row.file_size_bytes or 0)
        _invalidate_export_file(db, row)
        invalidated += 1
    db.commit()
    return {"invalidated": invalidated, "bytes_cleared": bytes_cleared}


def export_file_exists(db: Session, export: InvoicePdfExport | None) -> bool:
    if (
        not export
        or export.status != InvoicePdfExportStatus.completed
        or not export.file_path
    ):
        return False
    record = file_uploads.get_active_entity_file(
        db, "invoice_pdf_export", str(export.id)
    )
    if record:
        try:
            file_uploads.stream_file(record)
            return True
        except ObjectNotFoundError:
            return False
    local_path = Path(export.file_path)
    if local_path.exists():
        return True
    try:
        return get_s3_storage().exists(export.file_path)
    except Exception:
        return False


def maybe_finalize_stalled_export(
    db: Session,
    export: InvoicePdfExport | None,
) -> InvoicePdfExport | None:
    """Best-effort inline completion for stale or missing export artifacts."""
    if not export:
        return None

    should_process_inline = False
    if export.status == InvoicePdfExportStatus.completed and not export_file_exists(
        db, export
    ):
        should_process_inline = True
    elif export.status in (
        InvoicePdfExportStatus.queued,
        InvoicePdfExportStatus.processing,
    ):
        marker = export.updated_at or export.created_at
        if marker:
            age_seconds = (datetime.now(UTC) - marker).total_seconds()
            should_process_inline = age_seconds >= STALE_EXPORT_SECONDS

    if not should_process_inline:
        return export

    logger.info(
        "INLINE_INVOICE_PDF_FALLBACK export_id=%s status=%s",
        export.id,
        export.status.value,
    )
    process_export(str(export.id))
    db.expire_all()
    return db.get(InvoicePdfExport, export.id)


def download_filename(invoice: Invoice) -> str:
    return f"invoice-{_safe_invoice_number(invoice)}.pdf"


def _invoice_owner_subscriber_id(invoice: Invoice):
    account = getattr(invoice, "account", None)
    if not account:
        return None
    return (
        getattr(account, "id", None)
        if getattr(account, "category", None) == SubscriberCategory.business
        else None
    )


def _build_pdf_bytes(db: Session, invoice: Invoice) -> bytes:
    html_content = _render_invoice_html(invoice, db)
    try:
        _ensure_weasyprint_pydyf_compat()
        from weasyprint import HTML

        return HTML(string=html_content).write_pdf()
    except Exception as exc:
        logger.info(
            "WeasyPrint export failed for invoice %s; using branded PDF fallback: %s",
            invoice.id,
            exc,
        )
        return _build_branded_fallback_pdf(db, invoice)


def _stream_local_file(path: Path) -> StreamResult:
    def _chunks() -> Iterator[bytes]:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamResult(
        chunks=_chunks(),
        content_type="application/pdf",
        content_length=path.stat().st_size,
    )


def stream_export(db: Session, export: InvoicePdfExport) -> StreamResult:
    if not export.file_path:
        raise ObjectNotFoundError("missing export file path")
    local_path = Path(export.file_path)
    if local_path.exists():
        return _stream_local_file(local_path)

    record = file_uploads.get_active_entity_file(
        db, "invoice_pdf_export", str(export.id)
    )
    if record:
        return file_uploads.stream_file(record)

    return get_s3_storage().stream(export.file_path)


def process_export(export_id: str) -> dict[str, Any]:
    db = SessionLocal()
    export: InvoicePdfExport | None = None
    started_at = datetime.now(UTC)
    try:
        export = db.get(InvoicePdfExport, export_id)
        if not export:
            return {"status": "missing", "export_id": export_id}

        export.status = InvoicePdfExportStatus.processing
        export.error = None
        export.completed_at = None
        db.commit()

        invoice = db.get(Invoice, export.invoice_id)
        if not invoice:
            export.status = InvoicePdfExportStatus.failed
            export.error = "Invoice not found"
            export.completed_at = datetime.now(UTC)
            db.commit()
            return {
                "status": "failed",
                "reason": "invoice_not_found",
                "export_id": export_id,
            }

        # Load related data used by renderer.
        _ = invoice.account
        _ = invoice.lines

        existing_record = file_uploads.get_active_entity_file(
            db, "invoice_pdf_export", str(export.id)
        )
        if existing_record:
            file_uploads.soft_delete(
                db=db, file=existing_record, hard_delete_object=True
            )

        pdf_bytes = _build_pdf_bytes(db, invoice)
        uploaded = file_uploads.upload(
            db=db,
            domain="generated_docs",
            entity_type="invoice_pdf_export",
            entity_id=str(export.id),
            original_filename=download_filename(invoice),
            content_type="application/pdf",
            data=pdf_bytes,
            uploaded_by=str(export.requested_by_id) if export.requested_by_id else None,
            owner_subscriber_id=_invoice_owner_subscriber_id(invoice),
        )

        export.status = InvoicePdfExportStatus.completed
        export.file_path = uploaded.storage_key_or_relative_path
        export.file_size_bytes = uploaded.file_size
        export.completed_at = datetime.now(UTC)
        export.error = None
        db.commit()
        record_generated(db)
        return {
            "status": "completed",
            "export_id": export_id,
            "invoice_id": str(invoice.id),
            "file_path": uploaded.storage_key_or_relative_path,
            "duration_ms": int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        }
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback PDF export transaction.")

        try:
            failed_export = db.get(InvoicePdfExport, export_id)
            if failed_export is not None:
                failed_export.status = InvoicePdfExportStatus.failed
                failed_export.error = str(exc)
                failed_export.completed_at = datetime.now(UTC)
                db.commit()
        except Exception:
            logger.exception("Failed to persist invoice PDF export failure state.")
            try:
                db.rollback()
            except Exception:
                logger.exception("Failed to rollback after failure-state write error.")
        return {
            "status": "failed",
            "export_id": export_id,
            "reason": str(exc),
        }
    finally:
        db.close()
