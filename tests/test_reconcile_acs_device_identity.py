"""ACS device identity is resolved, never synthesized.

A GenieACS ``_id`` is ``{OUI}-{ProductClass}-{SerialNumber}``. The reconciler
used to hardcode ``00259E`` (Huawei) + ``HG8546M`` into every id it wrote, so
every ONT of any other model — the fleet also runs EG8145V5 — received a
permanent NBI 404 on every reconcile, forever.

These tests pin the corrected contract:

* the real, recorded ``Tr069CpeDevice.genieacs_device_id`` is what gets written,
  for any ProductClass;
* the ``_id`` the ACS itself reports is an acceptable second source;
* missing, ambiguous, or self-contradicting identity fails closed with no ACS
  action at all;
* a device the ACS holds no document for is a wait-for-Inform condition, not a
  guaranteed-404 push;
* nothing in the reconciler package composes a device id from a literal OUI or
  ProductClass.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.network.reconcile import (
    AcsObservedFields,
    AcsSetPppoe,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    ReconcileFailureReason,
    compute_plan,
    resolve_acs_device_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECONCILE_PACKAGE = PROJECT_ROOT / "app" / "services" / "network" / "reconcile"

EG8145V5_ID = "00259E-EG8145V5-HWTCEG814501"
EG8145V5_SERIAL = "HWTCEG814501"


# ── Builders ────────────────────────────────────────────────────────────────


def _desired(**overrides) -> OntDesiredState:
    defaults = dict(
        ont_unit_id="ont-eg",
        serial_number=EG8145V5_SERIAL,
        olt_id="olt-spdc",
        fsp="0/1/3",
        olt_ont_id=12,
        line_profile_id=40,
        service_profile_id=42,
        description=f"{EG8145V5_SERIAL}_authd_20260727",
        mgmt_vlan=201,
        mgmt_ip="172.16.210.21",
        mgmt_subnet_mask="255.255.255.0",
        mgmt_gateway="172.16.210.1",
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        mgmt_iphost_priority=2,
        tr069_profile_id=2,
        acs_server_id="acs-dotmac",
        cr_username="admin",
        cr_password_ref="bao://cr",
        periodic_inform_interval_sec=300,
        wan_mode="pppoe",
        wan_vlan=203,
        wan_gem_index=1,
        wan_pppoe_username="100024999",
        wan_pppoe_password_ref="bao://pppoe",
        wan_pppoe_provisioning_method="tr069",
        wan_pppoe_wcd_index=1,
        wan_pppoe_instance_index=1,
        wan_config_profile_id=None,
        wan_internet_config_ip_index=None,
        nat_enabled=True,
        ipv6_enabled=False,
        dhcp_enabled=True,
        dhcp_pool_min="192.168.100.2",
        dhcp_pool_max="192.168.100.254",
        dhcp_subnet_mask="255.255.255.0",
        wifi_ssid="KURSI-EG",
        wifi_password_ref="bao://wifi",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=25,
        wan_service_port_index=24,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
        acs_device_id=EG8145V5_ID,
    )
    defaults.update(overrides)
    return OntDesiredState(**defaults)


def _olt_observed(**overrides) -> OltObservedFields:
    defaults = dict(
        olt_present=True,
        olt_match_state="match",
        olt_run_state="online",
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_service_ports=(),
    )
    defaults.update(overrides)
    return OltObservedFields(**defaults)


def _acs_observed(**overrides) -> AcsObservedFields:
    defaults = dict(
        acs_present=True,
        acs_last_inform_at=datetime(2026, 7, 27, tzinfo=UTC),
        acs_last_boot_at=None,
        acs_last_bootstrap_at=None,
        acs_observed_software_version=None,
        acs_observed_pppoe_username=None,
        acs_observed_pppoe_enable=None,
        acs_observed_wan_vlan=None,
        acs_observed_wan_external_ip=None,
        acs_observed_wan_connection_status=None,
        acs_observed_nat_enabled=None,
        acs_observed_dhcp_enabled=None,
        acs_observed_ssid=None,
        acs_observed_periodic_inform_interval_sec=None,
        acs_observed_cr_username=None,
        acs_observed_cr_username_set=None,
        acs_observed_cr_password_set=None,
        acs_observed_wan_wcd_index=None,
        acs_observed_wan_instance_index=None,
        acs_observed_wan_ppp_locations=(),
        acs_observed_device_id=EG8145V5_ID,
        acs_observed_device_match_count=1,
    )
    defaults.update(overrides)
    return AcsObservedFields(**defaults)


def _observed(**overrides) -> OntObservedState:
    olt = overrides.pop("olt", None) or _olt_observed()
    acs = overrides.pop("acs", None) or _acs_observed()
    return OntObservedState(
        last_reconciled_at=datetime(2026, 7, 27, tzinfo=UTC),
        last_reconcile_duration_ms=0,
        mgmt_ip_pingable=True,
        consecutive_sweep_unreachable=0,
        olt=olt,
        acs=acs,
    )


def _acs_device_ids(plan) -> set[str]:
    return {
        action.device_id
        for action in plan.actions
        if action.surface == "acs" and hasattr(action, "device_id")
    }


# ── The regression: a non-HG8546M ONT ───────────────────────────────────────


def test_eg8145v5_ont_uses_its_real_recorded_device_id():
    """The defect: every ACS write carried ``00259E-HG8546M-<serial>``.

    An EG8145V5's real ``_id`` has a different ProductClass, so every one of
    those writes was an NBI 404. Its recorded id must be used verbatim.
    """
    desired = _desired()
    plan = compute_plan(desired, _observed(), "sweep")

    acs_actions = [action for action in plan.actions if action.surface == "acs"]
    assert acs_actions, "an informed, drifted EG8145V5 must still get ACS writes"
    assert _acs_device_ids(plan) == {EG8145V5_ID}
    # The old placeholder must appear nowhere in the plan.
    assert not any("HG8546M" in device_id for device_id in _acs_device_ids(plan))
    assert plan.acs_wait_reason is None


def test_eg8145v5_device_id_is_taken_from_the_acs_when_no_cpe_row_exists():
    """A CPE record we never wrote is not a licence to invent one.

    The ACS itself reported this ``_id`` for this serial on this pass, so it is
    an observation — a legitimate second source, and still not a fabrication.
    """
    desired = _desired(acs_device_id=None)
    identity = resolve_acs_device_id(desired, _observed())

    assert identity.device_id == EG8145V5_ID
    assert identity.wait_reason is None

    plan = compute_plan(desired, _observed(), "sweep")
    assert _acs_device_ids(plan) == {EG8145V5_ID}


def test_recorded_device_id_wins_over_a_composed_guess():
    """Whatever the serial suggests, the recorded id is what gets written."""
    recorded = "48575443-EG8145V5-CUSTOM-RECORDED-ID"
    desired = _desired(acs_device_id=recorded)
    observed = _observed(acs=_acs_observed(acs_observed_device_id=None))

    identity = resolve_acs_device_id(desired, observed)

    assert identity.device_id == recorded


# ── Fail-closed cases ───────────────────────────────────────────────────────


def test_device_absent_from_acs_plans_no_acs_action_and_waits_for_inform():
    desired = _desired(acs_device_id=None)
    observed = _observed(
        olt=_olt_observed(olt_present=False),
        acs=_acs_observed(
            acs_present=False,
            acs_observed_device_id=None,
            acs_observed_device_match_count=0,
        ),
    )

    plan = compute_plan(desired, observed, "sync")

    assert not [action for action in plan.actions if action.surface == "acs"]
    assert plan.waiting_for_acs is True
    assert plan.acs_wait_reason == ReconcileFailureReason.ONT_NOT_INFORMING
    # OLT bring-up still happens: that is what lets the CPE reach the ACS.
    assert [action for action in plan.actions if action.surface == "olt"]


def test_absent_from_acs_waits_even_when_a_device_id_is_recorded():
    """A recorded id does not make a missing ACS document writable."""
    observed = _observed(
        acs=_acs_observed(
            acs_present=False,
            acs_observed_device_id=None,
            acs_observed_device_match_count=0,
        )
    )

    plan = compute_plan(_desired(), observed, "sweep")

    assert not [action for action in plan.actions if action.surface == "acs"]
    assert plan.acs_wait_reason == ReconcileFailureReason.ONT_NOT_INFORMING


def test_no_identity_anywhere_fails_closed():
    desired = _desired(acs_device_id=None)
    observed = _observed(acs=_acs_observed(acs_observed_device_id=None))

    identity = resolve_acs_device_id(desired, observed)

    assert identity.device_id is None
    assert identity.wait_reason == ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED
    assert not [
        action
        for action in compute_plan(desired, observed, "sweep").actions
        if action.surface == "acs"
    ]


def test_ambiguous_multi_device_match_fails_closed():
    """Two ACS documents end in this serial. Guessing one risks writing another
    customer's CPE, so nothing is written."""
    observed = _observed(acs=_acs_observed(acs_observed_device_match_count=2))

    identity = resolve_acs_device_id(_desired(), observed)

    assert identity.device_id is None
    assert identity.wait_reason == ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED
    assert "2 ACS devices match" in identity.detail


def test_recorded_and_reported_ids_disagreeing_fails_closed():
    observed = _observed(
        acs=_acs_observed(acs_observed_device_id="00259E-HG8546M-OTHERSERIAL")
    )

    identity = resolve_acs_device_id(_desired(), observed)

    assert identity.device_id is None
    assert identity.wait_reason == ReconcileFailureReason.ACS_IDENTITY_UNRESOLVED


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_recorded_id_is_not_an_identity(blank):
    desired = _desired(acs_device_id=blank)
    observed = _observed(acs=_acs_observed(acs_observed_device_id=None))

    assert resolve_acs_device_id(desired, observed).device_id is None


# ── Release gate ────────────────────────────────────────────────────────────


_OUI_LITERAL = re.compile(r"[\"'][0-9A-Fa-f]{6}-[A-Za-z0-9]+-")


def test_no_module_in_the_reconciler_composes_a_device_identifier():
    """Release gate: no ACS plan synthesizes a device identifier.

    Catches the exact shape of the original defect — a string literal that
    concatenates an OUI and a ProductClass into a device id — anywhere in the
    reconciler package.
    """
    offenders: list[str] = []
    for path in sorted(RECONCILE_PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if _OUI_LITERAL.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line!r}")

    assert not offenders, (
        "reconciler code composes a GenieACS device id from literal OUI / "
        "ProductClass values. The id must come from the persisted "
        "Tr069CpeDevice record or from the ACS itself:\n  " + "\n  ".join(offenders)
    )


def test_reader_observes_the_real_device_id_for_any_product_class():
    """The reader copies ``_id`` through verbatim, whatever the model is."""
    from app.services.network.reconcile.readers.acs_reader import read_acs_state

    class _Client:
        def list_devices(self, query=None, projection=None):
            return [
                {
                    "_id": EG8145V5_ID,
                    "_lastInform": "2026-07-27T00:00:00.000Z",
                    "InternetGatewayDevice": {},
                }
            ]

    result = read_acs_state(_Client(), _desired())

    assert result.success is True
    assert result.observed is not None
    assert result.observed.acs_observed_device_id == EG8145V5_ID
    assert result.observed.acs_observed_device_match_count == 1


def test_hex_serial_acs_document_still_plans_pppoe_for_linked_device_id():
    """The UI's canonical Huawei serial must not hide its linked CWMP document."""
    from app.services.network.reconcile.readers.acs_reader import read_acs_state

    device_id = "00259E-HG8546M-485754431DAF83D1"
    desired = _desired(
        serial_number="HWTC1DAF83D1",
        acs_device_id=device_id,
        wan_pppoe_username="100099999",
    )

    class _Client:
        def __init__(self):
            self.queries: list[dict[str, object] | None] = []

        def list_devices(self, query=None, projection=None):
            self.queries.append(query)
            return [
                {
                    "_id": device_id,
                    "_lastInform": "2026-08-12T01:15:26.285Z",
                    "InternetGatewayDevice": {
                        "WANDevice": {"1": {"WANConnectionDevice": {"1": {}}}}
                    },
                }
            ]

    client = _Client()
    acs_result = read_acs_state(client, desired)
    assert acs_result.observed is not None
    observed = _observed(acs=acs_result.observed)

    plan = compute_plan(desired, observed, "sync")

    assert client.queries == [{"_id": device_id}]
    pppoe = next(action for action in plan.actions if isinstance(action, AcsSetPppoe))
    assert pppoe.device_id == device_id
    assert plan.acs_wait_reason is None


def test_hex_serial_acs_document_plans_pppoe_from_observed_identity_fallback():
    from app.services.network.reconcile.readers.acs_reader import read_acs_state

    device_id = "00259E-HG8546M-485754431DAF83D1"
    desired = _desired(
        serial_number="HWTC1DAF83D1",
        acs_device_id=None,
        wan_pppoe_username="100099999",
    )

    class _Client:
        def list_devices(self, query=None, projection=None):
            return [
                {
                    "_id": device_id,
                    "InternetGatewayDevice": {
                        "WANDevice": {"1": {"WANConnectionDevice": {"1": {}}}}
                    },
                }
            ]

    acs_result = read_acs_state(_Client(), desired)
    assert acs_result.observed is not None

    plan = compute_plan(desired, _observed(acs=acs_result.observed), "sync")

    pppoe = next(action for action in plan.actions if isinstance(action, AcsSetPppoe))
    assert pppoe.device_id == device_id
    assert plan.acs_wait_reason is None


def test_reader_reports_an_ambiguous_match_count():
    from app.services.network.reconcile.readers.acs_reader import read_acs_state

    class _Client:
        def list_devices(self, query=None, projection=None):
            return [
                {"_id": EG8145V5_ID, "InternetGatewayDevice": {}},
                {
                    "_id": f"00259E-HG8546M-{EG8145V5_SERIAL}",
                    "InternetGatewayDevice": {},
                },
            ]

    result = read_acs_state(_Client(), _desired())

    assert result.observed is not None
    assert result.observed.acs_observed_device_match_count == 2


def test_reader_reports_absence_as_a_clean_read():
    from app.services.network.reconcile.readers.acs_reader import read_acs_state

    class _Client:
        def list_devices(self, query=None, projection=None):
            return []

    result = read_acs_state(_Client(), _desired())

    assert result.success is True
    assert result.unreachable is False
    assert result.observed is not None
    assert result.observed.acs_present is False
    assert result.observed.acs_observed_device_id is None
    assert result.observed.acs_observed_device_match_count == 0
