"""UISP topology sync: import the customer-device relationship layer.

Pulls the UISP inventory (devices + sites, read-only) and reconciles the
wireless/UFiber customer layer into sub's own tables:

  - wireless customer radios (airMax stations, airCube/blackBox) -> cpe_devices,
    keyed by ``uisp_device_id``, with the CPE -> AP edge stored as
    ``parent_network_device_id`` (FK to the historical reconcile-owned
    network_devices row) so customer paths can walk radio -> AP -> basestation.
    ``cpe_devices.subscriber_id`` is NOT NULL, so creation is MATCH-THEN-CREATE:
    a new radio only gets a row once its MAC resolves to exactly one subscriber
    over ACTIVE subscriptions (unmatched/ambiguous radios are counted, enqueued
    into the unmatched-radio review queue, and retried naturally on later
    runs). The AP-side station listing is fetched every run: it is both the
    association fallback and the only source of the radio's RF signal, stored
    as a current-value observation (rf_signal_dbm/source/observed_at) next to
    the edge it describes. Rows pre-registered at install time
    (``uisp_device_id`` NULL, subscriber known — see
    ``app/services/radio_registration.py``) are ADOPTED by normalized MAC
    before any matching/creation, so the install flow and this sync converge
    on one row per radio;
  - UF-OLTs -> olt_devices and UFiber ONUs -> ont_units (parent resolved via
    the ONU's ``attributes.parentId``), both keyed by ``uisp_device_id``;
  - PON-port granularity for the UFiber plant: the generic ``/devices`` list
    carries no port info, so each imported UF-OLT's ONUs are re-listed via
    ``/devices/onus?parentId=<olt>`` whose ``onu.port`` is the OLT-side PON
    port number. The collector submits that exact observation to
    ``network.ont_topology_observations``; it does not rewrite topology.

Monitoring/state stays with the native polling layer; this sync only owns
*relationships* and identity fill-in.

Idioms follow the retired zabbix_reconcile/lldp_poller: idempotent upsert by stable
external id, match-don't-create against pre-existing rows, never overwrite a
non-NULL human-set field, per-item failure isolation, soft-prune limited to
the columns this sync owns.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import Enum
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    DeviceType,
    OLTDevice,
    OntUnit,
    OnuOnlineStatus,
)
from app.models.network_monitoring import (
    NetworkDevice,
    NetworkTopologyLink,
    TopologyLinkMedium,
)
from app.models.subscriber import Subscriber
from app.services import unmatched_radio_queue
from app.services.network.ont_status import apply_olt_status_observation
from app.services.network.ont_topology_observations import (
    observe_ont_electronic_topology,
)
from app.services.network.radio_signal import RadioSignalSource
from app.services.topology.lldp_poller import _canonical, build_device_index
from app.services.topology.lldp_poller import _norm as _norm_label

logger = logging.getLogger(__name__)

# Marks provenance on rows/edges this sync owns.
SOURCE = "uisp_sync"
# The UISP "Archive" site: decommissioned gear parked there is never imported.
ARCHIVE_SITE_ID = "d857d634-db38-45ff-81a1-4594410ded45"
# Arbitrary constant key for the Postgres advisory lock (single-flight guard,
# acquired via db_session_adapter.advisory_lock in the Celery task wrapper).
ADVISORY_LOCK_KEY = 0x75_69_53  # "uiS"

# Device types that are customer radios even when the role field is absent.
_STATION_TYPES = {"airCube", "blackBox"}
# Factory-default addresses several stations report; never unique, never keyed.
_SHARED_DEFAULT_IPS = {"192.168.1.1", "192.168.1.20"}

_MAC_JUNK = re.compile(r"[^0-9a-f]")
_NAME_JUNK = re.compile(r"[^a-z0-9]+")

# Minimum name corroboration for the secondary IP+name link arm. A unique IP
# hit alone is NOT trusted (RouterOS PPPoE IPs get reassigned, so a radio's
# stale recorded mgmt IP can collide with a *different* live customer); the
# UISP station name must also resemble the matched subscriber's name.
_IP_NAME_SIM_THRESHOLD = 0.60


class StationMatchDisposition(str, Enum):
    """How ``_upsert_station`` resolved one UISP station this run.

    Replaces the old bare ``(cpe, created)`` tuple so the caller can act on
    WHY a station produced no row (``ambiguous`` vs ``unmatched`` feed the
    unmatched-radio review queue) without re-deriving it from counters.
    """

    existing = "existing"  # row already keyed by uisp_device_id
    adopted = "adopted"  # install-time placeholder adopted by MAC
    created = "created"  # created via the MAC or IP+name arm
    ambiguous = "ambiguous"  # >1 candidate (adoption or MAC): nothing written
    unmatched = "unmatched"  # no subscriber resolvable: nothing written


def _now() -> datetime:
    return datetime.now(UTC)


def _norm_mac(mac: str | None) -> str | None:
    """Normalize a MAC to bare lowercase hex; None when unusable."""
    if not mac:
        return None
    normalized = _MAC_JUNK.sub("", str(mac).lower())
    return normalized if len(normalized) == 12 else None


def _norm_name(value: str | None) -> str:
    """Lowercase, collapse every non-alphanumeric run to a single space, trim."""
    if not value:
        return ""
    return _NAME_JUNK.sub(" ", str(value).lower()).strip()


def _name_similarity(name: str | None, candidates: list[str]) -> float:
    """Best similarity of ``name`` to any candidate label (0.0–1.0).

    Both signals are computed on the normalized (lowercase, alphanumeric-only)
    forms and the larger wins, so either a token overlap (word-order-robust,
    e.g. "Doe John" vs "John Doe") or a close character sequence (typos, glued
    words) can corroborate:

      - token Jaccard over the whitespace tokens, and
      - ``SequenceMatcher`` ratio over the space-stripped compact strings.

    Empty on either side scores 0.0 (an absent name never corroborates).
    """
    left = _norm_name(name)
    if not left:
        return 0.0
    left_tokens = set(left.split())
    left_compact = left.replace(" ", "")
    best = 0.0
    for candidate in candidates:
        right = _norm_name(candidate)
        if not right:
            continue
        right_tokens = set(right.split())
        if left_tokens and right_tokens:
            jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        else:
            jaccard = 0.0
        ratio = SequenceMatcher(None, left_compact, right.replace(" ", "")).ratio()
        best = max(best, jaccard, ratio)
    return best


def _ident(device: dict) -> dict:
    ident = device.get("identification")
    return ident if isinstance(ident, dict) else {}


def _attributes(device: dict) -> dict:
    attrs = device.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _device_id(device: dict) -> str:
    return str(_ident(device).get("id") or "").strip()


def _device_name(device: dict) -> str:
    return str(_ident(device).get("name") or "").strip()


def _device_role(device: dict) -> str:
    return str(_ident(device).get("role") or "").strip().lower()


def _device_type(device: dict) -> str:
    return str(_ident(device).get("type") or "").strip()


def _device_status(device: dict) -> str | None:
    overview = device.get("overview")
    if not isinstance(overview, dict):
        return None
    status = str(overview.get("status") or "").strip()
    return status[:20] or None


_DATA_LINK_SOURCE = "uisp_data_link"


def _onu_status(device: dict) -> OnuOnlineStatus | None:
    """Map a UISP device status string to OnuOnlineStatus; None when absent."""
    raw = (_device_status(device) or "").lower()
    if not raw:
        return None
    # Offline first: "disconnected" contains "connected", so an online check
    # must not win on a down radio.
    if any(token in raw for token in ("offline", "disconnect", "down", "inactive")):
        return OnuOnlineStatus.offline
    if any(token in raw for token in ("online", "active", "connected", "running")):
        return OnuOnlineStatus.online
    return None


def _onu_signal(device: dict) -> float | None:
    """Best-effort ONU receive signal (dBm) from the UISP overview; None if absent."""
    overview = device.get("overview")
    if not isinstance(overview, dict):
        return None
    for key in ("signal", "receivePower", "rxPower", "rxSignal"):
        val = overview.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _station_rx_signal(entry: dict) -> float | None:
    """RSSI (dBm) from one AP-side station-list entry; None when unusable.

    Only plausible dBm values are accepted: airMax reports negative dBm, so a
    non-negative number is some other unit (percent-style quality) and must not
    be stored as a signal. Bounded at -100 — anything below is noise floor or a
    sentinel.
    """
    for key in ("rxSignal", "signal", "rssi"):
        val = entry.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if -100.0 <= float(val) < 0.0:
                return float(val)
            # Out-of-range under this key (e.g. a percent-style quality
            # number); another key may still carry the real dBm value.
    return None


def _link_endpoint_uisp_id(side: object) -> str | None:
    """UISP device id of one end of a data-link, tolerating shape variance."""
    if not isinstance(side, dict):
        return None
    device = side.get("device")
    if not isinstance(device, dict):
        return None
    ident = device.get("identification")
    if not isinstance(ident, dict):
        return None
    return str(ident.get("id") or "").strip() or None


def _link_is_disabled(link: dict) -> bool:
    """True only when UISP explicitly marks the link disabled/inactive."""
    if link.get("enabled") is False:
        return True
    state = str(link.get("state") or link.get("status") or "").strip().lower()
    return state in {"inactive", "disabled", "deleted"}


def _link_medium(link: dict) -> TopologyLinkMedium:
    kind = str(link.get("type") or link.get("medium") or "").strip().lower()
    if "fiber" in kind or "fibre" in kind or "pon" in kind:
        return TopologyLinkMedium.fiber
    if "ethernet" in kind:
        return TopologyLinkMedium.ethernet
    if any(token in kind for token in ("wireless", "air", "ptp", "ptmp", "rf")):
        return TopologyLinkMedium.wireless
    return TopologyLinkMedium.unknown


def _device_site_id(device: dict) -> str | None:
    site = _ident(device).get("site")
    if not isinstance(site, dict):
        return None
    sid = str(site.get("id") or "").strip()
    return sid or None


def _mgmt_ip(device: dict) -> str | None:
    """The device's mgmt IP without its prefix length; None when absent."""
    raw = str(device.get("ipAddress") or "").strip()
    if not raw:
        return None
    return raw.split("/", 1)[0]


def _serial(device: dict) -> str | None:
    ident = _ident(device)
    for key in ("serialNumber", "serial"):
        value = str(ident.get(key) or "").strip()
        if value:
            return value
    return None


def _firmware_version(device: dict) -> str | None:
    ident = _ident(device)
    for key in ("firmwareVersion", "firmware", "version"):
        value = str(ident.get(key) or "").strip()
        if value:
            return value[:120]
    return None


def _onu_pon_port(device: dict) -> int | None:
    """OLT-side PON port number from a per-OLT ONU listing entry; None if absent.

    Only the ``/devices/onus?parentId=...`` payload carries the top-level
    ``onu`` object; its ``port`` is the OLT-side PON port (integer, 1-based).
    """
    onu = device.get("onu")
    if not isinstance(onu, dict):
        return None
    port = onu.get("port")
    if port is None or isinstance(port, bool):
        return None
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def _is_station(device: dict) -> bool:
    return _device_role(device) == "station" or _device_type(device) in _STATION_TYPES


def _is_ap(device: dict) -> bool:
    return _device_role(device) == "ap"


def _is_olt(device: dict) -> bool:
    return _device_type(device) == "olt"


def _is_onu(device: dict) -> bool:
    return _device_type(device) == "onu"


def excluded_site_ids(sites: list[dict]) -> set[str]:
    """The archive site plus every site parented under it (endpoints)."""
    excluded = {ARCHIVE_SITE_ID}
    for site in sites:
        sid = str(site.get("id") or "").strip()
        ident = site.get("identification")
        parent = (ident or {}).get("parent") if isinstance(ident, dict) else None
        parent_id = (
            str(parent.get("id") or "").strip() if isinstance(parent, dict) else ""
        )
        if sid and parent_id == ARCHIVE_SITE_ID:
            excluded.add(sid)
    return excluded


def _unique_ips(devices: list[dict]) -> set[str]:
    """IPs that appear on exactly one UISP device (and are not shared defaults)."""
    counts = Counter(ip for d in devices if (ip := _mgmt_ip(d)))
    return {ip for ip, n in counts.items() if n == 1 and ip not in _SHARED_DEFAULT_IPS}


def _active_subscriber_macs(session: Session) -> dict[str, set[tuple[UUID, UUID]]]:
    """Normalized MAC -> exact active (subscriber, subscription) services.

    The subscriptions table carries historical duplicate rows; scoping to
    status='active' prevents historical matches. Any MAC mapped to more than one
    service remains ambiguous, including two services on the same customer.
    """
    index: dict[str, set[tuple[UUID, UUID]]] = {}
    rows = (
        session.query(
            Subscription.mac_address,
            Subscription.subscriber_id,
            Subscription.id,
        )
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.mac_address.isnot(None),
        )
        .all()
    )
    for mac, subscriber_id, subscription_id in rows:
        normalized = _norm_mac(mac)
        if normalized:
            index.setdefault(normalized, set()).add((subscriber_id, subscription_id))
    return index


def _active_subscriber_ips(
    session: Session,
) -> dict[str, tuple[UUID, UUID, list[str]]]:
    """Unique IPv4 -> (subscriber, subscription, labels) over active services.

    The secondary IP+name arm keys on the DESIRED served IPv4
    (``Subscription.ipv4_address``), scoped to ACTIVE subscriptions. Because a
    stale radio mgmt IP can collide with a *different* live customer, an IP is
    only usable when it maps to EXACTLY ONE active subscription: shared IPs
    are dropped here (never surfaced to the caller),
    as are the factory-default shared addresses. Each retained entry also
    carries the subscriber's name labels (display name, first+last, company) so
    the caller can score name corroboration against a single candidate without
    a second query.
    """
    seen: dict[str, set[tuple[UUID, UUID]]] = {}
    labels: dict[str, list[str]] = {}
    rows = (
        session.query(
            Subscription.ipv4_address,
            Subscription.subscriber_id,
            Subscription.id,
            Subscriber.display_name,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.company_name,
        )
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.ipv4_address.isnot(None),
        )
        .all()
    )
    for ipv4, subscriber_id, subscription_id, display, first, last, company in rows:
        ip = str(ipv4 or "").split("/", 1)[0].strip()
        if not ip or ip in _SHARED_DEFAULT_IPS:
            continue
        seen.setdefault(ip, set()).add((subscriber_id, subscription_id))
        labels[ip] = [
            label
            for label in (display, f"{first or ''} {last or ''}", company)
            if _norm_name(label)
        ]
    return {
        ip: (*next(iter(services)), labels[ip])
        for ip, services in seen.items()
        if len(services) == 1
    }


def _fill(obj, attr: str, value) -> bool:
    """Fill a NULL field only; never overwrite human-set data. True if set."""
    if value is None or value == "":
        return False
    if getattr(obj, attr) is None:
        setattr(obj, attr, value)
        return True
    return False


# ---------------------------------------------------------------------------
# AP matching (UISP AP -> network_devices node)
# ---------------------------------------------------------------------------


def match_ap_nodes(
    session: Session,
    aps: list[dict],
    unique_ips: set[str],
    stats: Counter,
) -> dict[str, NetworkDevice]:
    """Match UISP APs to network_devices rows (mgmt IP first, then name).

    Stamps ``uisp_device_id`` on matched rows and returns uisp AP id -> node.
    These rows were created by the retired Zabbix reconcile; this sync never
    creates network_devices, it only links them.
    """
    by_name, by_ip = build_device_index(session)
    ap_nodes: dict[str, NetworkDevice] = {}
    for ap in aps:
        uisp_id = _device_id(ap)
        if not uisp_id:
            continue
        node = None
        ip = _mgmt_ip(ap)
        if ip and ip in unique_ips:
            node = by_ip.get(ip)
        if node is None:
            node = by_name.get(_norm_label(_device_name(ap)))
        if node is None:
            stats["aps_unmatched"] += 1
            continue
        if uisp_id in ap_nodes:
            continue
        if node.uisp_device_id != uisp_id:
            # The AP moved to a different node (re-IP/rename): release the id
            # from any other row first so the partial-unique index holds.
            session.query(NetworkDevice).filter(
                NetworkDevice.uisp_device_id == uisp_id,
                NetworkDevice.id != node.id,
            ).update({NetworkDevice.uisp_device_id: None}, synchronize_session=False)
            node.uisp_device_id = uisp_id
        ap_nodes[uisp_id] = node
        stats["aps_matched"] += 1
    return ap_nodes


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _blank_stats() -> Counter:
    return Counter(
        {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "edges_set": 0,
            "edges_moved": 0,
            "matched": 0,
            "matched_by_ip_name": 0,
            "adopted": 0,
            "ambiguous": 0,
            "unmatched_no_subscriber": 0,
            "skipped": 0,
            "failed": 0,
            "pruned": 0,
            "excluded_archive": 0,
            "aps_matched": 0,
            "aps_unmatched": 0,
            "pon_ports_initialized": 0,
            "onu_topology_initialized": 0,
            "onu_topology_confirmed": 0,
            "onu_topology_incomplete": 0,
            "onu_topology_review_required": 0,
            "port_fetch_failures": 0,
            "links_created": 0,
            "links_updated": 0,
            "links_pruned": 0,
            "prune_guarded": 0,
            "link_prune_guarded": 0,
            "links_skipped": 0,
            "link_fetch_failures": 0,
            "signals_recorded": 0,
            "signals_cleared": 0,
            "queue_opened": 0,
            "queue_bumped": 0,
            "queue_failed": 0,
        }
    )


def _note_edge_move(
    session: Session,
    cpe: CPEDevice,
    station: dict,
    new_node: NetworkDevice,
    stats: Counter,
) -> None:
    """Breadcrumb for a silent auto-heal: radio re-parented from one AP to another.

    The sync corrects ``parent_network_device_id`` whenever UISP observes the
    radio on a different AP — correct behavior (UISP is observed truth), but
    otherwise completely silent. These ``logger.info`` lines flow to Loki and
    are the only post-mortem history of when a customer's upstream path moved
    (e.g. "why did this street's outage blast-radius change overnight?").
    First-time parenting (old parent NULL) is a fill, not a move: no log, no
    ``edges_moved`` count — ``edges_set`` alone covers it.
    """
    old_id = cpe.parent_network_device_id
    if old_id is None or old_id == new_node.id:
        return
    old_node = session.get(NetworkDevice, old_id)
    old_name = old_node.name if old_node is not None else None
    logger.info(
        "uisp_sync_edge_moved device=%s uisp_id=%s old_parent=%s old_parent_id=%s "
        "new_parent=%s new_parent_id=%s",
        _device_name(station),
        cpe.uisp_device_id,
        old_name,
        old_id,
        new_node.name,
        new_node.id,
        extra={
            "event": "uisp_sync_edge_moved",
            "device_name": _device_name(station),
            "uisp_device_id": cpe.uisp_device_id,
            "old_parent_id": str(old_id),
            "old_parent_name": old_name,
            "new_parent_id": str(new_node.id),
            "new_parent_name": new_node.name,
        },
    )
    stats["edges_moved"] += 1


def _clear_rf_signal(cpe: CPEDevice, stats: Counter) -> None:
    """Null the RF observation columns (counted once per actual clear)."""
    if cpe.rf_signal_dbm is None and cpe.rf_signal_observed_at is None:
        return
    cpe.rf_signal_dbm = None
    cpe.rf_signal_source = None
    cpe.rf_signal_observed_at = None
    stats["signals_cleared"] += 1


def _apply_station_signal(
    cpe: CPEDevice,
    station: dict,
    signal: float | None,
    now: datetime,
    stats: Counter,
) -> None:
    """Write/clear the current-value RF observation for one station.

    Associated (active) with an AP-side RSSI -> record it, stamped with the
    sync run time (consistent with ``uisp_synced_at``). Not associated
    (disconnected/unauthorized) -> clear: a retained value would describe a
    link that no longer exists. Active but no AP-side entry (AP call failed,
    AP inactive or unmatched) -> retain the previous value and let the
    read-side TTL degrade it honestly — absence of evidence never clears.
    """
    status = (_device_status(station) or "").strip().lower()
    if status == "active":
        if signal is not None:
            cpe.rf_signal_dbm = signal
            cpe.rf_signal_source = RadioSignalSource.uisp_ap_station.value
            cpe.rf_signal_observed_at = now
            stats["signals_recorded"] += 1
    elif status in ("disconnected", "unauthorized"):
        _clear_rf_signal(cpe, stats)


@dataclass(frozen=True)
class _StationReview:
    """Typed payload for one deferred unmatched-radio queue write."""

    reason: str
    title: str
    description: str
    details: dict[str, str | None]


def _enqueue_station_review(
    session: Session,
    *,
    mac: str,
    review: _StationReview,
    stats: Counter,
) -> None:
    """Open (or bump) the unmatched-radio review item for one station.

    Runs in its own SAVEPOINT after the station's upsert savepoint closed: a
    queue failure must not fail the station, and vice versa. The queue dedupes
    per MAC, so chronically unmatched stations hold exactly one open item.
    """
    try:
        with session.begin_nested():
            _, created = unmatched_radio_queue.open_item(
                session,
                mac_compact=mac,
                reason=review.reason,
                title=review.title,
                description=review.description,
                details=review.details,
            )
        stats["queue_opened" if created else "queue_bumped"] += 1
    except Exception:
        stats["queue_failed"] += 1
        logger.exception(
            "uisp_sync_queue_item_failed mac=%s reason=%s", mac, review.reason
        )


def _station_parent_ap_id(station: dict) -> str | None:
    ap_device = _attributes(station).get("apDevice")
    if isinstance(ap_device, dict):
        ap_id = str(ap_device.get("id") or "").strip()
        if ap_id:
            return ap_id
    return None


@dataclass(frozen=True)
class ApStationIndex:
    """AP-side association + RF observation, keyed by station id and MAC.

    ``ap_by_*`` maps a station to its serving AP's UISP id (association
    fallback for stations whose own payload lacks ``attributes.apDevice``).
    ``signal_by_*`` carries the AP-observed station RSSI in dBm — the only
    place UISP reports the customer radio's RF value.
    """

    ap_by_station_id: dict[str, str] = field(default_factory=dict)
    ap_by_station_mac: dict[str, str] = field(default_factory=dict)
    signal_by_station_id: dict[str, float] = field(default_factory=dict)
    signal_by_station_mac: dict[str, float] = field(default_factory=dict)

    def ap_for(self, station_uisp_id: str, mac: str | None) -> str | None:
        return self.ap_by_station_id.get(station_uisp_id) or (
            self.ap_by_station_mac.get(mac) if mac else None
        )

    def signal_for(self, station_uisp_id: str, mac: str | None) -> float | None:
        signal = self.signal_by_station_id.get(station_uisp_id)
        if signal is None and mac:
            signal = self.signal_by_station_mac.get(mac)
        return signal


def _ap_side_station_index(
    client, aps: list[dict], matched_ap_ids: Container[str]
) -> ApStationIndex:
    """AP-side association + RSSI per station, from active node-matched APs.

    Queried unconditionally each run: the station list is both the association
    fallback (stations whose payload lacks ``attributes.apDevice``) and the
    only source of the radio's RF signal, ingested together so a stored signal
    always describes the stored edge. One AP failing (or an AP being inactive
    or unmatched) only leaves its stations absent from the index — the caller
    then retains prior signal values and lets the read-side TTL age them out,
    never clearing on missing evidence.

    Takes the matched AP ids rather than the matched nodes: this is the run's
    HTTP fan-out and must be callable with no transaction open, so it must not
    hold ORM entities whose attribute access would reopen one.
    """
    index = ApStationIndex()
    for ap in aps:
        ap_id = _device_id(ap)
        if not ap_id or ap_id not in matched_ap_ids or _device_status(ap) != "active":
            continue
        try:
            entries = client.list_airmax_stations(ap_id)
        except Exception as exc:  # one AP failing must not abort the run
            logger.warning("uisp_ap_station_list_failed ap=%s: %s", ap_id, exc)
            continue
        for entry in entries:
            ident = entry.get("deviceIdentification")
            station_id = (
                str(ident.get("id") or "").strip() if isinstance(ident, dict) else ""
            )
            mac = _norm_mac(entry.get("mac"))
            signal = _station_rx_signal(entry)
            if station_id:
                index.ap_by_station_id.setdefault(station_id, ap_id)
                if signal is not None:
                    index.signal_by_station_id.setdefault(station_id, signal)
            if mac:
                index.ap_by_station_mac.setdefault(mac, ap_id)
                if signal is not None:
                    index.signal_by_station_mac.setdefault(mac, signal)
    return index


def _adoption_candidates(session: Session, mac: str | None) -> list[CPEDevice]:
    """Pre-registered install-time cpe rows adoptable by a station's MAC.

    The install-time capture flow (``radio_registration.register_radio_mac``)
    creates CPE rows with the subscriber known and ``uisp_device_id``
    NULL, expecting this sync to ADOPT the row once UISP first reports the
    radio. Safety scope: wireless radios only, never a row already bound to a
    (different) UISP device, never a retired tombstone (the hourly
    unmatched-radio review retires superseded placeholders). MAC comparison
    is on the normalized bare-hex form — stored values may be canonical
    colon-separated or free-form legacy.
    """
    if not mac:
        return []
    rows = (
        session.query(CPEDevice)
        .filter(
            CPEDevice.uisp_device_id.is_(None),
            CPEDevice.device_type == DeviceType.wireless_radio,
            CPEDevice.status != DeviceStatus.retired,
            CPEDevice.mac_address.isnot(None),
        )
        .all()
    )
    return [row for row in rows if _norm_mac(row.mac_address) == mac]


def _match_by_ip_name(
    station: dict, ip_index: dict[str, tuple[UUID, UUID, list[str]]]
) -> tuple[UUID, UUID, str, float] | None:
    """Corroborated bridge match: subscriber, subscription, IP, score.

    The station's UISP mgmt IP must UNIQUELY equal one ACTIVE subscription's
    ``ipv4_address`` (``ip_index`` already holds only unique, non-default IPs),
    AND the UISP station name must resemble that subscriber's name at
    ``>= _IP_NAME_SIM_THRESHOLD``. Name similarity is scored for at most the one
    candidate the unique IP resolves to — never an all-pairs scan. Returns None
    (skip) unless both signals hold.
    """
    ip = _mgmt_ip(station)
    if not ip or ip in _SHARED_DEFAULT_IPS:
        return None
    entry = ip_index.get(ip)
    if entry is None:
        return None
    subscriber_id, subscription_id, labels = entry
    score = _name_similarity(_device_name(station), labels)
    if score < _IP_NAME_SIM_THRESHOLD:
        return None
    return subscriber_id, subscription_id, ip, score


def _upsert_station(
    session: Session,
    station: dict,
    mac_index: dict[str, set[tuple[UUID, UUID]]],
    ip_index: dict[str, tuple[UUID, UUID, list[str]]],
    now: datetime,
    stats: Counter,
) -> tuple[CPEDevice | None, StationMatchDisposition]:
    """Upsert one wireless customer radio into cpe_devices by uisp_device_id.

    ADOPT-THEN-MATCH-THEN-CREATE: ``cpe_devices.subscriber_id`` is NOT NULL
    in the production schema, so a new row may only be created once its owner
    is known.

    1. ADOPT (install-time ownership contract): the field flow pre-registers a
       radio at turn-up as a cpe row with ``subscriber_id`` set and
       ``uisp_device_id`` NULL. When the uisp-id lookup misses, a single such
       row whose normalized MAC equals the station's is adopted in place —
       ``uisp_device_id`` stamped, ``subscriber_id`` never touched, counted
       as ``adopted`` — instead of creating a duplicate. Multiple candidates
       for one MAC are genuinely ambiguous: nothing is adopted or created
       (``ambiguous``), because inventing a third row would compound the
       duplication.
    2. MATCH-THEN-CREATE by MAC (unchanged): a new station's own MAC is
       resolved against ACTIVE subscriptions (exactly one service). An
       ambiguous MAC creates nothing, even within one multi-service customer.
    3. SECONDARY: corroborated IP+name (bridge-mode radios). Reached ONLY when
       the MAC arm found no subscriber — bridge-mode stations authenticate with
       the *customer router's* MAC (held in ``subscription.mac_address``), so
       the radio's own MAC never matches a subscription and arm 2 always misses
       for them. A row is created for the subscriber whose ACTIVE
       ``ipv4_address`` UNIQUELY equals the station's UISP mgmt IP AND whose
       name resembles the station name (``>= _IP_NAME_SIM_THRESHOLD``). IP alone
       is not trusted (reassigned PPPoE IPs collide with other live customers),
       so both signals must agree. ``subscription.mac_address`` is NEVER read or
       written by this arm — overwriting it would risk breaking RADIUS auth.

    A station whose MAC matched in arm 2 never reaches arm 3 (no double count).
    Existing rows keep updating as before and their ``subscriber_id`` is
    never touched.
    """
    uisp_id = _device_id(station)
    mac = _norm_mac(_ident(station).get("mac"))
    cpe = (
        session.query(CPEDevice)
        .filter(CPEDevice.uisp_device_id == uisp_id)
        .one_or_none()
    )
    created = False
    adopted = False
    changed = False
    if cpe is None:
        candidates = _adoption_candidates(session, mac)
        if len(candidates) > 1:
            logger.warning(
                "uisp_sync_adoption_ambiguous uisp_id=%s mac=%s candidates=%d",
                uisp_id,
                mac,
                len(candidates),
            )
            stats["ambiguous"] += 1
            return None, StationMatchDisposition.ambiguous
        if candidates:
            cpe = candidates[0]
            cpe.uisp_device_id = uisp_id
            adopted = True
            changed = True
            stats["adopted"] += 1
    if cpe is None:
        services = mac_index.get(mac or "", set())
        if len(services) > 1:
            stats["ambiguous"] += 1
            return None, StationMatchDisposition.ambiguous
        if len(services) == 1:
            subscriber_id, subscription_id = next(iter(services))
            created = True
            cpe = CPEDevice(
                uisp_device_id=uisp_id,
                subscriber_id=subscriber_id,
                subscription_id=subscription_id,
                device_type=DeviceType.wireless_radio,
                vendor="ubiquiti",
            )
            session.add(cpe)
            stats["matched"] += 1
    if cpe is None:
        # SECONDARY ARM — the MAC found no subscriber (typical for bridge-mode
        # radios, whose subscription authenticates on the customer router MAC).
        # Corroborated IP+name only: a unique ACTIVE ipv4 hit AND a name match.
        ip_match = _match_by_ip_name(station, ip_index)
        if ip_match is None:
            stats["unmatched_no_subscriber"] += 1
            return None, StationMatchDisposition.unmatched
        subscriber_id, subscription_id, ip, score = ip_match
        created = True
        cpe = CPEDevice(
            uisp_device_id=uisp_id,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            device_type=DeviceType.wireless_radio,
            vendor="ubiquiti",
            notes=f"linked via UISP ip+name {ip} sim={score:.2f}",
        )
        session.add(cpe)
        stats["matched_by_ip_name"] += 1

    if cpe.subscription_id is None:
        services = mac_index.get(mac or _norm_mac(cpe.mac_address) or "", set())
        if len(services) == 1:
            subscriber_id, subscription_id = next(iter(services))
            if subscriber_id == cpe.subscriber_id:
                cpe.subscription_id = subscription_id
                changed = True

    ident = _ident(station)
    changed |= _fill(cpe, "mac_address", ident.get("mac"))
    changed |= _fill(cpe, "model", ident.get("model"))
    changed |= _fill(cpe, "serial_number", _serial(station))
    changed |= _fill(cpe, "vendor", "ubiquiti")
    firmware_version = _firmware_version(station)
    if firmware_version and cpe.firmware_version != firmware_version:
        cpe.firmware_version = firmware_version
        changed = True
    # Upgrade only the placeholder type; a human-chosen type is never stomped.
    if not created and cpe.device_type in (None, DeviceType.other):
        cpe.device_type = DeviceType.wireless_radio
        changed = True

    status = _device_status(station)
    if status and cpe.last_uisp_status != status:
        cpe.last_uisp_status = status
        changed = True
    cpe.uisp_synced_at = now

    session.flush()
    if created:
        stats["created"] += 1
        disposition = StationMatchDisposition.created
    elif adopted:
        # Already counted as ``adopted``; not an update of a synced row.
        disposition = StationMatchDisposition.adopted
    elif changed:
        stats["updated"] += 1
        disposition = StationMatchDisposition.existing
    else:
        stats["unchanged"] += 1
        disposition = StationMatchDisposition.existing
    return cpe, disposition


def _upsert_olt(
    session: Session,
    device: dict,
    unique_ips: set[str],
    stats: Counter,
) -> OLTDevice | None:
    """Upsert a UF-OLT into olt_devices (match-don't-create, then create)."""
    uisp_id = _device_id(device)
    name = _device_name(device)
    ip = _mgmt_ip(device)
    olt = (
        session.query(OLTDevice)
        .filter(OLTDevice.uisp_device_id == uisp_id)
        .one_or_none()
    )
    if olt is None and ip and ip in unique_ips:
        olt = session.query(OLTDevice).filter(OLTDevice.mgmt_ip == ip).one_or_none()
    if olt is None and name:
        candidates = (
            session.query(OLTDevice)
            .filter(or_(OLTDevice.hostname == name, OLTDevice.name == name))
            .all()
        )
        if len(candidates) == 1:
            olt = candidates[0]

    created = olt is None
    changed = False
    if olt is None:
        # is_active=False: ``ck_olt_devices_config_pack_required`` (see
        # migration 154) demands a populated provisioning config_pack
        # (internet/management VLANs + TR-069 profile) for ACTIVE OLTs, and a
        # UF-OLT imported from UISP has none — it is a monitoring/topology
        # object, not a provisioning target. Inactive placeholder rows are the
        # combination the constraint exempts; the topology read-side
        # (customer_path/affected/gaps/crm_api) resolves OLTs by primary key
        # with no is_active filter, so UF-OLTs stay fully traceable. An
        # operator activating one later must supply a config pack — exactly
        # the governance the constraint encodes. Pre-existing matched rows are
        # never flipped.
        olt = OLTDevice(
            name=name or f"uisp-{uisp_id[:8]}",
            vendor="ubiquiti",
            is_active=False,
        )
        session.add(olt)

    ident = _ident(device)
    changed |= _fill(olt, "vendor", "ubiquiti")
    changed |= _fill(olt, "model", ident.get("model"))
    changed |= _fill(olt, "serial_number", _serial(device))
    if ip and ip in unique_ips:
        changed |= _fill(olt, "mgmt_ip", ip)
    if olt.uisp_device_id != uisp_id:
        # Release the id from any other row first (a fallback ip/name match
        # can land on a row while another still holds this uisp id) so the
        # partial-unique index can't reject the flush.
        release = session.query(OLTDevice).filter(OLTDevice.uisp_device_id == uisp_id)
        if olt.id is not None:
            release = release.filter(OLTDevice.id != olt.id)
        release.update({OLTDevice.uisp_device_id: None}, synchronize_session=False)
        olt.uisp_device_id = uisp_id
        changed = True

    session.flush()
    if created:
        stats["created"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    return olt


def _upsert_onu(
    session: Session,
    device: dict,
    olt_ids_by_uisp: dict[str, UUID],
    seen_onu_keys: set[tuple],
    now: datetime,
    stats: Counter,
) -> OntUnit | None:
    """Upsert a UFiber ONU into ont_units; parent via attributes.parentId."""
    uisp_id = _device_id(device)
    parent_uisp_id = str(_attributes(device).get("parentId") or "").strip()
    olt_id = olt_ids_by_uisp.get(parent_uisp_id)
    if olt_id is None:
        stats["skipped"] += 1
        return None

    ident = _ident(device)
    mac = _norm_mac(ident.get("mac"))
    serial = _serial(device) or (mac.upper() if mac else None) or f"UISP-{uisp_id[:8]}"

    # Guard the (olt_device_id, serial_number) unique key in code: a second
    # UISP device collapsing onto the same key this run is skipped rather than
    # flushed into an IntegrityError that would poison the session.
    key = (olt_id, serial)
    if key in seen_onu_keys:
        stats["skipped"] += 1
        return None
    seen_onu_keys.add(key)

    ont = session.query(OntUnit).filter(OntUnit.uisp_device_id == uisp_id).one_or_none()
    if ont is not None:
        current_olt = (
            session.get(OLTDevice, ont.olt_device_id)
            if ont.olt_device_id is not None
            else None
        )
        uisp_owned = str(ont.vendor or "").strip().lower() == "ubiquiti" or (
            current_olt is not None
            and current_olt.uisp_device_id is not None
            and str(current_olt.vendor or "").strip().lower() == "ubiquiti"
        )
        if not uisp_owned:
            logger.warning(
                "uisp_sync_onu_foreign_owner_skipped uisp_id=%s ont=%s olt=%s",
                uisp_id,
                ont.id,
                ont.olt_device_id,
            )
            stats["skipped"] += 1
            return None
    if ont is None:
        ont = (
            session.query(OntUnit)
            .filter(OntUnit.olt_device_id == olt_id, OntUnit.serial_number == serial)
            .one_or_none()
        )

    created = ont is None
    changed = False
    if ont is None:
        # Electronic topology is projected separately through the observation
        # owner after the per-OLT listing supplies exact port evidence.
        ont = OntUnit(serial_number=serial, vendor="ubiquiti")
        session.add(ont)

    changed |= _fill(ont, "name", ident.get("name"))
    changed |= _fill(ont, "model", ident.get("model"))
    changed |= _fill(ont, "vendor", "ubiquiti")
    changed |= _fill(ont, "mac_address", ident.get("mac"))
    if ont.uisp_device_id != uisp_id:
        # Release the id from any other row first (the (olt, serial) fallback
        # can land on a row while another still holds this uisp id) so the
        # partial-unique index can't reject the flush.
        release = session.query(OntUnit).filter(OntUnit.uisp_device_id == uisp_id)
        if ont.id is not None:
            release = release.filter(OntUnit.id != ont.id)
        release.update({OntUnit.uisp_device_id: None}, synchronize_session=False)
        ont.uisp_device_id = uisp_id
        changed = True
    ont.last_sync_source = "uisp"
    ont.last_sync_at = now

    # Live telemetry UISP owns for UF-OLT ONUs (Huawei ONTs get theirs from
    # SNMP — different rows). Applied only when UISP actually reports a status,
    # so a missing field never blanks a known value. olt_status is a
    # status-owner field: it MUST go through the sanctioned setter (it also
    # stamps olt_status_seen_at / last_seen_at / offline_reason), never a
    # direct assignment here. Signal is not status-owned, so it's set inline.
    status = _onu_status(device)
    if status is not None and (
        ont.olt_status != status or ont.olt_status_seen_at is None
    ):
        apply_olt_status_observation(ont, status, now=now)
        changed = True
    signal = _onu_signal(device)
    if signal is not None and ont.onu_rx_signal_dbm != signal:
        ont.onu_rx_signal_dbm = signal
        changed = True

    session.flush()
    if created:
        stats["created"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    return ont


def _onu_parent_olt_id(device: dict, olt_ids_by_uisp: dict[str, UUID]) -> UUID | None:
    parent_uisp_id = str(_attributes(device).get("parentId") or "").strip()
    return olt_ids_by_uisp.get(parent_uisp_id)


def _collect_onu_ports(
    client, olt_ids_by_uisp: dict[str, UUID], stats: Counter
) -> dict[str, int]:
    """ONU uisp id -> OLT-side PON port, from one per-OLT listing per UF-OLT.

    A single OLT's listing failing is logged and counted
    (``port_fetch_failures``) but never aborts the run: the affected ONUs
    simply keep their current ``pon_port_id``.
    """
    ports: dict[str, int] = {}
    for olt_uisp_id in olt_ids_by_uisp:
        try:
            entries = client.list_olt_onus(olt_uisp_id)
        except Exception as exc:  # one OLT failing must not abort the run
            stats["port_fetch_failures"] += 1
            logger.warning("uisp_olt_onu_list_failed olt=%s: %s", olt_uisp_id, exc)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            onu_uisp_id = _device_id(entry)
            port = _onu_pon_port(entry)
            if onu_uisp_id and port is not None:
                ports.setdefault(onu_uisp_id, port)
    return ports


def _record_onu_topology_observation(
    session: Session,
    ont: OntUnit,
    device: dict,
    olt_ids_by_uisp: dict[str, UUID],
    onu_ports_by_uisp: dict[str, int],
    stats: Counter,
    now: datetime,
) -> None:
    """Delegate one exact UISP location observation to the topology owner."""
    port_number = onu_ports_by_uisp.get(_device_id(device))
    olt_pk = _onu_parent_olt_id(device, olt_ids_by_uisp)
    if olt_pk is None:
        return
    result = observe_ont_electronic_topology(
        session,
        source="uisp",
        evidence_key=_device_id(device),
        ont_unit_id=ont.id,
        observed_olt_id=olt_pk,
        observed_port_number=port_number,
        observed_port_label=(f"pon{port_number}" if port_number is not None else None),
        observed_at=now,
    )
    if result.pon_created:
        stats["pon_ports_initialized"] += 1
    if result.ont_updated:
        stats["onu_topology_initialized"] += 1
    if result.outcome == "confirmed":
        stats["onu_topology_confirmed"] += 1
    elif result.outcome == "incomplete":
        stats["onu_topology_incomplete"] += 1
    elif result.review_required:
        stats["onu_topology_review_required"] += 1
        logger.warning(
            "uisp_onu_topology_review_required onu=%s uisp_id=%s reason=%s",
            ont.id,
            ont.uisp_device_id,
            result.reason,
            extra={
                "event": "uisp_onu_topology_review_required",
                "ont_unit_id": str(ont.id),
                "uisp_device_id": ont.uisp_device_id,
                "evidence_id": str(result.evidence.id),
                "reason": result.reason,
            },
        )


def _import_data_links(session: Session, client, now: datetime, stats: Counter) -> None:
    """Import active UISP data-links as non-authoritative backhaul observations.

    Owns ``source='uisp_data_link'`` rows only (upsert + soft-prune), mirroring
    the LLDP poller. A link is kept only when BOTH endpoints resolve to a
    ``network_devices`` row by stamped ``uisp_device_id`` — client stations
    (``cpe_devices``) never carry a network-device uisp id, so customer links
    are excluded structurally. Endpoint/shape variance is tolerated per link
    (a malformed link is skipped, never fatal); UISP unreachable is counted and
    leaves existing links untouched.
    """
    # Fetch before the endpoint lookup so the HTTP call runs with no
    # transaction open behind it.
    try:
        links = client.list_data_links()
    except Exception as exc:  # UISP unreachable must not abort the sync
        stats["link_fetch_failures"] += 1
        logger.warning("uisp_data_links_fetch_failed: %s", exc)
        return
    node_by_uisp: dict[str, UUID] = {
        uid: nid
        for uid, nid in session.query(
            NetworkDevice.uisp_device_id, NetworkDevice.id
        ).filter(NetworkDevice.uisp_device_id.isnot(None))
    }

    existing_active_links = (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == _DATA_LINK_SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .count()
    )
    if not links and existing_active_links:
        stats["link_prune_guarded"] += existing_active_links
        logger.warning(
            "uisp_data_links_empty_prune_guard active_links=%s",
            existing_active_links,
        )
        return

    edges: dict[tuple[UUID, UUID], TopologyLinkMedium] = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        if _link_is_disabled(link):
            stats["links_skipped"] += 1
            continue
        a = _link_endpoint_uisp_id(link.get("from"))
        b = _link_endpoint_uisp_id(link.get("to"))
        na = node_by_uisp.get(a) if a else None
        nb = node_by_uisp.get(b) if b else None
        if na is None or nb is None or na == nb:
            stats["links_skipped"] += 1
            continue
        key = _canonical(na, nb)
        if key not in edges:
            edges[key] = _link_medium(link)

    for key, medium in edges.items():
        existing = (
            session.query(NetworkTopologyLink)
            .filter(
                NetworkTopologyLink.source == _DATA_LINK_SOURCE,
                NetworkTopologyLink.source_device_id == key[0],
                NetworkTopologyLink.target_device_id == key[1],
                NetworkTopologyLink.source_interface_id.is_(None),
                NetworkTopologyLink.target_interface_id.is_(None),
            )
            .first()
        )
        if existing is None:
            session.add(
                NetworkTopologyLink(
                    source_device_id=key[0],
                    target_device_id=key[1],
                    source=_DATA_LINK_SOURCE,
                    medium=medium,
                    is_active=True,
                    discovered_at=now,
                    last_seen_at=now,
                )
            )
            stats["links_created"] += 1
        else:
            existing.medium = medium
            existing.is_active = True
            existing.last_seen_at = now
            stats["links_updated"] += 1
    session.flush()

    for link_row in (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == _DATA_LINK_SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    ):
        pair = _canonical(link_row.source_device_id, link_row.target_device_id)
        if pair not in edges:
            link_row.is_active = False
            stats["links_pruned"] += 1
    session.flush()


def sync(
    session: Session,
    client,
    now: datetime | None = None,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict:
    """Run one UISP topology sync pass; returns the counter summary.

    Read-only against UISP. Idempotent: every row is keyed by its stable
    ``uisp_device_id``; re-runs only bump sync timestamps. Each device is
    upserted inside its own SAVEPOINT (``Session.begin_nested``, the
    per-item SAVEPOINT idiom): a flush failure rolls back to the savepoint,
    is counted as ``failed`` and never aborts the run or poisons the
    session. Single-flight locking lives in the Celery task wrapper
    (``db_session_adapter.advisory_lock``), not here.

    ``checkpoint`` commits the work so far and must leave the session
    transaction-free. It is called immediately before each block of UISP
    HTTP, because this pass fans out one request per AP and one per UF-OLT:
    holding the transaction across that fan-out exceeded PostgreSQL's
    ``idle_in_transaction_session_timeout`` in production and lost the whole
    pass. The per-item SAVEPOINTs are unaffected — they open and close well
    inside a single phase.

    Checkpointing means a failed pass can leave earlier phases committed.
    That is safe precisely because this sync is idempotent: the next pass
    re-derives the same rows from the same stable ids. Single-flight still
    holds across a checkpoint — the advisory lock is session-level and its
    connection is pinned, so a commit does not release it.
    """
    now = now or _now()
    checkpoint = checkpoint or session.commit
    stats = _blank_stats()
    sites = client.list_sites()
    devices = client.list_devices()
    excluded_sites = excluded_site_ids(sites)

    kept: list[dict] = []
    for device in devices:
        if not _device_id(device):
            continue
        site_id = _device_site_id(device)
        if site_id and site_id in excluded_sites:
            stats["excluded_archive"] += 1
            continue
        kept.append(device)

    unique_ips = _unique_ips(kept)
    aps = [d for d in kept if _is_ap(d)]
    olts = [d for d in kept if _is_olt(d)]
    onus = [d for d in kept if _is_onu(d)]
    stations = [d for d in kept if _is_station(d) and not _is_olt(d) and not _is_onu(d)]

    # --- APs -> network_devices (match-only; stamps uisp_device_id) ---
    ap_nodes = match_ap_nodes(session, aps, unique_ips, stats)
    session.flush()

    # --- Wireless customer radios -> cpe_devices ---
    # PtP backhaul masters report role=station but are infrastructure: a
    # station whose name matches an existing network_devices row must not
    # create a duplicate CPE (match-don't-create).
    infra_names, _infra_ips = build_device_index(session)

    # Subscriber matching moved BEFORE creation (match-then-create):
    # cpe_devices.subscriber_id is NOT NULL, so the owner must be resolved
    # before any INSERT is attempted.
    mac_index = _active_subscriber_macs(session) if stations else {}
    # Secondary IP+name arm index (unique ACTIVE ipv4 -> subscriber + names),
    # built once per run like the MAC index. Reads ipv4_address only — the
    # subscription MAC is never touched by the IP+name path.
    ip_index = _active_subscriber_ips(session) if stations else {}

    # Every read this phase needs is now materialized, so end the transaction
    # before the per-AP fan-out below. The matched AP ids are captured as
    # plain strings while they are still loaded.
    matched_ap_ids = set(ap_nodes)
    checkpoint()

    # Unconditional: the AP-side listing is both the association fallback and
    # the ONLY source of the station RSSI, so it is fetched every run to
    # ingest the RF signal together with the edge it describes. One request
    # per matched active AP, with no transaction open.
    ap_index = (
        _ap_side_station_index(client, aps, matched_ap_ids)
        if stations
        else (ApStationIndex())
    )

    # Seen = what UISP *reports*, taken from the raw inventory BEFORE the
    # upsert loop: a device whose upsert fails transiently is still present
    # in UISP and must not be soft-pruned to 'vanished'.
    seen_cpe_uisp_ids: set[str] = {_device_id(s) for s in stations}
    for station in stations:
        uisp_id = _device_id(station)
        if _norm_label(_device_name(station)) in infra_names:
            stats["skipped"] += 1
            continue
        mac = _norm_mac(_ident(station).get("mac"))
        # Deferred past the upsert savepoint so a queue write can never fail
        # (or lock) the station's own upsert.
        review: _StationReview | None = None
        try:
            # SAVEPOINT per item: a flush failure
            # rolls back only this device, not the surrounding transaction.
            with session.begin_nested():
                cpe, disposition = _upsert_station(
                    session, station, mac_index, ip_index, now, stats
                )
                if cpe is None:
                    # No confirmed subscriber match: nothing was created, the
                    # counters say why — and the disposition feeds the review
                    # queue below so the miss becomes a repairable work item.
                    if disposition is StationMatchDisposition.ambiguous:
                        review = _StationReview(
                            reason=unmatched_radio_queue.REASON_CONFLICT,
                            title=(
                                f"Radio MAC maps to multiple subscribers: "
                                f"{_device_name(station) or mac}"
                            ),
                            description=(
                                f"UISP station '{_device_name(station)}' "
                                f"(uisp_device_id {uisp_id}, MAC {mac}) matches "
                                "more than one candidate (active subscription "
                                "MACs or install-time placeholders), so the "
                                "sync cannot pick an owner. Resolve which "
                                "subscriber owns this radio."
                            ),
                            details={"uisp_device_id": uisp_id},
                        )
                    elif disposition is StationMatchDisposition.unmatched:
                        review = _StationReview(
                            reason=unmatched_radio_queue.REASON_UISP_UNMATCHED,
                            title=(
                                f"UISP radio matches no subscriber: "
                                f"{_device_name(station) or mac}"
                            ),
                            description=(
                                f"UISP station '{_device_name(station)}' "
                                f"(uisp_device_id {uisp_id}, MAC {mac}) matches "
                                "no active subscription MAC, no install-time "
                                "placeholder, and no corroborated IP+name "
                                "candidate. Link the radio to its customer or "
                                "retire it in UISP."
                            ),
                            details={
                                "uisp_device_id": uisp_id,
                                "station_name": _device_name(station) or None,
                                "ap_uisp_id": _station_parent_ap_id(station)
                                or ap_index.ap_for(uisp_id, mac),
                            },
                        )
                else:
                    parent_ap_id = _station_parent_ap_id(station) or ap_index.ap_for(
                        uisp_id, mac
                    )
                    node = ap_nodes.get(parent_ap_id) if parent_ap_id else None
                    if node is not None and cpe.parent_network_device_id != node.id:
                        _note_edge_move(session, cpe, station, node, stats)
                        cpe.parent_network_device_id = node.id
                        stats["edges_set"] += 1
                    if node is None and parent_ap_id:
                        # UISP knows the serving AP but no network_devices row
                        # matches it: the customer renders "AP unresolved"
                        # until someone repairs the AP mapping.
                        review = _StationReview(
                            reason=unmatched_radio_queue.REASON_AP_UNRESOLVED,
                            title=(
                                f"Radio's serving AP unmatched in topology: "
                                f"{_device_name(station) or mac}"
                            ),
                            description=(
                                f"UISP reports station '{_device_name(station)}' "
                                f"(MAC {mac}) served by UISP AP {parent_ap_id}, "
                                "but no network_devices row matches that AP, so "
                                "the radio cannot be parented and the customer "
                                "shows 'AP unresolved'. Match the AP (mgmt IP "
                                "or hostname) to its topology node."
                            ),
                            details={
                                "uisp_device_id": uisp_id,
                                "ap_uisp_id": parent_ap_id,
                                "cpe_device_id": str(cpe.id),
                            },
                        )
                    _apply_station_signal(
                        cpe, station, ap_index.signal_for(uisp_id, mac), now, stats
                    )
                    session.flush()
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_station_failed uisp_id=%s", uisp_id)
            continue
        if review is not None and mac:
            _enqueue_station_review(session, mac=mac, review=review, stats=stats)

    # --- UF-OLTs -> olt_devices ---
    olt_ids_by_uisp: dict[str, UUID] = {}
    for device in olts:
        try:
            with session.begin_nested():
                olt = _upsert_olt(session, device, unique_ips, stats)
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_olt_failed uisp_id=%s", _device_id(device))
            continue
        if olt is not None:
            olt_ids_by_uisp[_device_id(device)] = olt.id

    # --- PON-port granularity: per-OLT ONU listings (onu.port) ---
    # Second fan-out: one request per UF-OLT. The OLT upserts above supply its
    # input, so commit them and enter the fan-out transaction-free.
    checkpoint()
    onu_ports_by_uisp = _collect_onu_ports(client, olt_ids_by_uisp, stats)
    # --- UFiber ONUs -> ont_units ---
    seen_onu_keys: set[tuple] = set()
    for device in onus:
        try:
            with session.begin_nested():
                ont = _upsert_onu(
                    session, device, olt_ids_by_uisp, seen_onu_keys, now, stats
                )
                if ont is not None:
                    _record_onu_topology_observation(
                        session,
                        ont,
                        device,
                        olt_ids_by_uisp,
                        onu_ports_by_uisp,
                        stats,
                        now,
                    )
                    session.flush()
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_onu_failed uisp_id=%s", _device_id(device))

    # --- Soft-prune: radios that vanished from UISP ---
    # Only the columns this sync owns are touched; operator-managed fields
    # (status, subscriber link) are never flipped by a prune.
    stale = (
        session.query(CPEDevice)
        .filter(
            CPEDevice.uisp_device_id.isnot(None),
            or_(
                CPEDevice.last_uisp_status.is_(None),
                CPEDevice.last_uisp_status != "vanished",
            ),
        )
        .all()
    )
    if not stations and stale:
        stats["prune_guarded"] += len(stale)
        logger.warning("uisp_station_inventory_empty_prune_guard rows=%s", len(stale))
    else:
        for cpe in stale:
            if cpe.uisp_device_id not in seen_cpe_uisp_ids:
                if cpe.last_uisp_status == "missing":
                    cpe.last_uisp_status = "vanished"
                    stats["pruned"] += 1
                else:
                    cpe.last_uisp_status = "missing"
                # A radio gone from UISP has no current RF observation; a
                # retained value must never render as a live signal.
                _clear_rf_signal(cpe, stats)
                cpe.uisp_synced_at = now
    session.flush()

    # --- Backhaul topology: UISP data-links -> NetworkTopologyLink ---
    # One more request; it is issued first inside the importer, so end the
    # transaction here too.
    checkpoint()
    _import_data_links(session, client, now, stats)

    # Reuse this exact inventory read for desired/observed convergence. The
    # savepoint keeps an intent persistence failure from rolling back topology.
    try:
        from app.services.uisp_control_plane import reconcile_inventory

        with session.begin_nested():
            intent_result = reconcile_inventory(session, devices, now=now, commit=False)
        for key, value in intent_result.items():
            stats[f"intents_{key}"] += value
    except Exception:
        stats["intent_reconcile_failed"] += 1
        logger.exception("uisp_intent_reconcile_failed")

    result = dict(stats)
    log_extra = {"event": "uisp_topology_sync_complete"}
    log_extra.update({f"uisp_{key}": value for key, value in result.items()})
    logger.info(
        "uisp_topology_sync_complete",
        extra=log_extra,
    )
    return result
