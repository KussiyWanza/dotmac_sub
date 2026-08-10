from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_initial_customer_detail_defers_network_projection() -> None:
    route = _source("app/web/admin/customers.py")

    assert "CustomerDetailNetworkQuery(" in route
    assert 'include=panel == "network"' in route


def test_network_panel_is_lazy_and_selects_only_its_fragment() -> None:
    template = _source("templates/admin/customers/detail.html")

    assert 'id="customer-network-panel"' in template
    assert 'hx-trigger="revealed"' in template
    assert 'hx-select="#customer-network-panel"' in template
    assert "network_access_is_bounded" in template
    assert "{% if sub_conn %}" in template


def test_financial_balance_projection_uses_cohort_owner() -> None:
    service = _source("app/services/web_customer_details.py")

    assert "profiles = resolve_billing_profiles(db, accounts)" in service
    assert "prepaid_available_balances(db, prepaid_account_ids)" in service
    assert "get_available_balance" not in service
    assert "if include_network and map_data and primary_address:" in service
