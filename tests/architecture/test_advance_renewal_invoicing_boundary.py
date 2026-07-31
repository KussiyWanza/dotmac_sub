from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_advance_renewal_has_no_implicit_notice_day() -> None:
    owner = _source("app/services/advance_renewal_invoicing.py")
    settings = _source("app/services/settings_spec.py")

    assert 'key="renewal_invoice_notice_days"' in settings
    assert "default=None" in settings
    assert "raw_days or 7" not in owner
    assert "days_before=7" not in owner


def test_durable_timer_drives_owner_dates_and_invoice_writes() -> None:
    owner = _source("app/services/advance_renewal_invoicing.py")

    assert "ScheduleTimerCommand" in owner
    assert "schedule_advance_renewal_timer" in owner
    assert "find_due_subscription_ids" not in owner
    assert "billing_period_start=period_start" in owner
    assert "due_at=period_start" in owner
    assert "subscription.next_billing_at =" not in owner
    assert "Invoices.stage_system_invoice" in owner
    assert "InvoiceLines.stage_system_line" in owner
