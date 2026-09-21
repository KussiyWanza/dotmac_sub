from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "billing_health.rules.yml"
PREPAID_RULES = ROOT / "deploy" / "observability" / "prepaid_enforcement.rules.yml"
HEALTH = ROOT / "app" / "services" / "billing_health.py"


def test_aged_draft_stock_cannot_page_as_new_leakage() -> None:
    source = RULES.read_text(encoding="utf-8")
    health_source = HEALTH.read_text(encoding="utf-8")

    assert "SubAgedDraftInvoiceBacklogGrowing" not in source
    assert 'signal="aged_draft_invoices"' not in source
    assert "SubRecentDraftInvoiceCohortStalled" in source
    assert 'signal="stalled_draft_invoice_cohort",scope="all"} > 25' in source
    assert "STALLED_DRAFT_ALERT_COUNT = 25" in health_source


def test_money_path_prevention_alerts_use_owner_observations() -> None:
    source = RULES.read_text(encoding="utf-8")
    prepaid_source = PREPAID_RULES.read_text(encoding="utf-8")

    assert "SubPaymentReceiptEmailTemplateUnavailable" in source
    assert 'signal="payment_receipt_email_template_ready",scope="all"} == 0' in source
    assert "SubPrepaidFundingQuarantineGrowing" in source
    assert 'signal="prepaid_funding_quarantined",scope="all"}[24h]) > 0' in source
    assert "PrepaidFundingQuarantineActive" not in prepaid_source


def test_prepaid_lock_contention_alert_uses_the_bounded_sweep_observation() -> None:
    source = PREPAID_RULES.read_text(encoding="utf-8")

    assert "PrepaidSweepLockContentionPersistent" in source
    assert 'signal="lock_deferred"} > 0' in source
    assert 'runbook: "docs/runbooks/DATABASE_TRANSACTION_PRESSURE.md"' in source
