"""``set_sync_status`` — the single owning writer of ``OntUnit.sync_status``.

Mirrors ``tests/test_ont_provisioning_status_transitions.py``'s shape for the
analogous ``set_provisioning_status`` setter.

Unlike ``OntProvisioningStatus`` (a 7-value lifecycle with genuinely illegal
edges -- see that test file's `provisioned -> pending_acs_registration`
rejection), ``OntSyncStatus`` has only three values (``synced``,
``reconciling``, ``out_of_sync``), and every one of the six possible
non-self edges between them is exercised by real production call sites
(``reconcile/core.py``, ``reconcile/locking.py``, ``reconcile/lifecycle.py``,
``network_subscriber_bridge.py`` -- see each module for the specific
transition it drives). So there is no illegal *transition* to construct a
rejection test for here; the misuse case this test suite proves instead is
an invalid *status value* (not one of the three enum members), which the
enum coercion at the top of ``set_sync_status`` rejects the same way
``OntSyncStatus("bogus")`` would.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.models.network import OntSyncStatus
from app.services.network.ont_status import set_sync_status


def _ont(status: OntSyncStatus) -> SimpleNamespace:
    return SimpleNamespace(id="ont-sync-test", sync_status=status)


def test_set_sync_status_allows_and_logs_a_legal_transition(caplog) -> None:
    ont = _ont(OntSyncStatus.synced)

    with caplog.at_level(logging.INFO, logger="app.services.network.ont_status"):
        set_sync_status(ont, OntSyncStatus.reconciling, reason="reconcile_started")

    assert ont.sync_status == OntSyncStatus.reconciling
    records = [r for r in caplog.records if r.message == "ont_status_transition"]
    assert len(records) == 1
    assert records[0].field == "sync_status"
    assert records[0].__dict__["from"] == "synced"
    assert records[0].to == "reconciling"
    assert records[0].valid is True
    assert records[0].reason == "reconcile_started"


def test_set_sync_status_covers_every_production_transition() -> None:
    """Plant each of the six non-self edges the real call sites drive and show
    none of them is rejected -- the sensitivity check for this table is that
    it is permissive by design, not that it blocks something.
    """
    pairs = [
        (OntSyncStatus.synced, OntSyncStatus.reconciling),  # core.py: start
        (OntSyncStatus.out_of_sync, OntSyncStatus.reconciling),  # core.py: retry
        (OntSyncStatus.reconciling, OntSyncStatus.synced),  # core.py: success
        (OntSyncStatus.reconciling, OntSyncStatus.out_of_sync),  # core.py/locking.py
        (OntSyncStatus.out_of_sync, OntSyncStatus.synced),  # lifecycle.py: retire
        (
            OntSyncStatus.synced,
            OntSyncStatus.out_of_sync,
        ),  # network_subscriber_bridge.py
    ]
    for current, target in pairs:
        ont = _ont(current)
        set_sync_status(ont, target)
        assert ont.sync_status == target


def test_set_sync_status_is_a_silent_noop_on_the_same_value(caplog) -> None:
    ont = _ont(OntSyncStatus.synced)

    with caplog.at_level(logging.INFO, logger="app.services.network.ont_status"):
        set_sync_status(ont, OntSyncStatus.synced)

    assert ont.sync_status == OntSyncStatus.synced
    assert not any(r.message == "ont_status_transition" for r in caplog.records)


def test_set_sync_status_accepts_a_string_value() -> None:
    ont = _ont(OntSyncStatus.reconciling)
    set_sync_status(ont, "out_of_sync")
    assert ont.sync_status == OntSyncStatus.out_of_sync


def test_set_sync_status_rejects_an_invalid_status_value() -> None:
    """Not a transition rejection (none exist in this table) -- an invalid
    *value* rejection. Plant a bogus string (must fail) and a near-miss real
    member (must not fail) to prove the coercion actually discriminates.
    """
    ont = _ont(OntSyncStatus.synced)

    with pytest.raises(ValueError):
        set_sync_status(ont, "not_a_real_sync_status")

    # Near-miss: an actual member is accepted without raising.
    set_sync_status(ont, OntSyncStatus.out_of_sync)
    assert ont.sync_status == OntSyncStatus.out_of_sync
