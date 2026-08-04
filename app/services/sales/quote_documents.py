"""Branded, immutable PDF artifacts for customer-facing Quotes."""

from __future__ import annotations

import base64
import hashlib
import html
import inspect
import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.party import PartyContactPoint, PartyContactPointType
from app.models.sales import Quote, QuotePdfExport
from app.models.stored_file import StoredFile
from app.services.audit_adapter import stage_audit_event
from app.services.brand_profiles import ResolvedBrand, resolve_brand
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

_GENERATE_QUOTE_PDF = OwnerCommandDefinition(
    owner="sales.quote_documents",
    concern="immutable branded Quote PDF generation",
    name="generate_quote_pdf",
)


class QuoteDocumentError(DomainError):
    """Stable failure raised by the Quote document owner."""


@dataclass(frozen=True)
class GenerateQuotePdfCommand:
    context: CommandContext
    quote_id: UUID


@dataclass(frozen=True)
class GenerateQuotePdfOutcome:
    export_id: UUID
    quote_id: UUID
    snapshot_fingerprint: str
    filename: str
    replayed: bool


@dataclass(frozen=True)
class QuoteRecipient:
    contact_point_id: UUID
    email: str
    display_name: str


def _error(suffix: str, message: str, **details: object) -> QuoteDocumentError:
    return QuoteDocumentError(
        code=f"sales.quote_documents.{suffix}",
        message=message,
        details=details,
    )


def _uuid_or_none(value: str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _money(value: object) -> str:
    return f"{Decimal(str(value or '0')).quantize(Decimal('0.01')):,.2f}"


def _date(value: datetime | None) -> str:
    return value.strftime("%d %B %Y") if value else "—"


def download_filename(quote: Quote | QuotePdfExport) -> str:
    quote_id = quote.quote_id if isinstance(quote, QuotePdfExport) else quote.id
    return f"quote-{quote_id}.pdf"


def resolve_quote_recipient(db: Session, quote: Quote) -> QuoteRecipient | None:
    lead = quote.lead
    party = lead.party if lead is not None else None
    if party is None:
        return None
    contact = db.scalars(
        select(PartyContactPoint)
        .where(
            PartyContactPoint.party_id == party.id,
            PartyContactPoint.channel_type == PartyContactPointType.email.value,
            PartyContactPoint.is_active.is_(True),
        )
        .order_by(
            PartyContactPoint.is_primary.desc(),
            PartyContactPoint.created_at.asc(),
            PartyContactPoint.id.asc(),
        )
        .limit(1)
    ).first()
    if contact is None:
        return None
    email = str(contact.normalized_value or contact.display_value or "").strip()
    if not email:
        return None
    return QuoteRecipient(
        contact_point_id=contact.id,
        email=email,
        display_name=party.display_name,
    )


def _resolved_brand(db: Session, quote: Quote) -> ResolvedBrand:
    if quote.subscriber_id is not None:
        return resolve_brand(db, subscriber_id=quote.subscriber_id)
    lead = quote.lead
    party = lead.party if lead is not None else None
    organization_id = (
        party.organization_profile.id
        if party is not None and party.organization_profile is not None
        else None
    )
    reseller_id = (
        party.reseller_profile.id
        if party is not None and party.reseller_profile is not None
        else None
    )
    return resolve_brand(
        db,
        organization_id=organization_id,
        reseller_id=reseller_id,
    )


def _logo_data_uri(db: Session, logo_url: str) -> str | None:
    value = str(logo_url or "").strip()
    if not value:
        return None
    if value.startswith("data:"):
        return value
    if value.startswith("/branding/assets/"):
        from app.services import branding_storage

        file_id = branding_storage.file_id_from_branding_url(value)
        record = db.get(StoredFile, file_id) if file_id else None
        if record is None or record.is_deleted:
            return None
        content = b"".join(file_uploads.stream_file(record).chunks)
        mime = record.content_type or "image/png"
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    if value.startswith("/static/"):
        path = Path(value.lstrip("/"))
        if not path.exists():
            return None
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return (
            f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        )
    return value


def _snapshot(db: Session, quote: Quote) -> tuple[dict[str, object], str | None]:
    recipient = resolve_quote_recipient(db, quote)
    brand = _resolved_brand(db, quote)
    logo_src = _logo_data_uri(db, brand.logo_url)
    metadata = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    install_value = metadata.get("install")
    install = install_value if isinstance(install_value, dict) else {}
    lines = sorted(quote.line_items, key=lambda item: (item.created_at, str(item.id)))
    payload: dict[str, object] = {
        "quote_id": str(quote.id),
        "status": quote.status,
        "currency": quote.currency,
        "created_at": quote.created_at.isoformat(),
        "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
        "customer_name": recipient.display_name if recipient else "Customer",
        "install_address": str(install.get("address") or "").strip() or None,
        "subtotal": str(quote.subtotal or Decimal("0.00")),
        "tax_rate": str(quote.tax_rate) if quote.tax_rate is not None else None,
        "tax_total": str(quote.tax_total or Decimal("0.00")),
        "total": str(quote.total or Decimal("0.00")),
        "lines": [
            {
                "id": str(item.id),
                "description": item.description,
                "quantity": str(item.quantity or Decimal("0")),
                "unit_price": str(item.unit_price or Decimal("0.00")),
                "discount_percent": str(item.discount_percent or Decimal("0.00")),
                "amount": str(item.amount or Decimal("0.00")),
            }
            for item in lines
        ],
        "brand": brand.to_dict(),
        "logo_digest": hashlib.sha256((logo_src or "").encode()).hexdigest(),
    }
    return payload, logo_src


def _fingerprint(snapshot: dict[str, object]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _ensure_weasyprint_compat() -> None:
    try:
        import pydyf
    except Exception:
        return
    signature = inspect.signature(pydyf.PDF.__init__)
    if len(signature.parameters) != 1 or getattr(
        pydyf.PDF, "_dotmac_weasyprint_compat", False
    ):
        return
    original_pdf = cast(type[Any], pydyf.PDF)

    def _compat_init(self: Any, version: Any = None, identifier: Any = None) -> None:
        original_pdf.__init__(self)
        self.version = version or b"1.7"
        self.identifier = identifier

    pydyf.PDF = type(
        "CompatPDF",
        (original_pdf,),
        {"_dotmac_weasyprint_compat": True, "__init__": _compat_init},
    )


def _render_html(snapshot: dict[str, object], logo_src: str | None) -> str:
    brand = cast(dict[str, object], snapshot["brand"])
    address = cast(dict[str, str], brand.get("legal_address") or {})
    lines = cast(list[dict[str, str]], snapshot["lines"])
    currency = html.escape(str(snapshot["currency"]))
    rows = (
        "".join(
            "<tr>"
            f"<td>{html.escape(line['description'])}</td>"
            f"<td class='num'>{html.escape(line['quantity'])}</td>"
            f"<td class='num'>{currency} {_money(line['unit_price'])}</td>"
            f"<td class='num'>{html.escape(line['discount_percent'])}%</td>"
            f"<td class='num'>{currency} {_money(line['amount'])}</td>"
            "</tr>"
            for line in lines
        )
        or "<tr><td colspan='5'>No line items</td></tr>"
    )
    company_lines = [
        str(brand.get("legal_name") or brand.get("name") or "Dotmac"),
        str(address.get("street1") or ""),
        str(address.get("street2") or ""),
        " ".join(
            value
            for value in (address.get("city", ""), address.get("postal_code", ""))
            if value
        ),
        str(address.get("country") or ""),
        str(brand.get("support_email") or ""),
        str(brand.get("support_phone") or ""),
    ]
    company_markup = "<br>".join(html.escape(line) for line in company_lines if line)
    logo = (
        f"<img class='logo' src='{html.escape(logo_src)}' alt='Company logo'>"
        if logo_src
        else ""
    )
    primary = html.escape(str(brand.get("primary_color") or "#008000"))
    secondary = html.escape(str(brand.get("secondary_color") or "#FF0000"))
    status = str(snapshot["status"] or "draft").upper()
    install = str(snapshot.get("install_address") or "").strip()
    install_markup = (
        f"<p><strong>Installation address:</strong> {html.escape(install)}</p>"
        if install
        else ""
    )
    expires_raw = snapshot.get("expires_at")
    expires = datetime.fromisoformat(str(expires_raw)) if expires_raw else None
    created = datetime.fromisoformat(str(snapshot["created_at"]))
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><style>
@page {{ size: A4; margin: 15mm; }}
body {{ font-family: DejaVu Sans, Arial, sans-serif; color:#0f172a; font-size:11px; }}
.top {{ border-top:8px solid {primary}; padding-top:18px; display:flex; justify-content:space-between; }}
.logo {{ max-width:170px; max-height:70px; }}
.company {{ text-align:right; color:#475569; line-height:1.5; }}
h1 {{ color:{primary}; font-size:27px; margin:28px 0 4px; }}
.status {{ color:{secondary}; font-weight:700; letter-spacing:.08em; }}
.meta {{ display:flex; justify-content:space-between; margin:22px 0; padding:14px; background:#f8fafc; }}
table {{ width:100%; border-collapse:collapse; margin-top:22px; }}
th {{ background:{primary}; color:white; text-align:left; padding:9px; }}
td {{ border-bottom:1px solid #e2e8f0; padding:9px; }}
.num {{ text-align:right; }}
.totals {{ margin:22px 0 0 auto; width:45%; }}
.totals div {{ display:flex; justify-content:space-between; padding:6px 0; }}
.grand {{ border-top:2px solid {primary}; color:{primary}; font-size:15px; font-weight:700; }}
.footer {{ border-top:1px solid #e2e8f0; margin-top:34px; padding-top:12px; color:#64748b; }}
</style></head><body>
<div class='top'><div>{logo}</div><div class='company'>{company_markup}</div></div>
<h1>QUOTE</h1><div class='status'>{html.escape(status)}</div>
<div class='meta'><div><strong>Prepared for</strong><br>{html.escape(str(snapshot["customer_name"]))}</div>
<div><strong>Reference</strong><br>{html.escape(str(snapshot["quote_id"]))}<br>
<strong>Issued</strong> {_date(created)}<br><strong>Expires</strong> {_date(expires)}</div></div>
{install_markup}
<table><thead><tr><th>Description</th><th class='num'>Qty</th><th class='num'>Unit price</th><th class='num'>Discount</th><th class='num'>Amount</th></tr></thead><tbody>{rows}</tbody></table>
<div class='totals'><div><span>Subtotal</span><span>{currency} {_money(snapshot["subtotal"])}</span></div>
<div><span>Tax</span><span>{currency} {_money(snapshot["tax_total"])}</span></div>
<div class='grand'><span>Total</span><span>{currency} {_money(snapshot["total"])}</span></div></div>
<div class='footer'>This document was generated by {html.escape(str(brand.get("legal_name") or brand.get("name") or "Dotmac"))}.</div>
</body></html>"""


def _render_pdf(snapshot: dict[str, object], logo_src: str | None) -> bytes:
    try:
        from weasyprint import HTML
    except Exception as exc:
        raise _error(
            "renderer_unavailable", "Quote PDF rendering is unavailable"
        ) from exc
    _ensure_weasyprint_compat()
    content = HTML(string=_render_html(snapshot, logo_src)).write_pdf()
    if not content.startswith(b"%PDF-"):
        raise _error("invalid_pdf", "Quote PDF rendering returned invalid content")
    return content


def _locked_quote(db: Session, quote_id: UUID) -> Quote:
    quote = db.scalars(
        select(Quote)
        .where(Quote.id == quote_id, Quote.is_active.is_(True))
        .options(
            selectinload(Quote.line_items),
            selectinload(Quote.lead),
        )
        .with_for_update()
    ).one_or_none()
    if quote is None:
        raise _error("quote_not_found", "Quote not found")
    return quote


def stage_quote_pdf_export(
    db: Session,
    *,
    quote: Quote,
    requested_by_id: UUID | None,
) -> tuple[QuotePdfExport, bool]:
    """Stage a content-addressed artifact in the caller-owned transaction."""
    if not (
        owner_command_active(db, owner="sales.quote_documents")
        or owner_command_active(db, owner="sales.quote_delivery")
    ):
        raise _error(
            "owner_command_required",
            "Quote PDF staging requires the document or delivery command owner",
        )
    snapshot, logo_src = _snapshot(db, quote)
    fingerprint = _fingerprint(snapshot)
    existing = db.scalars(
        select(QuotePdfExport).where(
            QuotePdfExport.quote_id == quote.id,
            QuotePdfExport.snapshot_fingerprint == fingerprint,
        )
    ).one_or_none()
    if existing is not None and not existing.stored_file.is_deleted:
        return existing, True

    export_id = (
        existing.id
        if existing is not None
        else uuid5(
            NAMESPACE_URL,
            f"dotmac-sub:quote-pdf:{quote.id}:{fingerprint}",
        )
    )
    pdf = _render_pdf(snapshot, logo_src)
    stored = file_uploads.stage_upload(
        db=db,
        domain="generated_docs",
        entity_type="quote_pdf_export",
        entity_id=str(export_id),
        original_filename=download_filename(quote),
        content_type="application/pdf",
        data=pdf,
        uploaded_by=None,
        owner_subscriber_id=quote.subscriber_id,
    )
    if existing is not None:
        existing.stored_file_id = stored.id
        existing.requested_by_id = requested_by_id
        export = existing
    else:
        export = QuotePdfExport(
            id=export_id,
            quote_id=quote.id,
            stored_file_id=stored.id,
            snapshot_fingerprint=fingerprint,
            snapshot=snapshot,
            requested_by_id=requested_by_id,
        )
        db.add(export)
    db.flush()
    return export, False


def generate_quote_pdf(
    db: Session, command: GenerateQuotePdfCommand
) -> GenerateQuotePdfOutcome:
    def operation() -> GenerateQuotePdfOutcome:
        quote = _locked_quote(db, command.quote_id)
        actor_id = _uuid_or_none(command.context.actor)
        export, replayed = stage_quote_pdf_export(
            db,
            quote=quote,
            requested_by_id=actor_id,
        )
        if not replayed:
            stage_audit_event(
                db,
                action="quote.pdf_exported",
                entity_type="quote",
                entity_id=str(quote.id),
                actor_type=AuditActorType.user,
                actor_id=command.context.actor,
                request_id=str(command.context.command_id),
                metadata={
                    "export_id": str(export.id),
                    "snapshot_fingerprint": export.snapshot_fingerprint,
                },
            )
            emit_event(
                db,
                EventType.quote_pdf_exported,
                {
                    "quote_id": str(quote.id),
                    "pdf_export_id": str(export.id),
                    "snapshot_fingerprint": export.snapshot_fingerprint,
                },
                actor=command.context.actor,
            )
        return GenerateQuotePdfOutcome(
            export_id=export.id,
            quote_id=quote.id,
            snapshot_fingerprint=export.snapshot_fingerprint,
            filename=download_filename(quote),
            replayed=replayed,
        )

    return execute_owner_command(
        db,
        definition=_GENERATE_QUOTE_PDF,
        context=command.context,
        operation=operation,
    )


def get_export(db: Session, export_id: UUID) -> QuotePdfExport:
    export = db.get(QuotePdfExport, export_id)
    if export is None:
        raise _error("export_not_found", "Quote PDF export not found")
    return export


def stream_export(db: Session, export: QuotePdfExport) -> StreamResult:
    record = db.get(StoredFile, export.stored_file_id)
    if record is None or record.is_deleted:
        raise _error("artifact_missing", "Quote PDF artifact is unavailable")
    return file_uploads.stream_file(record)
