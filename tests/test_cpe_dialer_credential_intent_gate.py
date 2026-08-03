"""The producer gate delegates to the delivery-authorization owner.

An earlier version of this gate read `OntAssignment.wan_mode`, `ip_mode` and
`pppoe_username`. Migration 084 copied those into desired config and then set
them `NULL`, so the 12 production survivors are unexplained residue and cannot
authorise a device write.

One owner now answers "may this ONT terminate PPP" for both the producer and
delivery, so the producer cannot stage what delivery would refuse to send. The
ruling matrix itself is covered in `test_ppp_delivery_authorization.py`.
"""

from __future__ import annotations

from app.models.network import (
    OntAssignment,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
)
from app.services.cpe_dialer_credential_reconcile import termination_intent


def _ont(db_session, serial):
    ont = OntUnit(serial_number=serial, is_active=True)
    db_session.add(ont)
    db_session.flush()
    return ont


def test_declared_pppoe_service_intent_authorises(db_session, subscription):
    ont = _ont(db_session, "HWTC-GATE-OK")
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscription_id=subscription.id, active=True)
    )
    db_session.add(
        OntWanServiceInstance(
            ont_id=ont.id,
            subscription_id=subscription.id,
            name="internet",
            service_type="internet",
            connection_type="pppoe",
            is_primary=True,
            lifecycle_state=OntWanServiceLifecycle.active,
            is_active=True,
        )
    )
    db_session.commit()

    eligible, reason = termination_intent(db_session, ont.id)

    assert eligible is True
    assert reason == "managed_ont_pppoe"


def test_no_declared_intent_refuses(db_session):
    """The production majority: 1,373 services carried a staged dialer."""
    ont = _ont(db_session, "HWTC-GATE-NONE")
    db_session.commit()

    eligible, reason = termination_intent(db_session, ont.id)

    assert eligible is False
    # No active assignment resolves to no exact service to check intent for.
    assert reason == "no_active_assignment"


def test_an_unverified_legacy_row_does_not_authorise_the_producer(
    db_session, subscription
):
    """Producer and delivery must agree, including on the quarantine.

    Migration 456 leaves legacy `is_active` untouched, so this row looks active
    to anything reading the old flag while the owner holds it non-authorising.
    """
    ont = _ont(db_session, "HWTC-GATE-LEGACY")
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscription_id=subscription.id, active=True)
    )
    db_session.add(
        OntWanServiceInstance(
            ont_id=ont.id,
            subscription_id=subscription.id,
            name="internet",
            service_type="internet",
            connection_type="pppoe",
            is_primary=True,
            lifecycle_state=OntWanServiceLifecycle.unverified,
            is_active=True,
        )
    )
    db_session.commit()

    eligible, reason = termination_intent(db_session, ont.id)

    assert eligible is False
    assert reason == "no_active_service_intent"


def test_the_gate_no_longer_reads_migration_084_residue():
    """Guard against reintroducing the cleared fields as an authority."""
    from pathlib import Path

    source = Path("app/services/cpe_dialer_credential_reconcile.py").read_text(
        encoding="utf-8"
    )
    gate = source[source.index("def termination_intent(") :]
    gate = gate[: gate.index("\ndef ", 1)]

    # Strip the docstring: it names the cleared fields precisely to explain
    # why they are not read, and matching on the word would flag the warning.
    body = gate.split('"""')[-1]

    assert "authorize_ppp_termination_intent" in gate
    assert ".wan_mode" not in body
    assert ".ip_mode" not in body
    assert "assignment_pppoe_username" not in body
