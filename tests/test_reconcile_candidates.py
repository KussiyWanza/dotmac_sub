"""The canonical ONT reconciliation population.

Every exclusion here is a decision about whether an automatic process may drive
a customer's device, so each one is pinned individually rather than inferred
from a single happy-path count.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.network import DeviceStatus, OLTDevice, OntUnit
from app.services.network.reconcile.candidates import (
    restrict_to_reconcile_candidates,
)


def _olt(db, **kw):
    olt = OLTDevice(
        name=kw.pop("name", "OLT-CAND"),
        vendor=kw.pop("vendor", "Huawei"),
        is_active=kw.pop("is_active", True),
        status=kw.pop("status", DeviceStatus.active),
        **kw,
    )
    db.add(olt)
    db.flush()
    return olt


def _ont(db, olt, serial="HWTC-CAND", **kw):
    ont = OntUnit(
        serial_number=serial,
        is_active=kw.pop("is_active", True),
        olt_device_id=olt.id if olt else None,
        **kw,
    )
    db.add(ont)
    db.flush()
    return ont


def _candidates(db, **kw):
    return set(
        db.scalars(restrict_to_reconcile_candidates(select(OntUnit.id), **kw)).all()
    )


def test_an_active_ont_on_an_active_huawei_olt_is_a_candidate(db_session):
    ont = _ont(db_session, _olt(db_session))

    assert ont.id in _candidates(db_session)


@pytest.mark.parametrize(
    ("label", "olt_kw"),
    [
        ("olt is not huawei", {"vendor": "ZTE"}),
        ("olt is deactivated", {"is_active": False}),
        ("olt status is not active", {"status": DeviceStatus.inactive}),
        ("olt is uisp managed", {"uisp_device_id": "uisp-olt-1"}),
    ],
)
def test_the_olt_disqualifies_its_onts(db_session, label, olt_kw):
    ont = _ont(db_session, _olt(db_session, name=f"OLT-{label}", **olt_kw))

    assert ont.id not in _candidates(db_session), label


def test_a_uisp_managed_ont_is_excluded_even_on_a_huawei_olt(db_session):
    """Ownership is explicit: a UFiber ONU must never enter Huawei SSH/ACS."""
    ont = _ont(
        db_session,
        _olt(db_session),
        serial="UFIBER-1",
        uisp_device_id="uisp-ont-1",
    )

    assert ont.id not in _candidates(db_session)


def test_an_ont_with_no_olt_association_is_never_a_candidate(db_session):
    """The join removes it, and nothing in the resulting count says so.

    This is the silent exclusion behind issue #1964 -- 62 active ONTs on
    production. Pinned so the behaviour is at least deliberate and visible in
    the test suite while the topology question is adjudicated.
    """
    ont = _ont(db_session, None, serial="HWTC-NO-OLT")

    assert ont.id not in _candidates(db_session)
    assert ont.id not in _candidates(db_session, only_active=False)


def test_the_ont_vendor_string_does_not_decide_eligibility(db_session):
    """Vendor is tested on the OLT, not the ONT.

    Blank ONT vendor strings are widespread in the fleet; treating them as a
    disqualifier here would silently shrink the population.
    """
    ont = _ont(db_session, _olt(db_session), serial="HWTC-NO-VENDOR", vendor=None)

    assert ont.id in _candidates(db_session)


def test_an_inactive_ont_is_excluded_unless_explicitly_requested(db_session):
    ont = _ont(db_session, _olt(db_session), serial="HWTC-OFF", is_active=False)

    assert ont.id not in _candidates(db_session)
    assert ont.id in _candidates(db_session, only_active=False)
