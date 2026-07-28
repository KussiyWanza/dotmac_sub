"""Outage lifecycle -> capability-bound outbound event fan-out."""

from __future__ import annotations

from app.models.integration_platform import IntegrationDelivery
from app.models.network_monitoring import NetworkDevice
from app.services.integrations import delivery, installations
from app.services.integrations.runtime import ValidationResult
from app.services.topology.outage import (
    AUTO_DETECT_ACTOR,
    declare_outage,
    resolve_outage,
)


def _node(db, name="Agg-1"):
    node = NetworkDevice(name=name, is_active=True)
    db.add(node)
    db.flush()
    return node


def _subscribe(db, url="https://crm.example/hooks/outage"):
    endpoint = installations.create_draft(
        db,
        connector_key="webhook.http",
        name="CRM outage events",
        environment="test",
    )
    installations.create_config_revision(
        db,
        installation_id=endpoint.id,
        config={"url": url, "method": "POST", "max_attempts": 3},
        secret_refs={},
    )
    binding = installations.bind_capability(
        db,
        installation_id=endpoint.id,
        capability_id="events.deliver.v1",
        policy={"approved_egress_hosts": ["crm.example"]},
    )
    installations.validate_static(db, installation_id=endpoint.id)
    installations.enable_after_connection_validation(
        db,
        installation_id=endpoint.id,
        connection_result=ValidationResult(valid=True),
    )
    delivery.create_event_subscription(
        db,
        capability_binding_id=binding.id,
        event_type="network.alert",
    )
    return endpoint


def _deliveries(db):
    return (
        db.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "network.alert")
        .all()
    )


def test_declare_creates_delivery_with_outage_payload(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    incident = declare_outage(db_session, node=node, declared_by="noc@x")
    # Events stage in the declare transaction and dispatch after commit.
    db_session.commit()

    deliveries = _deliveries(db_session)
    assert len(deliveries) == 1
    payload = deliveries[0].payload_json["payload"]
    assert payload["alert_type"] == "outage.created"
    assert payload["incident_id"] == str(incident.id)
    assert payload["detection_source"] == "manual"
    assert payload["scope"] == {"type": "node", "id": str(node.id), "name": node.name}
    # No PII beyond counts — detail comes from the CRM outage API.
    assert "subscribers" not in payload


def test_resolve_creates_second_delivery_only_on_transition(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    db_session.commit()

    resolve_outage(db_session, incident.id)
    resolve_outage(db_session, incident.id)  # idempotent: no third delivery
    db_session.commit()

    deliveries = _deliveries(db_session)
    assert len(deliveries) == 2
    kinds = {d.payload_json["payload"]["alert_type"] for d in deliveries}
    assert kinds == {"outage.created", "outage.resolved"}
    resolved = next(
        d
        for d in deliveries
        if d.payload_json["payload"]["alert_type"] == "outage.resolved"
    )
    assert resolved.payload_json["payload"]["resolved_at"] is not None


def test_auto_detected_incident_marked_in_payload(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    declare_outage(db_session, node=node, declared_by=AUTO_DETECT_ACTOR)
    db_session.commit()
    payload = _deliveries(db_session)[0].payload_json["payload"]
    assert payload["detection_source"] == "auto"


def test_no_subscription_means_no_delivery_and_no_error(db_session):
    """Webhooks disabled cleanly when nothing subscribes: declare/resolve
    still succeed and no delivery rows appear."""
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    resolve_outage(db_session, incident.id)
    db_session.commit()
    assert _deliveries(db_session) == []
    db_session.refresh(incident)
    assert incident.status == "resolved"
