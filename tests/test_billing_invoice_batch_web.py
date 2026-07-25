from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.audit import AuditEvent
from app.models.billing import BillingRun, BillingRunStatus
from app.services.billing_automation import reconcile_billing_run_audit
from app.services.web_billing_invoice_batch import (
    INVOICE_BATCH_STALE_MESSAGE,
    InvoiceBatchActionError,
    build_batch_action_form,
    confirm_batch_action,
    get_run_row,
    list_recent_runs,
    preview_batch_action,
    preview_retry_batch,
    render_runs_csv,
    render_single_run_csv,
)


def _summary(*, subscription_id: str = "sub-1") -> dict[str, object]:
    return {
        "run_at": datetime(2026, 2, 24, tzinfo=UTC),
        "invoices_created": 1,
        "accounts_affected": 1,
        "subscriptions_billed": 1,
        "skipped": 2,
        "totals_by_currency": {"NGN": Decimal("12500.00")},
        "subscriptions": [
            {
                "id": subscription_id,
                "account_id": "account-1",
                "offer_name": "Fibre 100",
                "amount": Decimal("12500.00"),
                "currency": "NGN",
                "period_start": "2026-02-24T00:00:00+00:00",
                "period_end": "2026-03-24T00:00:00+00:00",
                "pending_activation": False,
            }
        ],
    }


def test_preview_batch_action_is_exact_and_excludes_prepaid_orchestration(monkeypatch):
    captured: list[dict[str, object]] = []

    def _fake_run_invoice_cycle(**kwargs):
        captured.append(kwargs)
        return _summary()

    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        _fake_run_invoice_cycle,
    )

    preview = preview_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
    )

    assert preview.invoice_count == 1
    assert preview.subscription_count == 1
    assert preview.total_display == "NGN 12,500.00"
    assert preview.subscriptions[0].subscription_id == "sub-1"
    assert captured[0]["dry_run"] is True
    assert captured[0]["run_prepaid_renewals"] is False


def test_preview_fingerprint_changes_with_exact_membership(monkeypatch):
    current_id = "sub-1"

    def _fake_run_invoice_cycle(**kwargs):
        return _summary(subscription_id=current_id)

    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        _fake_run_invoice_cycle,
    )
    first = preview_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
    )
    current_id = "sub-2"
    second = preview_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
    )

    assert first.fingerprint != second.fingerprint


def test_confirm_batch_rejects_missing_confirmation(monkeypatch):
    with pytest.raises(InvoiceBatchActionError, match="confirmation"):
        confirm_batch_action(
            db=None,
            billing_cycle="monthly",
            billing_date="2026-02-24",
            preview_fingerprint="fingerprint",
            source_run_id=None,
            confirmed=False,
            actor="staff-1",
        )


def test_confirm_batch_rejects_stale_preview(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        lambda **kwargs: _summary(),
    )

    with pytest.raises(InvoiceBatchActionError, match=INVOICE_BATCH_STALE_MESSAGE):
        confirm_batch_action(
            db=None,
            billing_cycle="monthly",
            billing_date="2026-02-24",
            preview_fingerprint="stale",
            source_run_id=None,
            confirmed=True,
            actor="staff-1",
        )


def test_confirm_batch_rechecks_then_executes_postpaid_scope(monkeypatch):
    calls: list[dict[str, object]] = []

    def _fake_run_invoice_cycle(**kwargs):
        calls.append(kwargs)
        return _summary()

    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        _fake_run_invoice_cycle,
    )
    preview = preview_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
    )
    note = confirm_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
        preview_fingerprint=preview.fingerprint,
        source_run_id=None,
        confirmed=True,
        actor="staff-1",
    )

    assert len(calls) == 3
    assert calls[-1]["dry_run"] is False
    assert calls[-1]["run_prepaid_renewals"] is False
    assert calls[-1]["launch_kind"] == "manual"
    assert calls[-1]["requested_by"] == "staff-1"
    assert calls[-1]["preview_fingerprint"] == preview.fingerprint
    assert "Invoices created: 1" in note


def test_retry_preview_is_limited_to_failed_runs(db_session):
    successful = BillingRun(
        run_at=datetime(2026, 2, 1, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.success,
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db_session.add(successful)
    db_session.commit()

    with pytest.raises(InvoiceBatchActionError, match="Only a failed"):
        preview_retry_batch(db_session, run_id=str(successful.id))


def test_retry_preview_maps_missing_run_to_domain_error(db_session):
    with pytest.raises(InvoiceBatchActionError, match="not found"):
        preview_retry_batch(db_session, run_id="not-a-run-id")


def test_list_recent_runs_projects_retry_eligibility(db_session):
    failed = BillingRun(
        run_at=datetime(2026, 2, 1, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.failed,
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
        error="failed",
    )
    successful = BillingRun(
        run_at=datetime(2026, 2, 2, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.success,
        started_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    db_session.add_all([failed, successful])
    db_session.commit()

    rows = list_recent_runs(db_session, limit=10)
    rows_by_id = {row["id"]: row for row in rows}

    assert rows_by_id[str(failed.id)]["retry_allowed"] is True
    assert rows_by_id[str(successful.id)]["retry_allowed"] is False


def test_billing_run_audit_projection_is_idempotently_repairable(db_session):
    run = BillingRun(
        run_at=datetime(2026, 2, 3, tzinfo=UTC),
        billing_cycle="monthly",
        launch_kind="manual",
        requested_by="staff-1",
        preview_fingerprint="a" * 64,
        status=BillingRunStatus.failed,
        started_at=datetime(2026, 2, 3, tzinfo=UTC),
        error="failed",
    )
    db_session.add(run)
    db_session.commit()

    assert reconcile_billing_run_audit(db_session, run.id) is True
    assert reconcile_billing_run_audit(db_session, run.id) is False
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "billing_run")
        .filter(AuditEvent.entity_id == str(run.id))
        .one()
    )
    assert audit.actor_id == "staff-1"
    assert audit.metadata_["preview_fingerprint"] == "a" * 64


def test_batch_action_form_carries_exact_owner_evidence(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        lambda **kwargs: _summary(),
    )
    preview = preview_batch_action(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
    )

    form = build_batch_action_form(preview)

    assert form.action_url.endswith("/generate-batch/confirm")
    assert {item.key: item.value for item in form.hidden_values}[
        "preview_fingerprint"
    ] == preview.fingerprint
    assert form.confirmation is not None


def test_get_run_row_and_csv_exports(db_session):
    run = BillingRun(
        run_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.success,
        started_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 3, 1, 8, 1, tzinfo=UTC),
        subscriptions_scanned=12,
        subscriptions_billed=9,
        invoices_created=9,
        lines_created=9,
        skipped=3,
    )
    db_session.add(run)
    db_session.commit()

    row = get_run_row(db_session, run_id=str(run.id))

    assert row is not None
    assert str(run.id) in render_single_run_csv(row)
    assert "run_id,run_at,created_at,billing_cycle" in render_runs_csv([row])


def test_batch_routes_and_templates_use_review_confirm_flow():
    route = Path("app/web/admin/billing_invoice_batch.py").read_text()
    page = Path("templates/admin/billing/invoice_batch.html").read_text()
    history = Path(
        "templates/admin/billing/_invoice_batch_history_table.html"
    ).read_text()

    assert '"/invoices/generate-batch/preview"' in route
    assert '"/invoices/generate-batch/confirm"' in route
    assert '"/invoices/batch/{run_id}/retry/preview"' in route
    assert '"/invoices/batch/{run_id}/retry"' not in route
    assert "action_form(batch_action_form)" in page
    assert "window.confirm" not in page
    assert "confirm(" not in history
