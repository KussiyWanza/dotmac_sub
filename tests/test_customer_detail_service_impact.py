"""Customer detail renders the spine's impact + SLA words (S7).

The card carries owner-provided presentations only: network.service_impact's
six-state word (exposure never rendered as downtime), customer.service_level's
verdict with measured availability, and honest absence when no live incident
covers the subscription.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, NasDevice, SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.network_monitoring import NetworkDevice
from app.models.usage import AccountingStatus, RadiusAccountingSession
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


def _sla_evidence(db, subscription):
    """Give the card an exact eligible and positively monitored period."""

    evaluated_at = datetime.now(UTC)
    evidence_start = evaluated_at - timedelta(days=10)
    evidence_id = uuid.uuid4()
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    subscription.start_at = evidence_start
    subscription.next_billing_at = evaluated_at + timedelta(days=1)
    db.add_all(
        (
            SubscriptionLifecycleEvent(
                id=evidence_id,
                subscription_id=subscription.id,
                event_type=LifecycleEventType.activate,
                to_status=SubscriptionStatus.active,
                evidence_grade="state_baseline",
                evidence_source="reconciliation_baseline",
                source_id=f"test:customer-card:{evidence_id}",
                evidence_fingerprint=f"sha256:{uuid.uuid4().hex * 2}",
                effective_at=evidence_start,
                recorded_at=evidence_start,
                created_at=evidence_start,
            ),
            ServiceEntitlement(
                account_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                starts_at=evidence_start,
                ends_at=evaluated_at + timedelta(days=1),
                status=ServiceEntitlementStatus.active,
            ),
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id=f"customer-card-{uuid.uuid4()}",
                status_type=AccountingStatus.stop,
                session_start=evidence_start,
                session_end=evaluated_at,
                last_update_at=evaluated_at,
            ),
        )
    )
    db.flush()
    return evaluated_at


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
    evaluated_at = _sla_evidence(db_session, subscription)

    level = details._build_service_level(db_session, subscription, now=evaluated_at)

    assert level is not None
    # No SLA profile on the fixture offer: the honest verdict, never 99.5%.
    assert level["verdict"] == "no_contractual_sla"
    assert level["presentation"].label == "No contractual SLA"
    assert level["target_percent"] is None
    assert level["availability_percent"] is not None


def test_cards_carry_the_panels(db_session, subscription):
    node = _covered_subscription(db_session, subscription)
    declare_outage(db_session, node=node)
    subscription.login = "ui-login"
    evaluated_at = _sla_evidence(db_session, subscription)

    cards = details._build_network_access_cards(
        [subscription],
        {},
        service_impact_by_subscription={
            str(subscription.id): details._build_service_impact(
                db_session, subscription
            )
        },
        service_level_by_subscription={
            str(subscription.id): details._build_service_level(
                db_session, subscription, now=evaluated_at
            )
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
