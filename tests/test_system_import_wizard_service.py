from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

from app.models.billing import Invoice, LedgerEntry, Payment, PaymentAllocation
from app.models.catalog import BillingMode, Subscription
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import IpPool
from app.models.network_monitoring import NetworkDevice
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.services import import_runs
from app.services import web_system_import_wizard as import_wizard_service


def _stage_and_apply(db, *, module: str, raw_text: str, source_name: str):
    dry = import_runs.create_import_run(
        db,
        module=module,
        raw_text=raw_text,
        data_format="json" if source_name.endswith(".json") else "csv",
        source_name=source_name,
        dry_run=True,
    )
    dry = import_runs.process_import_run(db, dry.id)
    assert dry.status.value == "dry_run_ready"
    applied = import_runs.apply_from_dry_run(db, dry.id)
    assert applied.failed_rows == 0, [row.error_message for row in applied.rows]
    return applied


def _build_inline_xlsx(headers: list[str], rows: list[list[str]]) -> bytes:
    def _col_name(index: int) -> str:
        out = ""
        n = index + 1
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(ord("A") + rem) + out
        return out

    sheet_rows = [headers, *rows]
    row_xml: list[str] = []
    for row_idx, row in enumerate(sheet_rows, start=1):
        cells: list[str] = []
        for col_idx, value in enumerate(row):
            ref = f"{_col_name(col_idx)}{row_idx}"
            safe = (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{safe}</t></is></c>')
        row_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def test_payment_import_requires_external_id_during_validation(subscriber):
    valid, errors = import_wizard_service._validate_rows(
        "payments",
        [{"account_id": str(subscriber.id), "amount": "1000.00"}],
    )

    assert valid == []
    assert len(errors) == 1
    assert "external_id" in errors[0]["detail"]


def test_execute_import_dry_run_does_not_persist(db_session):
    payload = "first_name,last_name,email\nAda,Lovelace,ada@example.com\n"

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="subscribers.csv",
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["validated_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "ada@example.com")
        .count()
        == 0
    )


def test_execute_import_subscribers_persists_rows(db_session):
    payload = "first_name,last_name,email,phone\nAda,Lovelace,ada@example.com,+123\n"

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="subscribers.csv",
        dry_run=False,
    )

    assert result["status"] == "success"
    assert result["imported_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "ada@example.com")
        .count()
        == 1
    )


def test_execute_import_subscribers_with_column_mapping(db_session):
    payload = "fname,lname,mail\nAda,Lovelace,ada-map@example.com\n"

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="subscribers_mapped.csv",
        dry_run=False,
        column_mapping={
            "fname": "first_name",
            "lname": "last_name",
            "mail": "email",
        },
    )

    assert result["status"] == "success"
    assert result["imported_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "ada-map@example.com")
        .count()
        == 1
    )


def test_execute_import_subscribers_with_semicolon_delimiter(db_session):
    payload = "first_name;last_name;email\nAda;Lovelace;ada-semi@example.com\n"

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="subscribers_semicolon.csv",
        dry_run=False,
        csv_delimiter=";",
    )

    assert result["status"] == "success"
    assert result["imported_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "ada-semi@example.com")
        .count()
        == 1
    )


def test_execute_import_subscribers_from_xlsx_upload(db_session):
    workbook = _build_inline_xlsx(
        ["first_name", "last_name", "email"],
        [["Ada", "Lovelace", "ada-xlsx@example.com"]],
    )

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="xlsx",
        raw_text="",
        source_name="subscribers.xlsx",
        dry_run=False,
        file_bytes=workbook,
    )

    assert result["status"] == "success"
    assert result["imported_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "ada-xlsx@example.com")
        .count()
        == 1
    )


def test_execute_import_subscriptions_json(db_session, subscriber, catalog_offer):
    payload = f'[{{"subscriber_id": "{subscriber.id}", "offer_id": "{catalog_offer.id}", "status": "pending"}}]'

    result = _stage_and_apply(
        db_session,
        module="subscriptions",
        raw_text=payload,
        source_name="subscriptions.json",
    )

    assert result.status.value == "completed"
    assert result.ok_rows == 1
    assert (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.offer_id == catalog_offer.id)
        .count()
        >= 1
    )


def test_execute_import_subscriptions_preserves_next_billing_date(
    db_session, subscriber, catalog_offer
):
    subscriber.billing_mode = BillingMode.postpaid
    catalog_offer.billing_mode = BillingMode.postpaid
    db_session.commit()
    payload = (
        "subscriber_id,offer_id,status,billing_mode,start_at,next_billing_at\n"
        f"{subscriber.id},{catalog_offer.id},active,postpaid,"
        "2026-07-15T00:00:00+00:00,2026-08-15T00:00:00+00:00\n"
    )

    result = _stage_and_apply(
        db_session,
        module="subscriptions",
        raw_text=payload,
        source_name="splynx_subscriptions.csv",
    )

    assert result.status.value == "completed"
    imported = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.offer_id == catalog_offer.id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    assert imported is not None
    assert imported.billing_mode.value == "postpaid"
    assert imported.next_billing_at is not None
    assert imported.next_billing_at.isoformat().startswith("2026-08-15T00:00:00")


def test_execute_import_invoices_payments_ip_pools_and_network_equipment(
    db_session, subscriber
):
    invoice_payload = (
        "account_id,invoice_number,status,billing_mode,currency,subtotal,tax_total,total,balance_due,memo\n"
        f"{subscriber.id},INV-1,issued,prepaid,NGN,100,0,100,100,Imported\n"
    )
    payment_payload = (
        "account_id,amount,currency,status,memo,external_id\n"
        f"{subscriber.id},50,NGN,succeeded,Imported Payment,ref-1\n"
    )
    pool_payload = "name,ip_version,cidr\nImport Pool,ipv4,10.30.0.0/24\n"
    device_payload = "name,hostname,role,status\nSwitch 1,switch-1,edge,offline\n"

    invoice_result = _stage_and_apply(
        db_session,
        module="invoices",
        raw_text=invoice_payload,
        source_name="inv.csv",
    )
    payment_result = _stage_and_apply(
        db_session,
        module="payments",
        raw_text=payment_payload,
        source_name="pay.csv",
    )
    pool_result = import_wizard_service.execute_import(
        db_session,
        module="ip_pools",
        data_format="csv",
        raw_text=pool_payload,
        source_name="pool.csv",
        dry_run=False,
    )
    device_result = import_wizard_service.execute_import(
        db_session,
        module="network_equipment",
        data_format="csv",
        raw_text=device_payload,
        source_name="device.csv",
        dry_run=False,
    )

    assert invoice_result.ok_rows == 1
    assert payment_result.ok_rows == 1
    assert pool_result["imported_rows"] == 1
    assert device_result["imported_rows"] == 1
    imported_invoice = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == subscriber.id)
        .order_by(Invoice.created_at.desc())
        .first()
    )
    assert imported_invoice is not None
    assert imported_invoice.metadata_["imported_via"] == "system_import_run"
    assert imported_invoice.metadata_["source_name"] == "inv.csv"
    assert imported_invoice.metadata_["billing_mode"] == "prepaid"

    assert (
        db_session.query(Invoice).filter(Invoice.invoice_number == "INV-1").count() == 1
    )
    assert db_session.query(Payment).filter(Payment.external_id == "ref-1").count() == 1
    imported_payment = (
        db_session.query(Payment).filter(Payment.external_id == "ref-1").one()
    )
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == imported_payment.id)
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == imported_payment.id)
        .count()
        == 1
    )
    db_session.refresh(imported_invoice)
    assert imported_invoice.balance_due == 50
    assert imported_invoice.status.value == "partially_paid"
    assert db_session.query(IpPool).filter(IpPool.name == "Import Pool").count() == 1
    assert (
        db_session.query(NetworkDevice).filter(NetworkDevice.name == "Switch 1").count()
        == 1
    )


def test_legacy_wizard_cannot_apply_financial_rows_directly(db_session, subscriber):
    payload = (
        "account_id,invoice_number,status,currency,total,balance_due\n"
        f"{subscriber.id},INV-STAGED,issued,NGN,10,10\n"
    )

    import pytest

    with pytest.raises(ValueError, match="durable dry run"):
        import_wizard_service.execute_import(
            db_session,
            module="invoices",
            data_format="csv",
            raw_text=payload,
            source_name="staged.csv",
            dry_run=False,
        )


def test_history_and_template_helpers(db_session):
    content = import_wizard_service.csv_template("subscribers")
    assert "first_name,last_name,email" in content

    history_before = import_wizard_service.list_history(db_session)
    import_wizard_service.append_history(
        db_session,
        {
            "import_id": "test-1",
            "module": "subscribers",
            "module_label": "Subscribers",
            "status": "dry_run",
            "timestamp": "2026-02-25T00:00:00+00:00",
        },
    )
    history_after = import_wizard_service.list_history(db_session)

    assert len(history_after) >= len(history_before)
    assert history_after[0]["import_id"] == "test-1"


def test_rollback_import_deletes_created_records(db_session):
    payload = "first_name,last_name,email\nRoll,Back,rollback@example.com\n"
    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="rollback.csv",
        dry_run=False,
    )
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "rollback@example.com")
        .count()
        == 1
    )

    rollback = import_wizard_service.rollback_import(
        db_session, import_id=result["import_id"]
    )

    assert rollback["rolled_back_rows"] == 1
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "rollback@example.com")
        .count()
        == 0
    )
    history = import_wizard_service.list_history(db_session)
    assert history[0]["import_id"] == result["import_id"]
    assert history[0]["status"] == "rolled_back"
    assert history[0].get("rolled_back_at")


def test_rollback_import_respects_window(db_session):
    subscriber = Subscriber(
        first_name="Old",
        last_name="Import",
        email="old-import@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    old_timestamp = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    import_wizard_service.append_history(
        db_session,
        {
            "import_id": "expired-import",
            "module": "subscribers",
            "module_label": "Subscribers",
            "status": "success",
            "timestamp": old_timestamp,
            "dry_run": False,
            "created_records": [{"module": "subscribers", "id": str(subscriber.id)}],
        },
    )

    rollback_window = (
        db_session.query(DomainSetting)
        .filter(
            DomainSetting.domain == SettingDomain.imports,
            DomainSetting.key == "import_rollback_window_hours",
        )
        .first()
    )
    if rollback_window is None:
        rollback_window = DomainSetting(
            domain=SettingDomain.imports,
            key="import_rollback_window_hours",
            value_type=SettingValueType.integer,
            value_text="1",
            is_active=True,
        )
        db_session.add(rollback_window)
    else:
        rollback_window.value_text = "1"
    db_session.commit()

    try:
        import_wizard_service.rollback_import(db_session, import_id="expired-import")
        raised = False
    except ValueError as exc:
        raised = True
        assert "expired" in str(exc).lower()
    assert raised
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "old-import@example.com")
        .count()
        == 1
    )


def test_import_jobs_registry_helpers(db_session):
    import_wizard_service.upsert_job(
        db_session,
        {
            "job_id": "job-1",
            "module": "subscribers",
            "status": "queued",
            "progress_percent": 0,
        },
    )
    import_wizard_service.upsert_job(
        db_session,
        {
            "job_id": "job-1",
            "status": "running",
            "progress_percent": 40,
        },
    )
    job = import_wizard_service.get_job(db_session, "job-1")
    assert job is not None
    assert job["status"] == "running"
    assert int(job["progress_percent"]) == 40
    jobs = import_wizard_service.list_jobs(db_session)
    assert jobs
    assert jobs[0]["job_id"] == "job-1"


def test_execute_import_reports_progress_updates(db_session):
    payload = "first_name,last_name,email\nA,One,a1@example.com\nB,Two,b2@example.com\n"
    updates: list[dict[str, object]] = []

    result = import_wizard_service.execute_import(
        db_session,
        module="subscribers",
        data_format="csv",
        raw_text=payload,
        source_name="progress.csv",
        dry_run=False,
        progress_callback=lambda update: updates.append(update),
    )

    assert result["status"] == "success"
    assert result["imported_rows"] == 2
    assert any(str(item.get("phase")) == "validated" for item in updates)
    assert any(str(item.get("phase")) == "completed" for item in updates)
