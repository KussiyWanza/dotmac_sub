from __future__ import annotations

import uuid

import pytest

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_topology_observation import OntTopologyObservationEvidence
from app.services.network.ont_assignment_alignment import (
    project_ont_topology_from_fsp_observation,
)
from app.services.network.ont_authorization import (
    create_or_find_ont_for_authorized_serial,
    record_topology_observation_for_authorized_ont,
)
from app.services.network.ont_topology_observations import (
    OntTopologyObservationError,
    observe_ont_electronic_topology,
)
from app.services.web_network_ont_identity_reviews import (
    list_topology_observation_reviews,
)


def _olt(db_session, label: str) -> OLTDevice:
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"{label} {suffix}", hostname=f"{label.lower()}-{suffix}", is_active=True
    )
    db_session.add(olt)
    db_session.flush()
    return olt


def _ont(db_session, label: str, **values) -> OntUnit:
    ont = OntUnit(
        serial_number=f"{label}-{uuid.uuid4().hex[:10]}", is_active=True, **values
    )
    db_session.add(ont)
    db_session.flush()
    return ont


def test_uisp_observation_initializes_empty_topology_with_durable_evidence(
    db_session,
):
    olt = _olt(db_session, "UISP")
    ont = _ont(db_session, "UISP-ONT")

    initialized = observe_ont_electronic_topology(
        db_session,
        source="uisp",
        evidence_key="uisp-onu-42",
        ont_unit_id=ont.id,
        observed_olt_id=olt.id,
        observed_port_number=4,
    )
    repeated = observe_ont_electronic_topology(
        db_session,
        source="uisp",
        evidence_key="uisp-onu-42",
        ont_unit_id=ont.id,
        observed_olt_id=olt.id,
        observed_port_number=4,
    )
    db_session.commit()
    db_session.refresh(ont)
    db_session.refresh(repeated.evidence)

    assert initialized.outcome == "initialized"
    assert initialized.pon_created is True
    assert ont.olt_device_id == olt.id
    assert ont.pon_port_id == initialized.pon_port.id
    assert initialized.evidence.id == repeated.evidence.id
    assert repeated.outcome == "confirmed"
    assert repeated.evidence.initial_outcome == "initialized"
    assert repeated.evidence.latest_outcome == "confirmed"
    assert repeated.evidence.seen_count == 2
    assert repeated.evidence.resolved_at is not None


def test_observation_conflict_is_evidence_and_never_overwrites_assignment(
    db_session, subscription
):
    canonical_olt = _olt(db_session, "Canonical")
    observed_olt = _olt(db_session, "Observed")
    canonical_pon = PonPort(
        olt_id=canonical_olt.id, name="pon1", port_number=1, is_active=True
    )
    db_session.add(canonical_pon)
    db_session.flush()
    ont = _ont(
        db_session,
        "CONFLICT",
        olt_device_id=canonical_olt.id,
        pon_port_id=canonical_pon.id,
    )
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=canonical_pon.id,
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    result = observe_ont_electronic_topology(
        db_session,
        source="uisp",
        evidence_key="uisp-onu-conflict",
        ont_unit_id=ont.id,
        observed_olt_id=observed_olt.id,
        observed_port_number=9,
    )
    db_session.commit()
    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert result.outcome == "review_required"
    assert result.assignment_conflict_ids == (assignment.id,)
    assert ont.olt_device_id == canonical_olt.id
    assert ont.pon_port_id == canonical_pon.id
    assert assignment.pon_port_id == canonical_pon.id
    assert assignment.active is True
    assert result.evidence.resolved_at is None
    review_rows = list_topology_observation_reviews(
        db_session, query=str(result.evidence.id)
    )
    assert len(review_rows) == 1
    assert review_rows[0]["evidence"].id == result.evidence.id
    assert review_rows[0]["proposal_assignment_id"] == str(assignment.id)


def test_huawei_fsp_reuses_modeled_pon_and_does_not_create_missing_inventory(
    db_session,
):
    olt = _olt(db_session, "Huawei")
    modeled = PonPort(olt_id=olt.id, name="0/1/3", is_active=True)
    db_session.add(modeled)
    db_session.flush()
    ont = _ont(db_session, "HUAWEI-ONT")

    initialized = project_ont_topology_from_fsp_observation(
        db_session, ont=ont, olt_id=olt.id, fsp="0/1/3"
    )
    missing_ont = _ont(db_session, "HUAWEI-MISSING")
    missing = project_ont_topology_from_fsp_observation(
        db_session, ont=missing_ont, olt_id=olt.id, fsp="0/9/9"
    )
    db_session.commit()

    assert initialized is not None
    assert initialized.updated is True
    assert initialized.review_required is False
    assert ont.pon_port_id == modeled.id
    assert ont.board == "0/1"
    assert ont.port == "3"
    assert missing is not None
    assert missing.review_required is True
    assert missing_ont.olt_device_id is None
    assert missing_ont.pon_port_id is None
    assert db_session.query(PonPort).filter_by(olt_id=olt.id, name="0/9/9").count() == 0
    assert db_session.query(OntTopologyObservationEvidence).count() == 2


def test_huawei_authorization_records_topology_only_through_observation_owner(
    db_session,
):
    canonical_olt = _olt(db_session, "Auth-Canonical")
    observed_olt = _olt(db_session, "Auth-Observed")
    canonical_pon = PonPort(olt_id=canonical_olt.id, name="0/1/1", is_active=True)
    observed_pon = PonPort(olt_id=observed_olt.id, name="0/2/2", is_active=True)
    db_session.add_all([canonical_pon, observed_pon])
    db_session.flush()
    ont = _ont(
        db_session,
        "HWTC-AUTH-CONFLICT",
        olt_device_id=canonical_olt.id,
        pon_port_id=canonical_pon.id,
        board="0/1",
        port="1",
    )
    db_session.commit()

    canonical_external_id = ont.external_id

    ont_id, _message = create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(observed_olt.id),
        fsp="0/2/2",
        serial_number=ont.serial_number,
        ont_id_on_olt=7,
    )
    recorded, message = record_topology_observation_for_authorized_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(observed_olt.id),
        fsp="0/2/2",
    )
    db_session.commit()
    db_session.refresh(ont)

    # Serial uniqueness is scoped to (olt_device_id, serial_number): the row on
    # the canonical OLT is a different ONT and must never be hijacked.
    assert ont_id is not None
    assert ont_id != str(ont.id)
    hijack_candidate = db_session.get(OntUnit, ont_id)
    assert hijack_candidate is not None
    assert hijack_candidate.olt_device_id == observed_olt.id
    assert ont.external_id == canonical_external_id
    assert recorded is False
    assert "reviewed identity repair" in message
    assert ont.olt_device_id == canonical_olt.id
    assert ont.pon_port_id == canonical_pon.id
    assert ont.board == "0/1"
    assert ont.port == "1"
    evidence = db_session.query(OntTopologyObservationEvidence).one()
    assert evidence.latest_outcome == "review_required"
    assert evidence.observed_olt_id == observed_olt.id


def test_new_huawei_authorization_initializes_only_an_exact_modeled_pon(db_session):
    olt = _olt(db_session, "Auth-New")
    modeled_pon = PonPort(olt_id=olt.id, name="0/3/4", is_active=True)
    db_session.add(modeled_pon)
    db_session.commit()

    ont_id, _message = create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(olt.id),
        fsp="0/3/4",
        serial_number=f"HWTC{uuid.uuid4().hex[:12].upper()}",
        ont_id_on_olt=9,
    )
    assert ont_id is not None
    ont = db_session.get(OntUnit, ont_id)
    assert ont is not None
    # The constraint-scoping column is populated at creation time so the scoped
    # unique constraint actually dedupes new rows under Postgres NULL semantics.
    assert ont.olt_device_id == olt.id
    assert ont.pon_port_id is None

    recorded, _message = record_topology_observation_for_authorized_ont(
        db_session,
        ont_unit_id=ont_id,
        olt_id=str(olt.id),
        fsp="0/3/4",
    )
    db_session.commit()
    db_session.refresh(ont)

    assert recorded is True
    assert ont.olt_device_id == olt.id
    assert ont.pon_port_id == modeled_pon.id
    assert ont.board == "0/3"
    assert ont.port == "4"


def test_observation_source_is_allowlisted(db_session):
    olt = _olt(db_session, "Unknown")
    ont = _ont(db_session, "UNKNOWN-ONT")

    with pytest.raises(OntTopologyObservationError, match="unsupported"):
        observe_ont_electronic_topology(
            db_session,
            source="untrusted_import",
            evidence_key="external-1",
            ont_unit_id=ont.id,
            observed_olt_id=olt.id,
            observed_port_number=1,
        )
