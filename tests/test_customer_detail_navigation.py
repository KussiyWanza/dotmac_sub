from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_project_detail_renders_owner_provided_customer_route_only_on_detail_page():
    detail = _source("templates/admin/projects/project_detail.html")
    project_list = _source("templates/admin/projects/_table.html")

    assert 'href="{{ project_customer.detail_url }}"' in detail
    assert "project_customer.detail_url" not in project_list


def test_ticket_detail_renders_owner_provided_customer_route_only_on_detail_page():
    detail = _source("templates/admin/support/tickets/detail.html")
    ticket_list = _source("templates/admin/support/tickets/_table.html")

    assert 'href="{{ customer_details.detail_url }}"' in detail
    assert "customer_details.detail_url" not in ticket_list
