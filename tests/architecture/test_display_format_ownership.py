from __future__ import annotations

from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]

PILOT_SERVICES = (
    "app/services/web_billing_overview.py",
    "app/services/web_billing_payments.py",
    "app/services/web_billing_ledger.py",
    "app/services/web_billing_reconciliation.py",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pilot_services_delegate_multi_currency_display_to_named_owner() -> None:
    for path in PILOT_SERVICES:
        source = _read(path)
        assert "from app.services import display_format" in source, path
        assert "display_format.format_currency_groups(" in source, path
        assert "def _currency_code(" not in source, path
        assert "def _format_currency_amount(" not in source, path
        assert "def _format_currency_groups(" not in source, path
        assert '"NGN 0.00"' not in source, path
        assert 'or "NGN"' not in source, path


def test_checked_in_sources_name_display_format_owner_and_migration() -> None:
    owner = service_relationship("ui.display_formatting")
    relationships = _read("docs/SOT_RELATIONSHIP_MAP.md")
    frontend = _read("docs/FRONTEND_SPEC.md")

    assert owner.module == "app.services.display_format"
    assert "## UI Display Formatting" in relationships
    assert "four billing web projection modules" in relationships
    assert "app.services.display_format" in frontend
    assert "format_currency_groups" in frontend
    assert "Missing scalar fact" in frontend


def test_mobile_formatter_is_documented_as_renderer_not_fact_owner() -> None:
    relationships = _read("docs/SOT_RELATIONSHIP_MAP.md")
    mobile = _read("mobile/lib/src/core/formatters.dart")

    assert "platform renderer" in relationships
    assert "class Fmt" in mobile
    assert "static String money(" in mobile
