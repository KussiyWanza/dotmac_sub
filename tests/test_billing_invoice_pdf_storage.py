from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    CollectionAccount,
    CollectionAccountType,
    InvoicePdfExport,
    InvoicePdfExportStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.stored_file import StoredFile
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as pdf_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services import web_system_config as web_system_config_service
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload(self, key: str, data: bytes, content_type: str | None):
        self.objects[key] = data

    def delete(self, key: str):
        self.objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objects

    def stream(self, key: str) -> StreamResult:
        payload = self.objects[key]
        return StreamResult(iter([payload]), "application/pdf", len(payload))


def _invoice(db_session, subscriber_account):
    return billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="NGN",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )


def test_process_export_uploads_invoice_pdf_to_s3_metadata(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)
    SessionLocal = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, autocommit=False
    )
    monkeypatch.setattr(pdf_service, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        pdf_service, "_build_pdf_bytes", lambda _db, _invoice: b"%PDF-1.4 bytes"
    )

    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=subscriber_account.id,
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    result = pdf_service.process_export(str(export.id))
    db_session.expire_all()
    export = db_session.get(InvoicePdfExport, export.id)
    assert export is not None

    assert result["status"] == "completed"
    assert export.status == InvoicePdfExportStatus.completed
    assert export.file_path is not None
    assert export.file_path in fake_storage.objects
    assert export.file_size_bytes == len(b"%PDF-1.4 bytes")
    record = (
        db_session.query(StoredFile)
        .filter(StoredFile.entity_type == "invoice_pdf_export")
        .filter(StoredFile.entity_id == str(export.id))
        .filter(StoredFile.is_deleted.is_(False))
        .first()
    )
    assert record is not None


def test_export_file_exists_and_stream_export_uses_s3(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)

    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        requested_by_id=subscriber_account.id,
        file_path="generated_docs/public/invoice_pdf_export/abc/file.pdf",
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    file_uploads.upload(
        db=db_session,
        domain="generated_docs",
        entity_type="invoice_pdf_export",
        entity_id=str(export.id),
        original_filename="invoice-test.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 body",
        uploaded_by=str(subscriber_account.id),
        owner_subscriber_id=None,
    )
    current = file_uploads.get_active_entity_file(
        db_session, "invoice_pdf_export", str(export.id)
    )
    assert current is not None
    export.file_path = current.storage_key_or_relative_path
    db_session.commit()
    db_session.refresh(export)

    assert pdf_service.export_file_exists(db_session, export) is True
    stream = pdf_service.stream_export(db_session, export)
    assert b"".join(stream.chunks) == b"%PDF-1.4 body"


def test_queue_export_ignores_non_subscriber_requested_by_id(
    db_session, subscriber_account, monkeypatch
):
    invoice = _invoice(db_session, subscriber_account)

    captured: dict[str, object] = {}

    def _fake_enqueue(
        task,
        *,
        args=None,
        kwargs=None,
        correlation_id=None,
        source=None,
        actor_id=None,
        **extra,
    ):
        captured["task"] = task
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["correlation_id"] = correlation_id
        captured["source"] = source
        captured["actor_id"] = actor_id
        captured["extra"] = extra
        return type("AsyncResult", (), {"id": "task-123"})()

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    export = pdf_service.queue_export(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id="87bdb5e4-d626-4541-9aff-25b04b0423a4",
    )

    assert export.requested_by_id is None
    assert captured["args"] == [str(export.id)]
    assert captured["kwargs"] is None
    assert captured["correlation_id"] == f"invoice_pdf_export:{export.id}"
    assert captured["source"] == "billing_invoice_pdf"
    assert captured["actor_id"] is None


def test_queue_export_reuses_queued_export_without_task_id_with_correlated_enqueue(
    db_session, subscriber_account, monkeypatch
):
    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=subscriber_account.id,
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    captured: dict[str, object] = {}

    def _fake_enqueue(
        task,
        *,
        args=None,
        kwargs=None,
        correlation_id=None,
        source=None,
        actor_id=None,
        **extra,
    ):
        captured["task"] = task
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["correlation_id"] = correlation_id
        captured["source"] = source
        captured["actor_id"] = actor_id
        captured["extra"] = extra
        return type("AsyncResult", (), {"id": "task-queued-1"})()

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    reused = pdf_service.queue_export(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id=str(subscriber_account.id),
    )

    assert reused.id == export.id
    assert reused.celery_task_id == "task-queued-1"
    assert captured["args"] == [str(export.id)]
    assert captured["kwargs"] is None
    assert captured["correlation_id"] == f"invoice_pdf_export:{export.id}"
    assert captured["source"] == "billing_invoice_pdf"
    assert captured["actor_id"] == str(subscriber_account.id)


def test_render_invoice_html_includes_branding_and_company_info(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account)
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.comms,
                key="sidebar_logo_url",
                value_text="data:image/png;base64,ZmFrZS1sb2dv",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_name",
                value_text="Dotmac Green ISP",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_email",
                value_text="billing@dotmac.ng",
                value_type=SettingValueType.string,
            ),
        ]
    )
    db_session.commit()

    html = pdf_service._render_invoice_html(invoice, db_session)

    assert "Dotmac Green ISP" in html
    assert "data:image/png;base64,ZmFrZS1sb2dv" in html
    assert "--green-900" in html
    assert "--red-700" in html
    assert "background: var(--green-900)" in html
    assert "customer-name" in html
    assert "margin: 42px 0 0" in html
    assert "billed-name" in html
    assert "text-overflow: ellipsis" in html
    assert "company-name" not in html
    assert "company-copy" not in html


def test_direct_bank_transfer_context_surfaces_owned_accounts(db_session):
    """The admin page shows the owner's accounts, read-only.

    Sort code used to round-trip through the settings blob this page wrote. It
    now lives on `collection_accounts`, edited via the collection-accounts CRUD,
    and this page only displays it so staff can see what customers will be told.
    """
    db_session.add(
        CollectionAccount(
            name="Dotmac Bank Transfer Account",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    context = web_system_config_service.get_direct_bank_transfer_context(db_session)

    account = context["collection_accounts"][0]
    assert account["bank_name"] == "Dotmac Bank"
    assert account["account_number"] == "0123456789"
    assert account["sort_code"] == "12-34-56"
    assert (
        context["collection_accounts_url"]
        == "/admin/settings/billing/collection-accounts"
    )


def test_invoice_detail_context_includes_bank_details(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account)
    # Bank details come from `collection_accounts`, the owner. They previously
    # came from the `direct_bank_transfer_accounts` settings blob, which was one
    # of four copies of the same fact and could name a different account from the
    # one the portal offered the same customer.
    db_session.add(
        CollectionAccount(
            name="Dotmac Bank Invoice Account",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    detail = web_billing_invoices_service.load_invoice_detail_data(
        db_session,
        invoice_id=str(invoice.id),
    )

    assert detail is not None
    assert detail["invoice_bank_details"] == {
        "bank_name": "Dotmac Bank",
        "account_name": "Dotmac Technologies Ltd",
        "account_number": "0123456789",
        "sort_code": "12-34-56",
    }


def test_render_invoice_outputs_bank_details(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account)
    # Bank details come from `collection_accounts`, the owner. They previously
    # came from the `direct_bank_transfer_accounts` settings blob, which was one
    # of four copies of the same fact and could name a different account from the
    # one the portal offered the same customer.
    db_session.add(
        CollectionAccount(
            name="Dotmac Bank Invoice Account",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    html = pdf_service._render_invoice_html(invoice, db_session)
    text_lines = pdf_service._render_invoice_text_lines(invoice, db_session)

    assert "Bank Details" in html
    assert "Dotmac Bank" in html
    assert "Dotmac Technologies Ltd" in html
    assert "0123456789" in html
    assert "12-34-56" in html
    assert "Bank Details:" in text_lines
    assert "Bank Name: Dotmac Bank" in text_lines
    assert "Sort Code: 12-34-56" in text_lines


def _add_lines(db_session, invoice, count):
    from app.models.billing import InvoiceLine

    for index in range(count):
        db_session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                description=f"Item {index + 1}",
                quantity=Decimal("1.000"),
                unit_price=Decimal("10.00"),
                amount=Decimal("10.00"),
            )
        )
    db_session.commit()
    db_session.refresh(invoice)


def test_render_invoice_html_uses_naira_sign(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account)
    _add_lines(db_session, invoice, 1)

    html = pdf_service._render_invoice_html(invoice, db_session)

    assert "₦" in html
    assert f"{pdf_service.NAIRA_SIGN}10.00" in html
    # The bare-letter naira ("N0.00" etc.) must be gone
    assert ">N0.00<" not in html and "'num'>N1" not in html


def test_text_fallback_uses_ngn_and_truncates_with_marker(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account)
    _add_lines(db_session, invoice, pdf_service._TEXT_FALLBACK_MAX_ITEMS + 2)

    lines = pdf_service._render_invoice_text_lines(invoice)

    assert any("unit NGN 10.00" in line for line in lines)
    assert "- ... and 2 more line items (see itemised statement)" in lines
    # latin-1 encodable for the minimal text PDF
    for line in lines:
        line.encode("latin-1")
    pdf_bytes = pdf_service._build_simple_pdf(lines)
    assert pdf_bytes.startswith(b"%PDF-")


def test_pil_fallback_renders_currency_and_truncation_marker(
    db_session, subscriber_account, monkeypatch
):
    from PIL import ImageDraw

    invoice = _invoice(db_session, subscriber_account)
    _add_lines(db_session, invoice, 9)

    rendered: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def _spy(self, xy, text, *args, **kwargs):
        rendered.append(str(text))
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", _spy)

    pdf_bytes = pdf_service._build_branded_fallback_pdf(db_session, invoice)

    assert pdf_bytes.startswith(b"%PDF-")
    assert "... and 2 more line items (see itemised statement)" in rendered
    assert any(pdf_service.NAIRA_SIGN in text or "NGN " in text for text in rendered)


def test_completed_export_before_template_refresh_is_stale(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        requested_by_id=subscriber_account.id,
        completed_at=pdf_service.INVOICE_PDF_TEMPLATE_REFRESHED_AT
        - timedelta(minutes=1),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert pdf_service._is_export_fresh(invoice, export) is False


def test_generate_export_now_uses_current_renderer(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)
    SessionLocal = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, autocommit=False
    )
    monkeypatch.setattr(pdf_service, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        pdf_service,
        "_build_pdf_bytes",
        lambda _db, _invoice: b"%PDF-1.4 branded-current",
    )

    invoice = _invoice(db_session, subscriber_account)
    export = pdf_service.generate_export_now(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id=str(subscriber_account.id),
    )

    assert export.status == InvoicePdfExportStatus.completed
    assert export.file_path in fake_storage.objects
    assert fake_storage.objects[export.file_path] == b"%PDF-1.4 branded-current"


def test_build_pdf_bytes_with_weasyprint_pydyf_compat(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account)

    pdf_bytes = pdf_service._build_pdf_bytes(db_session, invoice)

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1500


def test_render_invoice_html_falls_back_to_brand_legal_name(
    db_session, subscriber_account
):
    """With no company-info company_name set, the PDF uses the configured brand
    legal name (brand.json) instead of a generic placeholder."""
    from app.services.branding_config import get_brand

    invoice = _invoice(db_session, subscriber_account)
    _add_lines(db_session, invoice, 1)

    html = pdf_service._render_invoice_html(invoice, db_session)

    assert get_brand()["legal_name"] in html
    assert "Your Company" not in html
