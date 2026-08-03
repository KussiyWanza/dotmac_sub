"""Customer detail renders the spine's impact + SLA words (S7).

The card carries owner-provided presentations only: network.service_impact's
six-state word (exposure never rendered as downtime), customer.service_level's
verdict with measured availability, and honest absence when no live incident
covers the subscription.
"""

from __future__ import annotations

from pathlib import Path

from app.models.catalog import NasDevice, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.services import web_customer_details as details
from app.services.topology.outage import declare_outage


def _covered_subscription(db, subscription):
    nas = NasDevice(name="NAS-UI", management_ip="10.5.0.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name="ui-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    subscription.status = SubscriptionStatus.active
    subscription.provisioning_nas_device_id = nas.id
    db.flush()
    return node


def test_service_impact_word_reaches_the_card(db_session, subscription):
    node = _covered_subscription(db_session, subscription)
    incident = declare_outage(db_session, node=node)

    impact = details._build_service_impact(db_session, subscription)

    assert impact is not None
    assert impact["state"] == "confirmed_unavailable"
    assert impact["presentation"].label == "Confirmed unavailable"
    assert impact["presentation"].tone.value == "negative"
    assert impact["incident_id"] == str(incident.id)


def test_no_live_incident_is_honest_absence(db_session, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.flush()

    assert details._build_service_impact(db_session, subscription) is None


def test_service_level_context_reaches_the_card(db_session, subscription):
    from datetime import UTC, datetime, timedelta

    subscription.status = SubscriptionStatus.active
    # Entitled time must have elapsed for the period to be measurable.
    subscription.created_at = datetime.now(UTC) - timedelta(days=10)
    db_session.flush()

    level = details._build_service_level(db_session, subscription)

    assert level is not None
    # No SLA profile on the fixture offer: the honest verdict, never 99.5%.
    assert level["verdict"] == "no_contractual_sla"
    assert level["presentation"].label == "No contractual SLA"
    assert level["target_percent"] is None
    assert level["availability_percent"] is not None


def test_cards_carry_the_panels(db_session, subscription):
    from datetime import UTC, datetime, timedelta

    node = _covered_subscription(db_session, subscription)
    declare_outage(db_session, node=node)
    subscription.login = "ui-login"
    subscription.created_at = datetime.now(UTC) - timedelta(days=10)
    db_session.flush()

    cards = details._build_network_access_cards(
        [subscription],
        {},
        service_impact_by_subscription={
            str(subscription.id): details._build_service_impact(
                db_session, subscription
            )
        },
        service_level_by_subscription={
            str(subscription.id): details._build_service_level(db_session, subscription)
        },
    )

    assert cards[0]["service_impact"]["state"] == "confirmed_unavailable"
    assert cards[0]["service_level"]["verdict"] == "no_contractual_sla"


def test_template_includes_the_owner_panel():
    template = Path("templates/admin/customers/detail.html").read_text()
    assert 'include "admin/customers/_service_impact_panel.html"' in template
    # The new SLA score is the only availability figure on this page: the
    # legacy read-time derivation must never render beside it (SHADOWING).
    assert "customer_availability" not in template
    assert "availability_percent" not in template.replace(
        "service_level.availability_percent", ""
    )

    panel = Path("templates/admin/customers/_service_impact_panel.html").read_text()
    # The panel renders presentations; it never maps states or verdicts.
    assert "status_presentation_badge" in panel
    for retired in ("== 'confirmed_unavailable'", "== 'breach'", "text-red-"):
        assert retired not in panel

    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.parse(panel)
    env.parse(template)
