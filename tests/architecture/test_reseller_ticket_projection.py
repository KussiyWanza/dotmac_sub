from pathlib import Path


def test_reseller_dashboard_and_account_tickets_do_not_query_crm():
    web_source = Path("app/services/web_reseller_routes.py").read_text(encoding="utf-8")
    dashboard = web_source.split("def reseller_dashboard", 1)[1].split(
        "def reseller_accounts", 1
    )[0]
    account_tickets = web_source.split("def reseller_account_tickets", 1)[1].split(
        "def reseller_fiber_map", 1
    )[0]
    api_source = Path("app/api/reseller.py").read_text(encoding="utf-8")
    api_dashboard = api_source.split("def my_reseller_dashboard", 1)[1].split(
        "@router.get", 1
    )[0]
    api_tickets = api_source.split("def my_reseller_account_tickets", 1)[1].split(
        "@router.post", 1
    )[0]

    for projection in (
        dashboard,
        account_tickets,
        api_dashboard,
        api_tickets,
    ):
        assert "crm_portal" not in projection
        assert "capability_client" not in projection
