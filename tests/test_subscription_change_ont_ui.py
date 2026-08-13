from __future__ import annotations

from pathlib import Path


def test_change_ont_routes_require_ont_write_permission() -> None:
    source = Path("app/web/admin/catalog.py").read_text()
    assert '"/subscriptions/{subscription_id}/change-ont"' in source
    assert 'require_permission("network:ont:write")' in source
    assert source.count('require_permission("network:ont:write")') >= 2


def test_subscription_detail_gates_change_ont_button() -> None:
    template = Path("templates/admin/catalog/subscription_detail.html").read_text()

    assert "can_change_ont and network_path.ont" in template
    assert "Change ONT" in template
    assert "/change-ont" in template


def test_change_ont_confirmation_template_shows_required_evidence() -> None:
    template = Path("templates/admin/catalog/subscription_change_ont.html").read_text()

    assert "Current active assignment" in template
    assert "Current ONT" in template
    assert "Current MAC" in template
    assert "Replacement ONT" in template
    assert "confirm_change" in template
    assert "customer access path will resolve through the replacement ONT" in template
    assert "submitting" in template
