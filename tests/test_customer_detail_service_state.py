"""Observed service state and ticket prefill on the customer page.

Sub reports state and path; it does not infer a cause. An agent reading the
RADIUS state next to the access path draws a better conclusion than a guess
would, and a wrong guess is worse than none because it stops the agent looking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app.services import web_customer_details as details


def _subscription():
    return SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        login="10005452",
        ipv4_address="10.10.11.6",
        status=SimpleNamespace(value="active"),
        offer=SimpleNamespace(name="Home 20M"),
        provisioning_nas_device=None,
        mac_address=None,
        ipv6_address=None,
    )


def _decision(**overrides):
    defaults = {
        "radius_access_state": SimpleNamespace(value="suspended"),
        "radius_allowed": False,
        "radius_blocked": True,
        "radius_mode": "hard_reject",
        "access_block_reason": "account_disabled",
        "billing_block_reason": "overdue_balance",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Access state facts
# ---------------------------------------------------------------------------


def test_access_state_reports_the_owners_reason_verbatim(monkeypatch):
    monkeypatch.setattr(details, "resolve_customer_access", lambda _s: _decision())

    facts = details._build_access_state_facts(_subscription())

    assert facts["radius_state"] == "suspended"
    assert facts["radius_blocked"] is True
    assert facts["access_block_reason"] == "account_disabled"
    assert facts["billing_block_reason"] == "overdue_balance"
    assert facts["source"] == "access.subscription_lifecycle"


def test_access_state_handles_unprovisioned_service(monkeypatch):
    monkeypatch.setattr(
        details,
        "resolve_customer_access",
        lambda _s: _decision(
            radius_access_state=None,
            radius_blocked=False,
            access_block_reason=None,
            billing_block_reason=None,
        ),
    )

    facts = details._build_access_state_facts(_subscription())

    assert facts["radius_state"] is None
    assert facts["access_block_reason"] is None


def test_access_state_failure_does_not_break_the_page(monkeypatch):
    def _boom(_s):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(details, "resolve_customer_access", _boom)

    assert details._build_access_state_facts(_subscription()) is None


# ---------------------------------------------------------------------------
# Ticket prefill
# ---------------------------------------------------------------------------


def test_prefill_carries_state_and_path_but_not_a_ticket_type():
    """Sub does not guess the type; a wrong one is worse than an empty one."""

    subscription = _subscription()
    card = {
        "access_endpoint": {
            "endpoint_display": "Gudu OLT (0/1/3)",
            "endpoint_source": "provisioning",
        },
        "connection_status": {
            "label": "Not connected",
            "detail": "No open RADIUS accounting session",
        },
        "access_state": {
            "radius_state": "suspended",
            "access_block_reason": "account_disabled",
            "billing_block_reason": None,
        },
        "topology_trace": {
            "nodes": [
                {
                    "kind": "ont",
                    "label": "UBNT1",
                    "state": "down",
                    "detail": {"onu_rx_signal_dbm": -31.0},
                }
            ],
            "breaks": [{"code": "upstream.unproven", "message": "No reviewed path."}],
        },
    }

    url = details._ticket_prefill_url(subscription, card)

    params = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/admin/support/tickets/new"
    assert "ticket_type" not in params
    assert params["subscriber_id"] == [str(subscription.subscriber_id)]
    description = params["description"][0]
    assert "Serving endpoint: Gudu OLT (0/1/3)" in description
    assert "Session: Not connected" in description
    assert "RADIUS access: suspended" in description
    assert "Access block reason: account_disabled" in description
    assert "ont: UBNT1 [down] rx -31.0 dBm" in description
    assert "path break: upstream.unproven" in description


def test_prefill_reports_an_unresolved_path_explicitly():
    card = {
        "access_endpoint": {"endpoint_source": "unresolved", "gap": "ont_unassigned"},
    }

    url = details._ticket_prefill_url(_subscription(), card)

    assert (
        "Serving endpoint: unresolved (ont_unassigned)"
        in parse_qs(urlparse(url).query)["description"][0]
    )


def test_card_carries_access_state_and_prefill(monkeypatch):
    monkeypatch.setattr(details, "resolve_customer_access", lambda _s: _decision())
    subscription = _subscription()
    facts = details._build_access_state_facts(subscription)

    cards = details._build_network_access_cards(
        [subscription], {}, {}, {}, {}, {str(subscription.id): facts}
    )

    assert cards[0]["access_state"]["radius_state"] == "suspended"
    assert cards[0]["ticket_prefill_url"].startswith("/admin/support/tickets/new?")


# ---------------------------------------------------------------------------
# The inference layer stays gone
# ---------------------------------------------------------------------------


def test_no_diagnosis_service_remains():
    """Removed deliberately: it inferred causes it could not justify."""

    assert not Path("app/services/network/service_diagnosis.py").exists()
    assert (
        "service_diagnosis"
        not in Path("app/services/web_customer_details.py").read_text()
    )


def test_template_shows_state_not_a_verdict():
    template = Path("templates/admin/customers/detail.html").read_text()

    assert "access_state.radius_state" in template
    assert "diagnosis.verdict" not in template


# ---------------------------------------------------------------------------
# Known incident (G4)
# ---------------------------------------------------------------------------


class _ServiceState(SimpleNamespace):
    pass


def _state(**overrides):
    defaults = {
        "area_outage": True,
        "connection_state": "outage",
        "active_outage_id": uuid.uuid4(),
        "open_infrastructure_ticket_id": uuid.uuid4(),
        "customer_message": "We're working on an area outage.",
        "checked_at": datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return _ServiceState(**defaults)


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(
        "app.services.customer_service_state.get_customer_service_state",
        lambda _db, _sub: state,
    )


def test_known_incident_is_surfaced_to_the_agent(monkeypatch):
    """The customer already sees this on the portal; the agent did not."""

    _patch_state(monkeypatch, _state())

    incident = details._build_known_incident(None, _subscription())

    assert incident["area_outage"] is True
    assert incident["connection_state"] == "outage"
    assert incident["incident_id"]
    assert incident["infrastructure_ticket_id"]


def test_no_incident_renders_no_panel(monkeypatch):
    """Absent, not an empty "no known issues" box on every customer."""

    _patch_state(
        monkeypatch,
        _state(
            area_outage=False,
            active_outage_id=None,
            open_infrastructure_ticket_id=None,
        ),
    )

    assert details._build_known_incident(None, _subscription()) is None


def test_incident_without_a_ticket_still_surfaces(monkeypatch):
    _patch_state(monkeypatch, _state(open_infrastructure_ticket_id=None))

    incident = details._build_known_incident(None, _subscription())

    assert incident["incident_id"]
    assert incident["infrastructure_ticket_id"] is None


def test_service_state_failure_does_not_break_the_page(monkeypatch):
    def _boom(_db, _sub):
        raise RuntimeError("topology unavailable")

    monkeypatch.setattr(
        "app.services.customer_service_state.get_customer_service_state", _boom
    )

    assert details._build_known_incident(None, _subscription()) is None


def test_card_carries_the_known_incident(monkeypatch):
    monkeypatch.setattr(details, "resolve_customer_access", lambda _s: _decision())
    subscription = _subscription()
    payload = {"area_outage": True, "incident_id": "abc"}

    cards = details._build_network_access_cards(
        [subscription], {}, {}, {}, {}, {}, {str(subscription.id): payload}
    )

    assert cards[0]["known_incident"] == payload


def test_card_carries_the_matching_service_health_projection(monkeypatch):
    monkeypatch.setattr(details, "resolve_customer_access", lambda _s: _decision())
    subscription = _subscription()
    service_health = SimpleNamespace(subscription_id=subscription.id)

    cards = details._build_network_access_cards(
        [subscription],
        {},
        service_health_by_subscription={str(subscription.id): service_health},
    )

    assert cards[0]["service_health"] is service_health


def test_customer_detail_rehomes_service_health_facts():
    template = Path("templates/admin/customers/detail.html").read_text()

    assert "service_health_strip" not in template
    assert "Subscription ID: {{ subscription.id }}" in template
    assert "service_health.pending_change" in template
    assert "card_health.access_reason" in template
    assert "health_connection.medium" in template
    assert "No known outage" in template
    assert "health_connection.checked_at" in template


def test_network_access_defaults_to_active_cards_and_expands_inactive_cards():
    template = Path("templates/admin/customers/detail.html").read_text()

    assert "showAllNetworkAccess: false" in template
    assert "showAllNetworkAccess || '{{ card.status }}' === 'active'" in template
    assert "(network_access_inactive_count | default(0)) > 0" in template
    assert "showAllNetworkAccess ? 'Show active only' : 'View more'" in template
    assert "cancelled, hidden, and other inactive subscriptions" in template
    assert (
        'class="rounded-xl border border-slate-200 p-4 shadow-sm '
        'dark:border-slate-700"' in template
    )
