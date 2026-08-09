"""UISP topology sync: upserts, edges, MAC matching and exclusions.

Runs against faked UISP payloads only (no network). Payload shapes follow the
live uisp.dotmac.ng NMS v2.1 responses.
"""

from __future__ import annotations

import logging
import uuid

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    DeviceType,
    OLTDevice,
    OntUnit,
    PonPort,
)
from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.models.ont_topology_observation import OntTopologyObservationEvidence
from app.models.subscriber import Subscriber
from app.services.topology.uisp_sync import ARCHIVE_SITE_ID, sync

# ---------------------------------------------------------------------------
# Fakes (shapes verified against the live instance)
# ---------------------------------------------------------------------------


class FakeUispClient:
    def __init__(
        self,
        devices=None,
        sites=None,
        stations_by_ap=None,
        onus_by_olt=None,
        onu_list_errors=None,
        data_links=None,
    ):
        self.devices = devices or []
        self.sites = sites or []
        self.stations_by_ap = stations_by_ap or {}
        self.onus_by_olt = onus_by_olt or {}
        self.onu_list_errors = set(onu_list_errors or ())
        self.data_links = data_links or []
        self.station_list_calls: list[str] = []
        self.onu_list_calls: list[str] = []

    def list_data_links(self):
        return self.data_links

    def list_devices(self):
        return self.devices

    def list_sites(self):
        return self.sites

    def list_airmax_stations(self, ap_id):
        self.station_list_calls.append(ap_id)
        return self.stations_by_ap.get(ap_id, [])

    def list_olt_onus(self, olt_id):
        self.onu_list_calls.append(olt_id)
        if olt_id in self.onu_list_errors:
            raise RuntimeError("UISP API request failed")
        return self.onus_by_olt.get(olt_id, [])


def _site(site_id, name="Endpoint", site_type="endpoint", parent_id=None):
    parent = {"id": parent_id, "name": "Parent"} if parent_id else None
    return {
        "id": site_id,
        "identification": {"name": name, "type": site_type, "parent": parent},
        "description": {"address": None, "location": None, "contact": None},
    }


def _device(
    device_id,
    name,
    *,
    role="station",
    device_type="airMax",
    mac=None,
    ip=None,
    site_id="site-endpoint-1",
    status="active",
    ap_device_id=None,
    parent_id=None,
    model="LBE-5AC-Gen2",
    firmware_version=None,
):
    attributes = {}
    if ap_device_id:
        attributes["apDevice"] = {"id": ap_device_id, "name": "AP"}
    if parent_id:
        attributes["parentId"] = parent_id
        attributes["isPartOfOlt"] = True
    identification = {
        "id": device_id,
        "name": name,
        "model": model,
        "mac": mac,
        "role": role,
        "type": device_type,
        "site": (
            {"id": site_id, "name": "Site", "type": "endpoint"} if site_id else None
        ),
    }
    if firmware_version:
        identification["firmwareVersion"] = firmware_version
    return {
        "identification": identification,
        "ipAddress": ip,
        "overview": {"status": status},
        "attributes": attributes or None,
    }


def _ap_node(db_session, name="AP-GARKI-SECTOR1", mgmt_ip="172.16.40.2"):
    node = NetworkDevice(
        name=name,
        hostname=name,
        mgmt_ip=mgmt_ip,
        role=DeviceRole.access,
        is_active=True,
    )
    db_session.add(node)
    db_session.flush()
    return node


def _active_subscription(db_session, subscriber, catalog_offer, mac):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        mac_address=mac,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


AP_ID = "0a0a0a0a-1111-2222-3333-444444444444"
STATION_ID = "b1b1b1b1-1111-2222-3333-555555555555"
STATION_MAC = "24:A4:3C:AA:BB:01"


def _wireless_payload(**station_kwargs):
    kwargs = {
        "role": "station",
        "mac": STATION_MAC,
        "ip": "192.168.1.1",
        "ap_device_id": AP_ID,
    }
    kwargs.update(station_kwargs)
    ap = _device(
        AP_ID,
        "AP-GARKI-SECTOR1",
        role="ap",
        ip="172.16.40.2/24",
        mac="24:A4:3C:00:00:01",
        site_id="site-bts-1",
    )
    station = _device(STATION_ID, "CUST-JOHN-DOE", **kwargs)
    return [ap, station]


# ---------------------------------------------------------------------------
# Wireless radios -> cpe_devices (+ CPE -> AP edge)
# ---------------------------------------------------------------------------


def test_station_creates_cpe_with_edge_to_matched_ap(
    db_session, subscriber, catalog_offer
):
    node = _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.device_type == DeviceType.wireless_radio
    assert cpe.mac_address == STATION_MAC
    assert cpe.model == "LBE-5AC-Gen2"
    assert cpe.vendor == "ubiquiti"
    assert cpe.last_uisp_status == "active"
    assert cpe.uisp_synced_at is not None
    assert cpe.parent_network_device_id == node.id
    # Match-then-create: the row is born with its owner set.
    assert cpe.subscriber_id == subscriber.id
    db_session.refresh(node)
    assert node.uisp_device_id == AP_ID
    assert result["created"] == 1
    assert result["matched"] == 1
    assert result["edges_set"] == 1
    assert result["aps_matched"] == 1


def test_second_run_is_idempotent(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())

    first = sync(db_session, client)
    second = sync(db_session, client)

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["unchanged"] == 1
    assert second["edges_set"] == 0
    assert second["matched"] == 0
    assert db_session.query(CPEDevice).count() == 1


def test_sync_refreshes_observed_cpe_firmware(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    first_payload = _wireless_payload()
    first_payload[1]["identification"]["firmwareVersion"] = "8.7.19"
    sync(db_session, FakeUispClient(devices=first_payload))
    cpe = db_session.query(CPEDevice).filter_by(uisp_device_id=STATION_ID).one()
    assert cpe.firmware_version == "8.7.19"

    second_payload = _wireless_payload()
    second_payload[1]["identification"]["firmwareVersion"] = "8.7.20"
    result = sync(db_session, FakeUispClient(devices=second_payload))

    db_session.refresh(cpe)
    assert cpe.firmware_version == "8.7.20"
    assert result["updated"] == 1


def test_sync_never_overwrites_human_set_fields(db_session, subscriber):
    _ap_node(db_session)
    existing = CPEDevice(
        uisp_device_id=STATION_ID,
        subscriber_id=subscriber.id,
        device_type=DeviceType.router,
        model="Operator Model",
        mac_address="FF:FF:FF:FF:FF:FF",
    )
    db_session.add(existing)
    db_session.flush()
    client = FakeUispClient(devices=_wireless_payload())

    sync(db_session, client)

    db_session.refresh(existing)
    # Human-set values survive; the placeholder-only upgrade never fires.
    assert existing.model == "Operator Model"
    assert existing.mac_address == "FF:FF:FF:FF:FF:FF"
    assert existing.device_type == DeviceType.router
    assert existing.subscriber_id == subscriber.id
    # NULL fields are filled from UISP.
    assert existing.vendor == "ubiquiti"


def test_sync_upgrades_placeholder_device_type(db_session, subscriber):
    _ap_node(db_session)
    existing = CPEDevice(
        uisp_device_id=STATION_ID,
        subscriber_id=subscriber.id,
        device_type=DeviceType.other,
    )
    db_session.add(existing)
    db_session.flush()
    client = FakeUispClient(devices=_wireless_payload())

    sync(db_session, client)

    db_session.refresh(existing)
    assert existing.device_type == DeviceType.wireless_radio


def test_ap_side_station_list_fallback_sets_edge(db_session, subscriber, catalog_offer):
    node = _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    devices = _wireless_payload(ap_device_id=None)
    client = FakeUispClient(
        devices=devices,
        stations_by_ap={
            AP_ID: [{"deviceIdentification": {"id": STATION_ID}, "mac": STATION_MAC}]
        },
    )

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.parent_network_device_id == node.id
    assert result["edges_set"] == 1
    assert client.station_list_calls == [AP_ID]


# ---------------------------------------------------------------------------
# RF signal ingestion (AP-side station RSSI -> cpe_devices current value)
# ---------------------------------------------------------------------------


def _station_entry(signal=-62.0, **extra):
    entry = {
        "deviceIdentification": {"id": STATION_ID},
        "mac": STATION_MAC,
    }
    if signal is not None:
        entry["rxSignal"] = signal
    entry.update(extra)
    return entry


def test_ap_station_list_is_fetched_unconditionally(
    db_session, subscriber, catalog_offer
):
    # Even when every station carries attributes.apDevice, the AP-side list
    # is still fetched: it is the only source of the radio's RF signal.
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(
        devices=_wireless_payload(),  # station HAS apDevice
        stations_by_ap={AP_ID: [_station_entry()]},
    )

    sync(db_session, client)

    assert client.station_list_calls == [AP_ID]


def test_active_station_records_rf_signal(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(
        devices=_wireless_payload(),
        stations_by_ap={AP_ID: [_station_entry(signal=-58.5)]},
    )

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.rf_signal_dbm == -58.5
    assert cpe.rf_signal_source == "uisp_ap_station"
    assert cpe.rf_signal_observed_at is not None
    assert result["signals_recorded"] == 1


def test_disconnected_station_clears_rf_signal(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    sync(
        db_session,
        FakeUispClient(
            devices=_wireless_payload(),
            stations_by_ap={AP_ID: [_station_entry()]},
        ),
    )
    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.rf_signal_dbm is not None
    assert cpe.rf_signal_observed_at is not None

    result = sync(
        db_session,
        FakeUispClient(devices=_wireless_payload(status="disconnected")),
    )

    db_session.refresh(cpe)
    assert cpe.rf_signal_dbm is None
    assert cpe.rf_signal_source is None
    assert cpe.rf_signal_observed_at is None
    assert result["signals_cleared"] == 1


def test_active_station_without_ap_entry_retains_prior_signal(
    db_session, subscriber, catalog_offer
):
    # AP list call failing (or AP unmatched) must NOT clear a prior value:
    # absence of evidence is not evidence of absence — the read-side TTL
    # degrades it to stale instead.
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    sync(
        db_session,
        FakeUispClient(
            devices=_wireless_payload(),
            stations_by_ap={AP_ID: [_station_entry(signal=-60.0)]},
        ),
    )

    result = sync(
        db_session,
        FakeUispClient(devices=_wireless_payload()),  # no AP-side entries
    )

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.rf_signal_dbm == -60.0
    assert result["signals_cleared"] == 0


def test_out_of_range_rssi_is_rejected(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(
        devices=_wireless_payload(),
        # 95 is a percent-style quality number, not dBm; True is a bool trap.
        stations_by_ap={
            AP_ID: [
                {
                    "deviceIdentification": {"id": STATION_ID},
                    "mac": STATION_MAC,
                    "rxSignal": 95,
                    "signal": True,
                }
            ]
        },
    )

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.rf_signal_dbm is None
    assert result["signals_recorded"] == 0


def test_prune_to_missing_clears_rf_signal(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    sync(
        db_session,
        FakeUispClient(
            devices=_wireless_payload(),
            stations_by_ap={AP_ID: [_station_entry()]},
        ),
    )

    # The station disappears while another station keeps the inventory
    # non-empty (prune guard requires a non-empty station list).
    other = Subscriber(
        first_name="Still",
        last_name="There",
        email=f"there-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    other_mac = "24:A4:3C:AA:BB:77"
    _active_subscription(db_session, other, catalog_offer, other_mac)
    survivor = _device(
        "77777777-1111-2222-3333-444444444444",
        "CUST-STILL-THERE",
        mac=other_mac,
        ap_device_id=AP_ID,
    )
    result = sync(
        db_session,
        FakeUispClient(devices=[_wireless_payload()[0], survivor]),
    )

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.last_uisp_status == "missing"
    assert cpe.rf_signal_dbm is None
    assert cpe.rf_signal_observed_at is None
    assert result["signals_cleared"] >= 1


# ---------------------------------------------------------------------------
# Unmatched-radio review queue wiring (sync-side hook)
# ---------------------------------------------------------------------------


def _queue_items(db_session):
    from app.services import unmatched_radio_queue

    return unmatched_radio_queue.open_items(db_session)


def test_unmatched_station_opens_review_item(db_session):
    from app.services.unmatched_radio_queue import REASON_UISP_UNMATCHED

    _ap_node(db_session)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    items = _queue_items(db_session)
    assert len(items) == 1
    meta = items[0].metadata_
    assert meta["reason"] == REASON_UISP_UNMATCHED
    assert meta["radio_mac"] == STATION_MAC.replace(":", "").lower()
    assert meta["uisp_device_id"] == STATION_ID
    assert result["queue_opened"] == 1


def test_unmatched_station_rerun_bumps_not_duplicates(db_session):
    _ap_node(db_session)
    sync(db_session, FakeUispClient(devices=_wireless_payload()))

    result = sync(db_session, FakeUispClient(devices=_wireless_payload()))

    items = _queue_items(db_session)
    assert len(items) == 1
    assert items[0].metadata_["occurrences"] == 2
    assert result["queue_bumped"] == 1
    assert result["queue_opened"] == 0


def test_ambiguous_station_opens_conflict_item(db_session, subscriber, catalog_offer):
    from app.services.unmatched_radio_queue import REASON_CONFLICT

    _ap_node(db_session)
    other = Subscriber(
        first_name="Other",
        last_name="Person",
        email=f"other-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    _active_subscription(db_session, other, catalog_offer, STATION_MAC)

    result = sync(db_session, FakeUispClient(devices=_wireless_payload()))

    items = _queue_items(db_session)
    assert len(items) == 1
    assert items[0].metadata_["reason"] == REASON_CONFLICT
    assert result["queue_opened"] == 1


def test_matched_station_with_unmatched_ap_opens_ap_unresolved_item(
    db_session, subscriber, catalog_offer
):
    from app.services.unmatched_radio_queue import REASON_AP_UNRESOLVED

    # No network_devices row matches the AP: the CPE row is created but cannot
    # be parented — the repairable "AP unresolved" gap becomes a work item.
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)

    result = sync(db_session, FakeUispClient(devices=_wireless_payload()))

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.parent_network_device_id is None
    items = _queue_items(db_session)
    assert len(items) == 1
    meta = items[0].metadata_
    assert meta["reason"] == REASON_AP_UNRESOLVED
    assert meta["ap_uisp_id"] == AP_ID
    assert result["queue_opened"] == 1
    assert result["aps_unmatched"] == 1


def test_matched_and_parented_station_opens_nothing(
    db_session, subscriber, catalog_offer
):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)

    result = sync(db_session, FakeUispClient(devices=_wireless_payload()))

    assert _queue_items(db_session) == []
    assert result["queue_opened"] == 0


def test_ptp_master_matching_network_device_is_not_created(db_session):
    # PtP backhaul masters report role=station but are infrastructure: a name
    # match against network_devices must not create a duplicate CPE.
    backhaul = NetworkDevice(
        name="BH-GARKI-KUBWA", role=DeviceRole.access, is_active=True
    )
    db_session.add(backhaul)
    db_session.flush()
    devices = [
        _device(
            "c2c2c2c2-1111-2222-3333-666666666666",
            "BH-GARKI-KUBWA",
            role="station",
            ip="172.16.50.9/24",
        )
    ]
    client = FakeUispClient(devices=devices)

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["skipped"] == 1
    assert result["created"] == 0


def test_flush_failure_is_isolated_and_prior_work_survives(
    db_session, subscriber, catalog_offer
):
    # A mid-run flush failure (unbindable value -> DBAPI error, standing in
    # for a Postgres IntegrityError that would abort the transaction) must be
    # rolled back to its own savepoint: the run completes, earlier and later
    # devices' work survives, and only the bad device is counted as failed.
    node = _ap_node(db_session)
    for mac in ("24:A4:3C:AA:BB:10", "24:A4:3C:AA:BB:12", "24:A4:3C:AA:BB:11"):
        other = Subscriber(
            first_name="Sub",
            last_name=mac[-2:],
            email=f"sub-{uuid.uuid4().hex[:8]}@example.test",
        )
        db_session.add(other)
        db_session.flush()
        _active_subscription(db_session, other, catalog_offer, mac)
    station_a = _device(
        "aaaaaaaa-0000-0000-0000-00000000000a",
        "CUST-BEFORE",
        mac="24:A4:3C:AA:BB:10",
        ap_device_id=AP_ID,
    )
    station_bad = _device(
        "bbbbbbbb-0000-0000-0000-00000000000b",
        "CUST-BROKEN",
        mac="24:A4:3C:AA:BB:12",
        model={"unbindable": True},  # matched subscriber, fails at flush()
        ap_device_id=AP_ID,
    )
    station_c = _device(
        "cccccccc-0000-0000-0000-00000000000c",
        "CUST-AFTER",
        mac="24:A4:3C:AA:BB:11",
        ap_device_id=AP_ID,
    )
    ap = _device(
        AP_ID,
        "AP-GARKI-SECTOR1",
        role="ap",
        ip="172.16.40.2/24",
        site_id="site-bts-1",
    )
    client = FakeUispClient(devices=[ap, station_a, station_bad, station_c])

    result = sync(db_session, client)

    assert result["failed"] == 1
    assert result["created"] == 2
    macs = {
        cpe.mac_address
        for cpe in db_session.query(CPEDevice).all()
        if cpe.uisp_device_id
    }
    assert macs == {"24:A4:3C:AA:BB:10", "24:A4:3C:AA:BB:11"}
    # The session survived the failure: edges were still written afterwards.
    after = (
        db_session.query(CPEDevice)
        .filter(CPEDevice.uisp_device_id == "cccccccc-0000-0000-0000-00000000000c")
        .one()
    )
    assert after.parent_network_device_id == node.id


def test_failed_upsert_does_not_mark_device_vanished(db_session, subscriber):
    # The seen-set is built from the raw UISP inventory, not from successful
    # upserts: a device whose upsert fails transiently is still reported by
    # UISP and must not be soft-pruned to 'vanished'.
    existing = CPEDevice(
        uisp_device_id=STATION_ID,
        subscriber_id=subscriber.id,
        mac_address=None,
        last_uisp_status="active",
    )
    db_session.add(existing)
    db_session.flush()
    devices = [_device(STATION_ID, "CUST-JOHN-DOE", mac={"unbindable": True})]
    client = FakeUispClient(devices=devices)

    result = sync(db_session, client)

    assert result["failed"] == 1
    assert result["pruned"] == 0
    db_session.refresh(existing)
    assert existing.last_uisp_status == "active"


def test_empty_inventory_does_not_mass_prune_radios(
    db_session, subscriber, catalog_offer
):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())
    sync(db_session, client)

    result = sync(db_session, FakeUispClient(devices=[]))

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.last_uisp_status == "active"
    assert result["pruned"] == 0
    assert result["prune_guarded"] == 1


def test_partial_inventory_requires_two_missing_passes_before_prune(
    db_session, subscriber, catalog_offer
):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    sync(db_session, FakeUispClient(devices=_wireless_payload()))

    other = Subscriber(
        first_name="Still",
        last_name="Visible",
        email=f"visible-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    visible_mac = "24:A4:3C:AA:BB:99"
    _active_subscription(db_session, other, catalog_offer, visible_mac)
    visible_station = _device(
        "99999999-1111-2222-3333-444444444444",
        "Still Visible",
        mac=visible_mac,
        ap_device_id=AP_ID,
    )
    partial = [_wireless_payload()[0], visible_station]

    first = sync(db_session, FakeUispClient(devices=partial))
    missing = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert missing.last_uisp_status == "missing"
    assert first["pruned"] == 0

    second = sync(db_session, FakeUispClient(devices=partial))
    db_session.refresh(missing)
    assert missing.last_uisp_status == "vanished"
    assert second["pruned"] == 1


# ---------------------------------------------------------------------------
# Edge-move breadcrumbs (auditability for the silent auto-heal)
# ---------------------------------------------------------------------------

AP2_ID = "0b0b0b0b-1111-2222-3333-444444444445"


def _two_ap_payload(station_ap_id):
    ap1 = _device(
        AP_ID,
        "AP-GARKI-SECTOR1",
        role="ap",
        ip="172.16.40.2/24",
        mac="24:A4:3C:00:00:01",
        site_id="site-bts-1",
    )
    ap2 = _device(
        AP2_ID,
        "AP-GUDU-SECTOR2",
        role="ap",
        ip="172.16.40.3/24",
        mac="24:A4:3C:00:00:02",
        site_id="site-bts-2",
    )
    station = _device(
        STATION_ID,
        "CUST-JOHN-DOE",
        role="station",
        mac=STATION_MAC,
        ip="192.168.1.1",
        ap_device_id=station_ap_id,
    )
    return [ap1, ap2, station]


def _edge_move_records(caplog):
    return [
        r for r in caplog.records if getattr(r, "event", "") == "uisp_sync_edge_moved"
    ]


def test_radio_reparent_emits_edge_move_breadcrumb(
    db_session, subscriber, catalog_offer, caplog
):
    node_a = _ap_node(db_session)
    node_b = _ap_node(db_session, name="AP-GUDU-SECTOR2", mgmt_ip="172.16.40.3")
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)

    with caplog.at_level(logging.INFO, logger="app.services.topology.uisp_sync"):
        first = sync(db_session, FakeUispClient(devices=_two_ap_payload(AP_ID)))
    # First-time parenting is a fill, not a move: no breadcrumb, no counter.
    assert first["edges_set"] == 1
    assert first["edges_moved"] == 0
    assert _edge_move_records(caplog) == []

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.topology.uisp_sync"):
        second = sync(db_session, FakeUispClient(devices=_two_ap_payload(AP2_ID)))

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.parent_network_device_id == node_b.id
    assert second["edges_set"] == 1
    assert second["edges_moved"] == 1
    (record,) = _edge_move_records(caplog)
    assert record.device_name == "CUST-JOHN-DOE"
    assert record.uisp_device_id == STATION_ID
    assert record.old_parent_id == str(node_a.id)
    assert record.old_parent_name == "AP-GARKI-SECTOR1"
    assert record.new_parent_id == str(node_b.id)
    assert record.new_parent_name == "AP-GUDU-SECTOR2"


def test_reparent_second_run_is_stable_no_further_moves(
    db_session, subscriber, catalog_offer, caplog
):
    _ap_node(db_session)
    _ap_node(db_session, name="AP-GUDU-SECTOR2", mgmt_ip="172.16.40.3")
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    sync(db_session, FakeUispClient(devices=_two_ap_payload(AP_ID)))
    sync(db_session, FakeUispClient(devices=_two_ap_payload(AP2_ID)))

    with caplog.at_level(logging.INFO, logger="app.services.topology.uisp_sync"):
        # Drop the (legitimate) reparent breadcrumb from the run above; only the
        # stable third run should be asserted on here.
        caplog.clear()
        third = sync(db_session, FakeUispClient(devices=_two_ap_payload(AP2_ID)))

    assert third["edges_set"] == 0
    assert third["edges_moved"] == 0
    assert _edge_move_records(caplog) == []


# ---------------------------------------------------------------------------
# Archive-site exclusion
# ---------------------------------------------------------------------------


def test_archive_site_devices_are_excluded(db_session):
    child_site = "site-archived-endpoint"
    sites = [
        _site(ARCHIVE_SITE_ID, name="Archive", site_type="site"),
        _site(child_site, parent_id=ARCHIVE_SITE_ID),
    ]
    devices = [
        _device(
            "d1d1d1d1-0000-0000-0000-000000000001",
            "ARCHIVED-DIRECT",
            site_id=ARCHIVE_SITE_ID,
            mac="24:A4:3C:AA:BB:02",
        ),
        _device(
            "d1d1d1d1-0000-0000-0000-000000000002",
            "ARCHIVED-ENDPOINT",
            site_id=child_site,
            mac="24:A4:3C:AA:BB:03",
        ),
    ]
    client = FakeUispClient(devices=devices, sites=sites)

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["excluded_archive"] == 2
    assert result["created"] == 0


# ---------------------------------------------------------------------------
# Subscriber MAC matching (match-then-create: NOT NULL subscriber_id)
# ---------------------------------------------------------------------------


def test_model_enforces_subscriber_not_null():
    # Regression guard for the prod NotNullViolation: the model (and thus the
    # test schema) must carry the same NOT NULL the production schema enforces,
    # so any INSERT attempt with subscriber_id=None fails loudly in tests too.
    assert CPEDevice.__table__.c.subscriber_id.nullable is False


def test_exact_mac_match_links_active_subscriber(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, "24a43caabb01")
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.subscriber_id == subscriber.id
    assert result["matched"] == 1
    assert result["ambiguous"] == 0


def test_unmatched_station_creates_no_row(db_session):
    # No ACTIVE subscription carries this MAC: with subscriber_id NOT NULL the
    # row cannot exist yet, so nothing is created (and nothing FAILS — the
    # insert is never attempted). The radio is retried on later runs.
    _ap_node(db_session)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["created"] == 0
    assert result["failed"] == 0
    assert result["unmatched_no_subscriber"] == 1
    assert result["matched"] == 0


def test_ambiguous_mac_creates_no_row(db_session, subscriber, catalog_offer):
    _ap_node(db_session)
    other = Subscriber(
        first_name="Other",
        last_name="Person",
        email=f"other-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    _active_subscription(db_session, other, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["ambiguous"] == 1
    assert result["matched"] == 0
    assert result["created"] == 0
    assert result["failed"] == 0


def test_inactive_subscription_mac_does_not_match(
    db_session, subscriber, catalog_offer
):
    _ap_node(db_session)
    subscription = _active_subscription(
        db_session, subscriber, catalog_offer, STATION_MAC
    )
    subscription.status = SubscriptionStatus.canceled
    db_session.flush()
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["matched"] == 0
    assert result["unmatched_no_subscriber"] == 1


def test_existing_row_subscriber_is_never_relinked(
    db_session, subscriber, catalog_offer
):
    # Matching decides CREATION only. An existing row keeps its operator-set
    # owner even when ACTIVE subscriptions now map the MAC to someone else.
    _ap_node(db_session)
    owner = Subscriber(
        first_name="Original",
        last_name="Owner",
        email=f"owner-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(owner)
    db_session.flush()
    existing = CPEDevice(
        uisp_device_id=STATION_ID,
        subscriber_id=owner.id,
        mac_address=STATION_MAC,
    )
    db_session.add(existing)
    db_session.flush()
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    db_session.refresh(existing)
    assert existing.subscriber_id == owner.id
    assert result["matched"] == 0
    assert result["created"] == 0


# ---------------------------------------------------------------------------
# Adoption of install-time pre-registered rows (PR #807 follow-up contract)
# ---------------------------------------------------------------------------


def _preregistered_row(db_session, owner, mac=STATION_MAC, **kwargs):
    """A cpe row as radio_registration.register_radio_mac creates it:
    subscriber known, canonical MAC, uisp_device_id NULL."""
    kwargs.setdefault("uisp_device_id", None)
    row = CPEDevice(
        subscriber_id=owner.id,
        device_type=DeviceType.wireless_radio,
        mac_address=mac,
        **kwargs,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_preregistered_row_is_adopted_not_duplicated(db_session, subscriber):
    # No active subscription MAC at all: adoption must precede (and not need)
    # the subscription-MAC index — the install flow already knows the owner.
    node = _ap_node(db_session)
    row = _preregistered_row(db_session, subscriber)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 1
    db_session.refresh(row)
    assert row.uisp_device_id == STATION_ID
    assert row.subscriber_id == subscriber.id  # never touched
    assert row.model == "LBE-5AC-Gen2"  # fill-if-NULL ran as usual
    assert row.last_uisp_status == "active"
    assert row.uisp_synced_at is not None
    assert row.parent_network_device_id == node.id
    assert result["adopted"] == 1
    assert result["created"] == 0
    assert result["matched"] == 0
    assert result["unmatched_no_subscriber"] == 0
    assert result["edges_set"] == 1

    second = sync(db_session, client)
    assert second["adopted"] == 0
    assert second["unchanged"] == 1
    assert db_session.query(CPEDevice).count() == 1


def test_row_bound_to_another_uisp_id_is_never_adopted(db_session, subscriber):
    # Same MAC, but the row already belongs to a different UISP device (e.g.
    # a swapped radio whose old row kept the MAC): adoption must not steal it.
    _ap_node(db_session)
    row = _preregistered_row(
        db_session, subscriber, uisp_device_id="99999999-0000-0000-0000-000000000099"
    )
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert result["adopted"] == 0
    assert result["created"] == 0
    assert result["unmatched_no_subscriber"] == 1
    db_session.refresh(row)
    assert row.uisp_device_id == "99999999-0000-0000-0000-000000000099"
    assert db_session.query(CPEDevice).count() == 1


def test_multiple_preregistered_candidates_adopt_none(
    db_session, subscriber, catalog_offer
):
    # Two NULL-uisp rows carry the station's MAC (shouldn't happen via the
    # registration flow, but defense in depth): adopting either would guess,
    # and creating a third row would compound the duplication — so the sync
    # does neither, even though the subscription MAC index WOULD match.
    _ap_node(db_session)
    other = Subscriber(
        first_name="Other",
        last_name="Owner",
        email=f"other-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    _preregistered_row(db_session, subscriber)
    _preregistered_row(db_session, other)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert result["adopted"] == 0
    assert result["created"] == 0
    assert result["ambiguous"] == 1
    assert db_session.query(CPEDevice).count() == 2
    assert (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id.isnot(None)).count()
        == 0
    )


def test_retired_placeholder_is_not_adopted(db_session, subscriber):
    # The hourly unmatched-radio review retires superseded placeholders;
    # a retired tombstone must never be resurrected by adoption.
    _ap_node(db_session)
    row = _preregistered_row(db_session, subscriber, status=DeviceStatus.retired)
    client = FakeUispClient(devices=_wireless_payload())

    result = sync(db_session, client)

    assert result["adopted"] == 0
    assert result["unmatched_no_subscriber"] == 1
    db_session.refresh(row)
    assert row.uisp_device_id is None
    assert db_session.query(CPEDevice).count() == 1


# ---------------------------------------------------------------------------
# UFiber: UF-OLTs -> olt_devices, ONUs -> ont_units
# ---------------------------------------------------------------------------

OLT_ID = "e1e1e1e1-1111-2222-3333-777777777777"
ONU_ID = "f1f1f1f1-1111-2222-3333-888888888888"


def _ufiber_payload():
    olt = _device(
        OLT_ID,
        "GPON-GARKI-1",
        role="gpon",
        device_type="olt",
        ip="172.16.60.2/24",
        mac="24:A4:3C:11:22:33",
        model="UF-OLT",
        site_id="site-bts-1",
    )
    onu = _device(
        ONU_ID,
        "ONU-CUST-42",
        role="station",
        device_type="onu",
        mac="24:A4:3C:44:55:66",
        ip=None,
        parent_id=OLT_ID,
        model="UF-LOCO",
    )
    return [olt, onu]


def _satisfies_olt_config_pack_check(olt) -> bool:
    """Python replica of ``ck_olt_devices_config_pack_required``.

    SQLite does not carry this DB-level check (it is created by migrations
    076/154, not the model), so tests assert the predicate directly. Per
    migration 154: ``NOT is_active OR (config_pack ->> internet_vlan_id /
    management_vlan_id / tr069_olt_profile_id are all non-null)``.
    """
    if not olt.is_active:
        return True
    pack = olt.config_pack or {}
    return all(
        pack.get(key) is not None
        for key in ("internet_vlan_id", "management_vlan_id", "tr069_olt_profile_id")
    )


def test_ufiber_olt_and_onu_upsert(db_session):
    client = FakeUispClient(devices=_ufiber_payload())

    result = sync(db_session, client)

    olt = db_session.query(OLTDevice).filter(OLTDevice.uisp_device_id == OLT_ID).one()
    assert olt.name == "GPON-GARKI-1"
    assert olt.vendor == "ubiquiti"
    assert olt.mgmt_ip == "172.16.60.2"
    # The parent resolved: ONUs are no longer skipped once the OLT row exists.
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.olt_device_id == olt.id
    assert ont.last_sync_source == "uisp"
    assert ont.pon_port_id is None
    assert ont.vendor == "ubiquiti"
    assert ont.model == "UF-LOCO"
    assert result["created"] == 2

    second = sync(db_session, client)
    assert second["created"] == 0
    assert second["unchanged"] == 2


def test_created_uf_olt_satisfies_config_pack_check(db_session):
    # Prod's ck_olt_devices_config_pack_required rejected every UF-OLT INSERT
    # (active + empty config_pack). UISP-managed OLTs are monitoring/topology
    # objects, not provisioning targets: they are created INACTIVE, the
    # combination the constraint exempts.
    client = FakeUispClient(devices=_ufiber_payload())

    sync(db_session, client)

    olt = db_session.query(OLTDevice).filter(OLTDevice.uisp_device_id == OLT_ID).one()
    assert olt.is_active is False
    assert _satisfies_olt_config_pack_check(olt)


def test_ufiber_olt_matches_existing_row_instead_of_creating(db_session):
    existing = OLTDevice(name="GPON-GARKI-1", vendor=None)
    db_session.add(existing)
    db_session.flush()
    client = FakeUispClient(devices=_ufiber_payload())

    sync(db_session, client)

    assert db_session.query(OLTDevice).count() == 1
    db_session.refresh(existing)
    assert existing.uisp_device_id == OLT_ID
    assert existing.vendor == "ubiquiti"
    # A matched pre-existing row keeps its operator-set activation state:
    # only rows this sync CREATES are inactive placeholders.
    assert existing.is_active is True


def test_onu_without_resolvable_parent_is_skipped(db_session):
    onu = _device(
        ONU_ID,
        "ONU-ORPHAN",
        device_type="onu",
        mac="24:A4:3C:44:55:77",
        parent_id="00000000-dead-beef-0000-000000000000",
    )
    client = FakeUispClient(devices=[onu])

    result = sync(db_session, client)

    assert db_session.query(OntUnit).count() == 0
    assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# UFiber PON-port granularity (per-OLT ONU listings, onu.port)
# ---------------------------------------------------------------------------


def _olt_onu_listing_entry(onu_id, port, *, parent_id=OLT_ID, mac="24:A4:3C:44:55:66"):
    """One GET /devices/onus?parentId=<olt> entry: /devices shape + onu.port."""
    entry = _device(
        onu_id,
        "ONU-CUST-42",
        role="station",
        device_type="onu",
        mac=mac,
        parent_id=parent_id,
        model="UF-LOCO",
    )
    entry["onu"] = {"id": onu_id, "port": port, "profile": "profile-small"}
    return entry


def test_onu_port_creates_pon_port_and_sets_onu(db_session):
    client = FakeUispClient(
        devices=_ufiber_payload(),
        onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
    )

    result = sync(db_session, client)

    olt = db_session.query(OLTDevice).filter(OLTDevice.uisp_device_id == OLT_ID).one()
    port = db_session.query(PonPort).one()
    assert port.olt_id == olt.id
    assert port.port_number == 3
    assert port.name == "pon3"
    assert "observation owner" in (port.notes or "")
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.pon_port_id == port.id
    assert result["pon_ports_initialized"] == 1
    assert result["onu_topology_initialized"] == 1
    assert result["onu_topology_confirmed"] == 0
    assert client.onu_list_calls == [OLT_ID]


def test_second_run_pon_ports_are_idempotent(db_session):
    client = FakeUispClient(
        devices=_ufiber_payload(),
        onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
    )

    first = sync(db_session, client)
    second = sync(db_session, client)

    assert first["pon_ports_initialized"] == 1
    assert second["pon_ports_initialized"] == 0
    assert second["onu_topology_initialized"] == 0
    assert second["onu_topology_confirmed"] == 1
    assert db_session.query(PonPort).count() == 1
    evidence = db_session.query(OntTopologyObservationEvidence).one()
    assert evidence.seen_count == 2
    assert evidence.initial_outcome == "initialized"
    assert evidence.latest_outcome == "confirmed"


def test_onu_move_between_uisp_olts_requires_review(db_session):
    second_olt_id = "e2e2e2e2-1111-2222-3333-777777777777"
    first_devices = _ufiber_payload()
    sync(
        db_session,
        FakeUispClient(
            devices=first_devices,
            onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
        ),
    )

    second_olt = _device(
        second_olt_id,
        "GPON-GARKI-2",
        role="gpon",
        device_type="olt",
        ip="172.16.60.3/24",
        mac="24:A4:3C:11:22:44",
        model="UF-OLT",
        site_id="site-bts-1",
    )
    moved_onu = _device(
        ONU_ID,
        "ONU-CUST-42",
        role="station",
        device_type="onu",
        mac="24:A4:3C:44:55:66",
        parent_id=second_olt_id,
        model="UF-LOCO",
    )
    result = sync(
        db_session,
        FakeUispClient(
            devices=[first_devices[0], second_olt, moved_onu],
            onus_by_olt={
                OLT_ID: [],
                second_olt_id: [
                    _olt_onu_listing_entry(ONU_ID, 5, parent_id=second_olt_id)
                ],
            },
        ),
    )

    destination = (
        db_session.query(OLTDevice)
        .filter(OLTDevice.uisp_device_id == second_olt_id)
        .one()
    )
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    original = (
        db_session.query(OLTDevice).filter(OLTDevice.uisp_device_id == OLT_ID).one()
    )
    port = db_session.get(PonPort, ont.pon_port_id)
    observed_port = (
        db_session.query(PonPort)
        .filter(PonPort.olt_id == destination.id, PonPort.port_number == 5)
        .one()
    )
    assert ont.olt_device_id == original.id
    assert port is not None
    assert port.olt_id == original.id
    assert port.port_number == 3
    assert observed_port.olt_id == destination.id
    assert result["onu_topology_review_required"] == 1
    evidence = (
        db_session.query(OntTopologyObservationEvidence)
        .filter_by(observed_olt_id=destination.id)
        .one()
    )
    assert evidence.latest_outcome == "review_required"


def test_onu_port_change_requires_review_without_moving_pon(db_session):
    # UISP supplies a location observation; it cannot rewrite canonical PON.
    sync(
        db_session,
        FakeUispClient(
            devices=_ufiber_payload(),
            onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
        ),
    )

    result = sync(
        db_session,
        FakeUispClient(
            devices=_ufiber_payload(),
            onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 5)]},
        ),
    )

    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    old_port = db_session.query(PonPort).filter(PonPort.port_number == 3).one()
    new_port = db_session.query(PonPort).filter(PonPort.port_number == 5).one()
    assert ont.pon_port_id == old_port.id
    assert ont.pon_port_id != new_port.id
    assert result["onu_topology_review_required"] == 1
    assert result["pon_ports_initialized"] == 1
    assert db_session.query(PonPort).count() == 2


def test_onu_port_move_emits_durable_review_evidence(db_session, caplog):
    with caplog.at_level(logging.INFO, logger="app.services.topology.uisp_sync"):
        first = sync(
            db_session,
            FakeUispClient(
                devices=_ufiber_payload(),
                onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
            ),
        )
    assert first["onu_topology_initialized"] == 1
    assert first["onu_topology_review_required"] == 0

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.topology.uisp_sync"):
        second = sync(
            db_session,
            FakeUispClient(
                devices=_ufiber_payload(),
                onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 5)]},
            ),
        )

    assert second["onu_topology_initialized"] == 0
    assert second["onu_topology_review_required"] == 1
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "uisp_onu_topology_review_required"
    ]
    assert record.uisp_device_id == ONU_ID
    evidence = db_session.get(OntTopologyObservationEvidence, record.evidence_id)
    assert evidence is not None
    assert evidence.latest_outcome == "review_required"
    assert evidence.observed_port_number == 5
    assert db_session.query(OntTopologyObservationEvidence).count() == 2


def test_existing_pon_port_is_matched_not_duplicated(db_session):
    # An operator-created port row for the same (olt, port_number) is reused
    # whatever its name; nothing about it is overwritten.
    existing_olt = OLTDevice(name="GPON-GARKI-1", vendor="ubiquiti")
    db_session.add(existing_olt)
    db_session.flush()
    existing_port = PonPort(
        olt_id=existing_olt.id,
        name="PON 3 (Garki feeder)",
        port_number=3,
        max_ont_capacity=64,
        is_active=True,
    )
    db_session.add(existing_port)
    db_session.flush()
    client = FakeUispClient(
        devices=_ufiber_payload(),
        onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 3)]},
    )

    result = sync(db_session, client)

    assert db_session.query(PonPort).count() == 1
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.pon_port_id == existing_port.id
    db_session.refresh(existing_port)
    assert existing_port.name == "PON 3 (Garki feeder)"
    assert existing_port.max_ont_capacity == 64
    assert result["pon_ports_initialized"] == 0
    assert result["onu_topology_initialized"] == 1


def test_huawei_ont_is_never_touched(db_session):
    # An ONT row parented under a non-UISP (Huawei) OLT keeps its pon_port_id
    # even when a UISP listing claims a port for the same uisp device id.
    huawei_olt = OLTDevice(name="HW-OLT-CENTRAL", vendor="huawei")
    db_session.add(huawei_olt)
    db_session.flush()
    huawei_port = PonPort(olt_id=huawei_olt.id, name="0/1/3", port_number=3)
    db_session.add(huawei_port)
    db_session.flush()
    huawei_ont = OntUnit(
        serial_number="48575443AABBCC01",
        vendor="huawei",
        olt_device_id=huawei_olt.id,
        pon_port_id=huawei_port.id,
        uisp_device_id=ONU_ID,
    )
    db_session.add(huawei_ont)
    db_session.flush()
    client = FakeUispClient(
        devices=_ufiber_payload(),
        onus_by_olt={OLT_ID: [_olt_onu_listing_entry(ONU_ID, 5)]},
    )

    result = sync(db_session, client)

    db_session.refresh(huawei_ont)
    assert huawei_ont.olt_device_id == huawei_olt.id
    assert huawei_ont.pon_port_id == huawei_port.id
    assert result["onu_topology_initialized"] == 0
    assert result["onu_topology_review_required"] == 0


def test_missing_onu_port_is_tolerated(db_session):
    entry = _olt_onu_listing_entry(ONU_ID, None)
    entry["onu"] = {"id": ONU_ID, "profile": "profile-small"}  # no port key
    client = FakeUispClient(
        devices=_ufiber_payload(),
        onus_by_olt={OLT_ID: [entry]},
    )

    result = sync(db_session, client)

    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.pon_port_id is None
    assert db_session.query(PonPort).count() == 0
    assert result["pon_ports_initialized"] == 0
    assert result["onu_topology_initialized"] == 1
    assert result["onu_topology_incomplete"] == 1
    assert result["port_fetch_failures"] == 0


def test_per_olt_port_fetch_failure_is_isolated(db_session):
    olt_b_id = "e2e2e2e2-1111-2222-3333-777777777778"
    onu_b_id = "f2f2f2f2-1111-2222-3333-888888888889"
    olt_b = _device(
        olt_b_id,
        "GPON-GUDU-2",
        role="gpon",
        device_type="olt",
        ip="172.16.60.3/24",
        mac="24:A4:3C:11:22:44",
        model="UF-OLT",
        site_id="site-bts-1",
    )
    onu_b = _device(
        onu_b_id,
        "ONU-CUST-43",
        role="station",
        device_type="onu",
        mac="24:A4:3C:44:55:88",
        parent_id=olt_b_id,
        model="UF-LOCO",
    )
    client = FakeUispClient(
        devices=[*_ufiber_payload(), olt_b, onu_b],
        onus_by_olt={
            olt_b_id: [
                _olt_onu_listing_entry(
                    onu_b_id, 7, parent_id=olt_b_id, mac="24:A4:3C:44:55:88"
                )
            ]
        },
        onu_list_errors={OLT_ID},
    )

    result = sync(db_session, client)

    assert result["port_fetch_failures"] == 1
    # The failing OLT's ONU keeps NULL; the healthy OLT's ONU still gets its port.
    ont_a = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont_a.pon_port_id is None
    ont_b = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == onu_b_id).one()
    port_b = db_session.query(PonPort).one()
    assert port_b.port_number == 7
    assert ont_b.pon_port_id == port_b.id
    assert result["pon_ports_initialized"] == 1
    assert result["onu_topology_initialized"] == 2
    assert result["onu_topology_incomplete"] == 1
    assert sorted(client.onu_list_calls) == sorted([OLT_ID, olt_b_id])


# ---------------------------------------------------------------------------
# Gap #4 — UFiber ONU live status + signal -> ont_units
# ---------------------------------------------------------------------------


def _onu_device(status="active", signal=None):
    onu = _device(
        ONU_ID,
        "ONU-CUST-42",
        role="station",
        device_type="onu",
        mac="24:A4:3C:44:55:66",
        ip=None,
        parent_id=OLT_ID,
        model="UF-LOCO",
        status=status,
    )
    if signal is not None:
        onu["overview"]["signal"] = signal
    return onu


def _ufiber_payload_with(onu):
    olt = _device(
        OLT_ID,
        "GPON-GARKI-1",
        role="gpon",
        device_type="olt",
        ip="172.16.60.2/24",
        mac="24:A4:3C:11:22:33",
        model="UF-OLT",
        site_id="site-bts-1",
    )
    return [olt, onu]


def test_onu_status_and_signal_mapped_from_payload(db_session):
    from app.models.network import OnuOnlineStatus

    client = FakeUispClient(
        devices=_ufiber_payload_with(_onu_device(status="active", signal=-21.5))
    )
    sync(db_session, client)
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_status_seen_at is not None
    assert ont.onu_rx_signal_dbm == -21.5


def test_onu_offline_status_mapped(db_session):
    from app.models.network import OnuOnlineStatus

    client = FakeUispClient(
        devices=_ufiber_payload_with(_onu_device(status="disconnected", signal=None))
    )
    sync(db_session, client)
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.olt_status == OnuOnlineStatus.offline
    assert ont.onu_rx_signal_dbm is None


def test_onu_without_status_leaves_telemetry_untouched(db_session):
    # No status string in the payload -> the UISP-owned fields are never set,
    # so a missing field can't blank a value we don't have.
    client = FakeUispClient(
        devices=_ufiber_payload_with(_onu_device(status="", signal=None))
    )
    sync(db_session, client)
    ont = db_session.query(OntUnit).filter(OntUnit.uisp_device_id == ONU_ID).one()
    assert ont.onu_rx_signal_dbm is None
    assert ont.olt_status_seen_at is None


# ---------------------------------------------------------------------------
# Gap #3 — UISP data-links -> NetworkTopologyLink (backhaul topology)
# ---------------------------------------------------------------------------


def _data_link(from_uisp_id, to_uisp_id, *, state="active", link_type="wireless"):
    return {
        "from": {"device": {"identification": {"id": from_uisp_id}}},
        "to": {"device": {"identification": {"id": to_uisp_id}}},
        "state": state,
        "type": link_type,
    }


def _stamped_node(db_session, name, uisp_id, mgmt_ip):
    node = NetworkDevice(
        name=name,
        hostname=name,
        mgmt_ip=mgmt_ip,
        role=DeviceRole.access,
        is_active=True,
        uisp_device_id=uisp_id,
    )
    db_session.add(node)
    db_session.flush()
    return node


def test_data_link_between_matched_nodes_creates_topology_link(db_session):
    from app.models.network_monitoring import NetworkTopologyLink, TopologyLinkMedium

    a = _stamped_node(db_session, "AP-A", "uisp-a", "172.16.40.10")
    b = _stamped_node(db_session, "AP-B", "uisp-b", "172.16.40.11")
    client = FakeUispClient(devices=[], data_links=[_data_link("uisp-a", "uisp-b")])

    result = sync(db_session, client)

    links = (
        db_session.query(NetworkTopologyLink)
        .filter(NetworkTopologyLink.source == "uisp_data_link")
        .all()
    )
    assert len(links) == 1
    assert {links[0].source_device_id, links[0].target_device_id} == {a.id, b.id}
    assert links[0].is_active is True
    assert links[0].medium == TopologyLinkMedium.wireless
    assert result["links_created"] == 1
    # Idempotent: a second run updates, never duplicates.
    again = sync(
        db_session,
        FakeUispClient(devices=[], data_links=[_data_link("uisp-a", "uisp-b")]),
    )
    assert again["links_created"] == 0
    assert again["links_updated"] == 1


def test_data_link_to_unmatched_endpoint_is_skipped(db_session):
    from app.models.network_monitoring import NetworkTopologyLink

    _stamped_node(db_session, "AP-A", "uisp-a", "172.16.40.10")
    client = FakeUispClient(
        devices=[], data_links=[_data_link("uisp-a", "uisp-unknown")]
    )

    result = sync(db_session, client)

    count = (
        db_session.query(NetworkTopologyLink)
        .filter(NetworkTopologyLink.source == "uisp_data_link")
        .count()
    )
    assert count == 0
    assert result["links_created"] == 0
    assert result["links_skipped"] == 1


def test_empty_data_link_response_does_not_mass_prune(db_session):
    from app.models.network_monitoring import NetworkTopologyLink

    _stamped_node(db_session, "AP-A", "uisp-a", "172.16.40.10")
    _stamped_node(db_session, "AP-B", "uisp-b", "172.16.40.11")
    sync(
        db_session,
        FakeUispClient(devices=[], data_links=[_data_link("uisp-a", "uisp-b")]),
    )

    result = sync(db_session, FakeUispClient(devices=[], data_links=[]))

    link = (
        db_session.query(NetworkTopologyLink)
        .filter(NetworkTopologyLink.source == "uisp_data_link")
        .one()
    )
    assert link.is_active is True
    assert result["links_pruned"] == 0
    assert result["link_prune_guarded"] == 1


# ---------------------------------------------------------------------------
# Secondary arm: bridge-mode radios linked by corroborated IP+name
# (never reads or writes subscription.mac_address — RADIUS-auth-safe)
# ---------------------------------------------------------------------------

# A stale radio mgmt IP that, in the production dry-run, collided with a
# DIFFERENT active customer by IP alone. The station's OWN MAC (below) never
# matches any subscription MAC — bridge-mode subs authenticate on the customer
# router's MAC, held in subscription.mac_address, a different device.
BRIDGE_IP = "172.16.131.185"
ROUTER_MAC = "AA:BB:CC:DD:EE:FF"  # customer router MAC on the subscription


def _bridge_payload(station_name, station_ip, station_mac=STATION_MAC):
    ap = _device(
        AP_ID,
        "AP-GARKI-SECTOR1",
        role="ap",
        ip="172.16.40.2/24",
        mac="24:A4:3C:00:00:01",
        site_id="site-bts-1",
    )
    station = _device(
        STATION_ID,
        station_name,
        role="station",
        mac=station_mac,
        ip=station_ip,
        ap_device_id=AP_ID,
    )
    return [ap, station]


def _active_sub_with_ip(db_session, subscriber, catalog_offer, ipv4, mac=ROUTER_MAC):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        ipv4_address=ipv4,
        mac_address=mac,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def test_ip_name_corroborated_links_bridge_radio(db_session, subscriber, catalog_offer):
    # Unique ACTIVE ipv4 == station mgmt IP AND the names agree: the row is
    # created for that subscriber, with the AP edge and provenance note — and
    # the subscription's (router) MAC is left completely untouched.
    node = _ap_node(db_session)
    subscriber.display_name = "Pro Alpha Ltd"
    db_session.flush()
    subscription = _active_sub_with_ip(db_session, subscriber, catalog_offer, BRIDGE_IP)
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.subscriber_id == subscriber.id
    assert cpe.mac_address == STATION_MAC  # the RADIO's own MAC, not the router
    assert cpe.device_type == DeviceType.wireless_radio
    assert cpe.parent_network_device_id == node.id
    assert f"linked via UISP ip+name {BRIDGE_IP} sim=" in (cpe.notes or "")
    assert result["matched_by_ip_name"] == 1
    assert result["matched"] == 0
    assert result["created"] == 1
    assert result["edges_set"] == 1
    # RADIUS-auth safety: subscription.mac_address is never written.
    db_session.refresh(subscription)
    assert subscription.mac_address == ROUTER_MAC


def test_ip_matches_but_name_below_threshold_creates_no_row(
    db_session, subscriber, catalog_offer
):
    # The stale-IP false match from the dry-run: the radio's recorded IP now
    # collides with a DIFFERENT customer. IP agrees but the names don't, so the
    # name gate rejects it — no row, nothing linked, subscription MAC untouched.
    _ap_node(db_session)
    subscriber.display_name = "Hajia Hassana Mohammed"
    db_session.flush()
    subscription = _active_sub_with_ip(db_session, subscriber, catalog_offer, BRIDGE_IP)
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["matched_by_ip_name"] == 0
    assert result["unmatched_no_subscriber"] == 1
    db_session.refresh(subscription)
    assert subscription.mac_address == ROUTER_MAC


def test_ip_shared_by_two_active_subs_creates_no_row(
    db_session, subscriber, catalog_offer
):
    # A reassigned PPPoE IP now sits on two ACTIVE subscribers: the IP is
    # ambiguous and dropped from the index, so even a perfect name match can't
    # link it (zero or >1 IP matches -> skip).
    _ap_node(db_session)
    subscriber.display_name = "Pro Alpha Ltd"
    db_session.flush()
    other = Subscriber(
        first_name="Pro",
        last_name="Alpha",
        email=f"other-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(other)
    db_session.flush()
    _active_sub_with_ip(
        db_session, subscriber, catalog_offer, BRIDGE_IP, mac="AA:BB:CC:DD:EE:01"
    )
    _active_sub_with_ip(
        db_session, other, catalog_offer, BRIDGE_IP, mac="AA:BB:CC:DD:EE:02"
    )
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    result = sync(db_session, client)

    assert db_session.query(CPEDevice).count() == 0
    assert result["matched_by_ip_name"] == 0
    assert result["unmatched_no_subscriber"] == 1


def test_mac_match_takes_precedence_over_ip_name(db_session, subscriber, catalog_offer):
    # When the station's OWN MAC already matches an ACTIVE subscription, the MAC
    # arm handles it: the ip+name arm is never reached, so there is no double
    # count and no ip+name provenance note.
    _ap_node(db_session)
    subscriber.display_name = "Pro Alpha Ltd"
    db_session.flush()
    _active_sub_with_ip(
        db_session, subscriber, catalog_offer, BRIDGE_IP, mac=STATION_MAC
    )
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    result = sync(db_session, client)

    cpe = (
        db_session.query(CPEDevice).filter(CPEDevice.uisp_device_id == STATION_ID).one()
    )
    assert cpe.subscriber_id == subscriber.id
    assert cpe.notes is None  # created via the MAC arm, not ip+name
    assert result["matched"] == 1
    assert result["matched_by_ip_name"] == 0
    assert result["created"] == 1


def test_existing_row_is_not_relinked_by_ip_name(db_session, subscriber, catalog_offer):
    # A row already keyed by uisp_device_id updates normally; the ip+name arm
    # (reached only when no row exists) never fires, the operator-set owner
    # stands, and the subscription MAC is untouched.
    _ap_node(db_session)
    owner = Subscriber(
        first_name="Original",
        last_name="Owner",
        email=f"owner-{uuid.uuid4().hex[:8]}@example.test",
    )
    db_session.add(owner)
    db_session.flush()
    existing = CPEDevice(
        uisp_device_id=STATION_ID,
        subscriber_id=owner.id,
        mac_address=STATION_MAC,
    )
    db_session.add(existing)
    db_session.flush()
    subscriber.display_name = "Pro Alpha Ltd"
    db_session.flush()
    subscription = _active_sub_with_ip(db_session, subscriber, catalog_offer, BRIDGE_IP)
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    result = sync(db_session, client)

    db_session.refresh(existing)
    assert existing.subscriber_id == owner.id
    assert result["matched_by_ip_name"] == 0
    assert db_session.query(CPEDevice).count() == 1
    db_session.refresh(subscription)
    assert subscription.mac_address == ROUTER_MAC


def test_ip_name_link_is_idempotent(db_session, subscriber, catalog_offer):
    # Re-running makes no new rows and never re-links; the subscription MAC
    # stays untouched across runs.
    _ap_node(db_session)
    subscriber.display_name = "Pro Alpha Ltd"
    db_session.flush()
    subscription = _active_sub_with_ip(db_session, subscriber, catalog_offer, BRIDGE_IP)
    client = FakeUispClient(devices=_bridge_payload("Pro-Alpha Ltd", BRIDGE_IP))

    first = sync(db_session, client)
    second = sync(db_session, client)

    assert first["matched_by_ip_name"] == 1
    assert second["matched_by_ip_name"] == 0
    assert second["unchanged"] == 1
    assert db_session.query(CPEDevice).count() == 1
    db_session.refresh(subscription)
    assert subscription.mac_address == ROUTER_MAC


# ---------------------------------------------------------------------------
# Transaction span: no transaction may be held across the UISP fan-outs
# ---------------------------------------------------------------------------


class _OrderRecordingClient(FakeUispClient):
    """Records UISP calls into a shared log alongside checkpoint markers."""

    def __init__(self, log: list[str], **kwargs):
        super().__init__(**kwargs)
        self._log = log

    def list_airmax_stations(self, ap_id):
        self._log.append("http:stations")
        return super().list_airmax_stations(ap_id)

    def list_olt_onus(self, olt_id):
        self._log.append("http:onus")
        return super().list_olt_onus(olt_id)

    def list_data_links(self):
        self._log.append("http:links")
        return super().list_data_links()


def test_each_uisp_fanout_is_preceded_by_a_checkpoint(
    db_session, subscriber, catalog_offer
):
    """The pass must commit before every block of UISP HTTP.

    Holding the transaction across the per-AP and per-OLT fan-outs is what
    exceeded ``idle_in_transaction_session_timeout`` in production and lost
    the whole pass after UISP had already answered.
    """
    _ap_node(db_session)
    _active_subscription(db_session, subscriber, catalog_offer, STATION_MAC)
    log: list[str] = []
    client = _OrderRecordingClient(
        log,
        devices=_wireless_payload(),
        stations_by_ap={AP_ID: [_station_entry()]},
    )

    sync(db_session, client, checkpoint=lambda: log.append("checkpoint"))

    assert "http:stations" in log, "the AP-side fan-out should have run"
    for index, event in enumerate(log):
        if event.startswith("http:"):
            assert "checkpoint" in log[:index], (
                f"{event} ran with no preceding checkpoint, so a transaction "
                f"was still open across it: {log}"
            )
