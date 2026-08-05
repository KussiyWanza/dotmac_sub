"""Regression tests for the ONT provisioning defect slice.

Covers three defects found during the 2026-08-04 turn-up incident:

* PPPoE WAN provisioning fell back to the TR-069 management WANConnectionDevice
  when no internet container existed, and its fail-closed branch was
  unreachable.
* A provisioning run that landed some phases reported a blanket ``failed``,
  inverting triage for a customer who was actually carrying traffic.
* An "already exists" service-port conflict that did not name an index failed
  the whole retry instead of resolving by exact readback.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.network.olt_ssh import ServicePortEntry
from app.services.network.ont_provision_steps import (
    _any_device_phase_succeeded,
    _igd_management_wcd_indexes,
    _resolve_igd_pppoe_wcd_index,
)


def _ont(connections: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        tr069_last_snapshot={
            "capabilities": {
                "wan": {
                    "data_model": "InternetGatewayDevice",
                    "connections": connections,
                }
            }
        }
    )


# ── PPPoE WANConnectionDevice selection ─────────────────────────────────────


def test_management_only_device_refuses_to_pick_a_wcd():
    """A freshly commissioned ONT exposes only the TR-069 container.

    Returning an index here is what caused PPPoE to be aimed at the management
    connection, producing a downstream "WANConnectionDevice is not empty"
    refusal that reads like stale device config.
    """
    ont = _ont([{"index": 1, "detected_wan_service": "TR069"}])

    assert _resolve_igd_pppoe_wcd_index(ont, {}, 203) is None


def test_management_wcd_is_never_selected_even_when_config_pack_points_at_it():
    ont = _ont([{"index": 1, "detected_wan_service": "TR069"}])

    assert _resolve_igd_pppoe_wcd_index(ont, {"pppoe_wcd_index": 1}, 203) is None


def test_snapshot_match_wins_when_an_internet_container_exists():
    ont = _ont(
        [
            {"index": 1, "detected_wan_service": "TR069"},
            {"index": 2, "detected_wan_service": "INTERNET", "detected_wan_vlan": 203},
        ]
    )

    assert _resolve_igd_pppoe_wcd_index(ont, {}, 203) == 2


def test_falls_back_to_configured_pppoe_wcd_index_when_snapshot_has_no_match():
    """The container may not exist yet; the config pack declares where it goes."""
    ont = _ont([{"index": 1, "detected_wan_service": "TR069"}])

    assert _resolve_igd_pppoe_wcd_index(ont, {"pppoe_wcd_index": 2}, 203) == 2


def test_no_snapshot_and_no_configured_index_fails_closed():
    ont = SimpleNamespace(tr069_last_snapshot=None)

    assert _resolve_igd_pppoe_wcd_index(ont, {}, 203) is None


def test_management_indexes_are_collected_from_the_snapshot():
    ont = _ont(
        [
            {"index": 1, "detected_wan_service": "TR069"},
            {"index": 4, "detected_wan_service": "tr069"},
            {"index": 2, "detected_wan_service": "INTERNET"},
        ]
    )

    assert _igd_management_wcd_indexes(ont) == {1, 4}


# ── Partial vs failed provisioning outcome ──────────────────────────────────


def test_partial_run_is_distinguishable_from_total_failure():
    assert (
        _any_device_phase_succeeded([{"phase": "internet_l2", "success": True}]) is True
    )
    assert (
        _any_device_phase_succeeded([{"phase": "internet_l2", "success": False}])
        is False
    )


def test_phases_without_a_success_key_are_not_assumed_successful():
    assert _any_device_phase_succeeded([{"phase": "prepare"}]) is False
    assert _any_device_phase_succeeded([]) is False


def test_preparatory_phases_do_not_make_a_failed_run_look_partial():
    """Resolving config and passing prerequisites touches no device.

    A run that got no further than preparation must still read ``failed``,
    otherwise a customer who never got service looks half-provisioned.
    """
    assert (
        _any_device_phase_succeeded(
            [
                {"phase": "config_pack_resolution", "success": True},
                {"phase": "prerequisite_validation", "success": True},
                {"phase": "olt_provisioning", "success": False, "subphases": []},
            ]
        )
        is False
    )


def test_device_work_nested_in_subphases_counts_as_partial():
    """``olt_provisioning`` carries its inner apply steps as subphases."""
    assert (
        _any_device_phase_succeeded(
            [
                {
                    "phase": "olt_provisioning",
                    "success": False,
                    "subphases": [{"phase": "internet_l2", "success": True}],
                }
            ]
        )
        is True
    )


# ── Service-port idempotency by exact readback ──────────────────────────────


def _entry(**overrides) -> ServicePortEntry:
    defaults = dict(
        index=555,
        vlan_id=203,
        ont_id=17,
        gem_index=1,
        flow_type="vlan",
        flow_para="203",
        state="up",
        fsp="0/1/3",
        tag_transform="translate",
    )
    defaults.update(overrides)
    return ServicePortEntry(**defaults)


def test_existing_service_port_is_resolved_by_intent_when_no_index_is_reported(
    monkeypatch,
):
    """The OLT says "has existed already" without naming an index."""
    from app.services.network import olt_ssh_service_ports as mod

    monkeypatch.setattr(
        mod,
        "get_service_ports_for_ont",
        lambda olt, fsp, ont_id: (True, "ok", [_entry()]),
    )

    ok, _message, port = mod._find_service_port_matching_intent(
        SimpleNamespace(name="boi-olt"),
        fsp="0/1/3",
        ont_id=17,
        gem_index=1,
        vlan_id=203,
        user_vlan=None,
        tag_transform="translate",
    )

    assert ok is True
    assert port is not None
    assert port.index == 555


def test_a_port_for_a_different_tuple_does_not_satisfy_the_conflict(monkeypatch):
    """Never treat someone else's service-port as our own idempotent success."""
    from app.services.network import olt_ssh_service_ports as mod

    monkeypatch.setattr(mod, "_SERVICE_PORT_VERIFY_DELAY_SEC", 0)
    monkeypatch.setattr(
        mod,
        "get_service_ports_for_ont",
        lambda olt, fsp, ont_id: (True, "ok", [_entry(vlan_id=201, gem_index=2)]),
    )

    ok, _message, port = mod._find_service_port_matching_intent(
        SimpleNamespace(name="boi-olt"),
        fsp="0/1/3",
        ont_id=17,
        gem_index=1,
        vlan_id=203,
        user_vlan=None,
        tag_transform="translate",
    )

    assert ok is False
    assert port is None
