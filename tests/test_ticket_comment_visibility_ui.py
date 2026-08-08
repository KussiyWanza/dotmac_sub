from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_comment_form_requires_explicit_customer_reply():
    template = (
        ROOT / "templates" / "admin" / "support" / "tickets" / "detail.html"
    ).read_text(encoding="utf-8")

    assert 'name="reply_to_customer"' in template
    assert 'name="is_internal"' not in template
    assert "Reply to customer" in template
    assert "Add Internal Note" in template
    assert "Send Reply" in template
    assert "Customer Visible" in template


def test_admin_comment_route_inverts_customer_reply_flag():
    route = (ROOT / "app" / "web" / "admin" / "support_tickets.py").read_text(
        encoding="utf-8"
    )

    assert "reply_to_customer: bool = Form(False)" in route
    assert "is_internal=not reply_to_customer" in route


def test_admin_description_requires_explicit_customer_publication():
    template = (
        ROOT / "templates" / "admin" / "support" / "tickets" / "new.html"
    ).read_text(encoding="utf-8")
    route = (ROOT / "app" / "web" / "admin" / "support_tickets.py").read_text(
        encoding="utf-8"
    )

    assert 'name="publish_description"' in template
    assert "Share description with customer" in template
    assert "publish_description: bool = Form(False)" in route
