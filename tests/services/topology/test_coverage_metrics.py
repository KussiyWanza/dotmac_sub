"""Topology coverage + pipeline-health metrics exporter."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import CPEDevice, OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, NetworkTopologyLink, PopSite
from app.models.subscriber import Subscriber
from app.services.topology import coverage_metrics as cm
from app.services.topology.gaps import topology_gaps


class _FakeRedis:
    """Minimal stand-in for the app_cache redis client (decode_responses)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def mget(self, keys):
        return [self.store.get(key) for key in keys]


@pytest.fixture
def fake_cache(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(
        "app.services.app_cache.get_cache_redis",
        lambda force_reconnect=False: fake,
    )
    return fake


def _samples_by_key(samples):
    return {
        (name, tuple(sorted(labels.items()))): value for name, labels, value in samples
    }


def _sub(subscriber_id, offer_id, **kwargs):
    return Subscription(
        subscriber_id=subscriber_id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
        **kwargs,
    )


def _subscriber(db_session, tag):
    row = Subscriber(
        first_name="Cov",
        last_name=tag,
        email=f"{tag}-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _seed_mixed_fixture(db_session, subscriber, catalog_offer):
    """Mixed-medium fixture:

    - fiber complete (ONT -> OLT -> node -> pop)
    - fiber gapped no_node (ONT -> OLT, no node)
    - wireless complete (radio -> AP -> pop)
    - nas gapped no_basestation (NAS -> node without pop)
    - unknown gapped no_ont (nothing at all)
    """
    # Fiber complete.
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(
        NetworkDevice(
            name="olt1-node",
            source="zabbix_reconcile",
            matched_device_type="olt",
            matched_device_id=olt.id,
            pop_site_id=pop.id,
            zabbix_hostid="201",
        )
    )
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    subs = [_sub(subscriber.id, catalog_offer.id)]

    # Fiber gapped: ONT -> OLT exists, but no topology node for the OLT.
    fiber_gapped = _subscriber(db_session, "fibergap")
    olt2 = OLTDevice(name="OLT-2", hostname="olt2", mgmt_ip="10.0.0.2")
    db_session.add(olt2)
    db_session.flush()
    ont2 = OntUnit(serial_number="SN-2", olt_device_id=olt2.id)
    db_session.add(ont2)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont2.id, subscriber_id=fiber_gapped.id, active=True)
    )
    subs.append(_sub(fiber_gapped.id, catalog_offer.id))

    # Wireless complete: radio -> AP -> pop.
    wireless = _subscriber(db_session, "wireless")
    bts = PopSite(name="BTS-1", zabbix_group_id="40")
    db_session.add(bts)
    db_session.flush()
    ap = NetworkDevice(name="ap-1", pop_site_id=bts.id, uisp_device_id="uisp-ap-1")
    db_session.add(ap)
    db_session.flush()
    db_session.add(
        CPEDevice(
            subscriber_id=wireless.id,
            parent_network_device_id=ap.id,
            last_uisp_status="active",
        )
    )
    subs.append(_sub(wireless.id, catalog_offer.id))

    # NAS gapped: NAS -> node, but the node has no pop_site.
    nas_sub = _subscriber(db_session, "nas")
    nas = NasDevice(name="NAS-1", management_ip="10.0.0.5")
    db_session.add(nas)
    db_session.flush()
    db_session.add(
        NetworkDevice(
            name="nas1-node",
            source="zabbix_reconcile",
            matched_device_type="nas",
            matched_device_id=nas.id,
            pop_site_id=None,
            zabbix_hostid="301",
        )
    )
    subs.append(_sub(nas_sub.id, catalog_offer.id, provisioning_nas_device_id=nas.id))

    # Nothing at all: no ONT, no radio, no NAS.
    orphan = _subscriber(db_session, "orphan")
    subs.append(_sub(orphan.id, catalog_offer.id))

    db_session.add_all(subs)
    db_session.flush()


def test_coverage_ratios_from_mixed_fixture(
    db_session, subscriber, catalog_offer, fake_cache
):
    _seed_mixed_fixture(db_session, subscriber, catalog_offer)

    samples = _samples_by_key(cm.collect_topology_metrics(db_session))

    def active(medium):
        return samples[("topology_subscribers_active", (("medium", medium),))]

    def ratio(medium):
        return samples[("topology_e2e_coverage_ratio", (("medium", medium),))]

    def gapped(medium, gap):
        return samples[
            ("topology_subscribers_gapped", (("gap", gap), ("medium", medium)))
        ]

    assert active("fiber") == 2.0
    assert active("wireless") == 1.0
    assert active("nas") == 1.0
    assert active("unknown") == 1.0

    assert ratio("fiber") == 0.5
    assert ratio("wireless") == 1.0
    assert ratio("nas") == 0.0
    assert ratio("unknown") == 0.0

    assert gapped("fiber", "no_node") == 1.0
    assert gapped("nas", "no_basestation") == 1.0
    assert gapped("unknown", "no_ont") == 1.0
    # Full cross-product is emitted with explicit zeros.
    assert gapped("wireless", "no_node") == 0.0
    assert gapped("fiber", "no_ont") == 0.0


def test_gap_labels_match_gaps_page(db_session, subscriber, catalog_offer, fake_cache):
    _seed_mixed_fixture(db_session, subscriber, catalog_offer)

    page = topology_gaps(db_session)
    page_gap_counts = Counter(row["gap"] for row in page.subscription_gaps)

    metric_gap_counts: Counter[str] = Counter()
    active_total = 0.0
    for name, labels, value in cm.collect_topology_metrics(db_session):
        if name == "topology_subscribers_gapped":
            metric_gap_counts[labels["gap"]] += int(value)
        elif name == "topology_subscribers_active":
            active_total += value

    assert +metric_gap_counts == +page_gap_counts
    assert active_total == float(page.active_subscriptions)
    assert sum(metric_gap_counts.values()) == page.subscription_gap_count


def test_task_stats_round_trip(fake_cache):
    stats = {"created": 3, "updated": 2, "failed": 629}
    assert cm.store_task_stats("uisp_sync", stats) is True

    payload = cm.read_task_stats("uisp_sync")
    assert payload is not None
    assert payload["stats"] == stats
    age = datetime.now(UTC).timestamp() - payload["stored_at"]
    assert 0 <= age < 60


def test_task_health_counters_emitted(db_session, fake_cache):
    cm.store_task_stats("uisp_sync", {"created": 1, "failed": 629, "skipped": None})

    samples = _samples_by_key(cm.collect_topology_metrics(db_session))

    assert (
        samples[
            (
                "topology_task_last_result",
                (("counter", "failed"), ("task", "uisp_sync")),
            )
        ]
        == 629.0
    )
    # Non-numeric stats values become presence markers.
    assert (
        samples[
            (
                "topology_task_last_result",
                (("counter", "skipped"), ("task", "uisp_sync")),
            )
        ]
        == 1.0
    )
    staleness = samples[("topology_task_staleness_seconds", (("task", "uisp_sync"),))]
    assert 0 <= staleness < 60
    # lldp never ran: sentinel.
    assert (
        samples[("topology_task_staleness_seconds", (("task", "lldp_poll"),))]
        == cm.NEVER_RUN_SENTINEL_SECONDS
    )


def test_staleness_sentinel_when_cache_empty(db_session, fake_cache):
    samples = _samples_by_key(cm.collect_topology_metrics(db_session))
    for task in cm.TRACKED_TASKS:
        assert (
            samples[("topology_task_staleness_seconds", (("task", task),))]
            == cm.NEVER_RUN_SENTINEL_SECONDS
        )


def test_source_freshness(db_session, subscriber, fake_cache):
    now = datetime.now(UTC)
    db_session.add(
        CPEDevice(
            subscriber_id=subscriber.id, uisp_synced_at=now - timedelta(seconds=120)
        )
    )
    left = NetworkDevice(name="sw-a", zabbix_hostid="401")
    right = NetworkDevice(name="sw-b", zabbix_hostid="402")
    db_session.add_all([left, right])
    db_session.flush()
    db_session.add(
        NetworkTopologyLink(
            source_device_id=left.id,
            target_device_id=right.id,
            source="lldp_neighbor",
            last_seen_at=now - timedelta(seconds=300),
        )
    )
    db_session.flush()

    samples = _samples_by_key(
        cm.collect_topology_metrics(db_session, now=now.timestamp())
    )

    uisp = samples[("topology_source_freshness_seconds", (("source", "uisp"),))]
    lldp = samples[("topology_source_freshness_seconds", (("source", "lldp"),))]
    assert uisp == pytest.approx(120, abs=2)
    assert lldp == pytest.approx(300, abs=2)


def test_source_freshness_sentinel_when_no_rows(db_session, fake_cache):
    samples = _samples_by_key(cm.collect_topology_metrics(db_session))
    for source in ("uisp", "lldp"):
        assert (
            samples[("topology_source_freshness_seconds", (("source", source),))]
            == cm.NEVER_RUN_SENTINEL_SECONDS
        )


# --- VM push (transport faked the same way the bandwidth adapter tests do) ---

_LINE_RE = re.compile(
    r"^[a-z0-9_]+\{[a-z0-9_]+=\"[^\"]*\"(,[a-z0-9_]+=\"[^\"]*\")*\}"
    r" -?\d+(\.\d+([eE][+-]?\d+)?)? \d+$"
)


def _mock_writer(post_side_effect=None):
    from app.services.bandwidth_metrics_adapter import VictoriaMetricsWriter

    mock_client = MagicMock()
    if post_side_effect is not None:
        mock_client.post.side_effect = post_side_effect
    else:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response
    mock_client.is_closed = False

    writer = VictoriaMetricsWriter()
    writer._client = mock_client
    return writer, mock_client


def test_export_pushes_well_formed_series(
    db_session, subscriber, catalog_offer, fake_cache, monkeypatch
):
    _seed_mixed_fixture(db_session, subscriber, catalog_offer)
    writer, mock_client = _mock_writer()
    monkeypatch.setattr(cm, "_writer", writer)

    result = cm.export_topology_metrics(db_session)

    assert result["success"] is True
    assert result["series"] == result["pushed"] > 0
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert call.args[0].endswith("/api/v1/import/prometheus")
    lines = call.kwargs["content"].split("\n")
    assert len(lines) == result["series"]
    for line in lines:
        assert _LINE_RE.match(line), line
    assert any(line.startswith("topology_e2e_coverage_ratio{") for line in lines)
    assert any(line.startswith("topology_task_staleness_seconds{") for line in lines)
    assert any(line.startswith("topology_source_freshness_seconds{") for line in lines)


def test_export_reports_failure_and_retries(db_session, fake_cache, monkeypatch):
    writer, mock_client = _mock_writer(
        post_side_effect=httpx.HTTPError("connection refused")
    )
    monkeypatch.setattr(cm, "_writer", writer)
    monkeypatch.setattr(cm.time, "sleep", lambda _s: None)

    result = cm.export_topology_metrics(db_session)

    assert result["success"] is False
    assert result["pushed"] == 0
    assert mock_client.post.call_count == cm.VM_WRITE_MAX_ATTEMPTS


# --- Wrapper stash guards -----------------------------------------------------


def test_lldp_wrapper_stashes_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import Session

    from app.services.topology.lldp_contracts import (
        LldpPoll,
        LldpReadQuery,
        LldpSnapshot,
        LldpStats,
        ReconcileLldpCommand,
    )
    from app.tasks import topology_lldp
    from tests.test_lldp_transaction_span import snapshot

    source = snapshot()

    def read(db: Session, *, query: LldpReadQuery) -> LldpSnapshot:
        return source

    def collect(*, snapshot: LldpSnapshot) -> LldpPoll:
        return LldpPoll(snapshot, (), frozenset(), LldpStats(neighbors_seen=5))

    def persist(db: Session, *, command: ReconcileLldpCommand) -> LldpStats:
        assert command.poll.stats.neighbors_seen == 5
        return LldpStats(created=2, neighbors_seen=5)

    stored = {}
    monkeypatch.setattr(
        topology_lldp,
        "store_task_stats",
        lambda task, stats: stored.setdefault(task, stats),
    )
    fake_db = MagicMock()
    monkeypatch.setattr(
        topology_lldp.db_session_adapter, "create_session", lambda: fake_db
    )
    monkeypatch.setattr(
        "app.services.topology.lldp_poller.poll_all",
        collect,
    )
    monkeypatch.setattr("app.services.topology.lldp_poller.read_snapshot", read)
    monkeypatch.setattr(
        "app.services.topology.lldp_poller.reconcile_poll",
        persist,
    )

    result = topology_lldp.run_lldp_topology_poll()

    assert result == LldpStats(created=2, neighbors_seen=5).to_dict()
    assert stored == {"lldp_poll": result}


def test_uisp_wrapper_stashes_stats(monkeypatch):
    from contextlib import contextmanager

    from app.tasks import topology_uisp

    stored = {}
    monkeypatch.setattr(
        topology_uisp,
        "store_task_stats",
        lambda task, stats: stored.setdefault(task, stats),
    )
    monkeypatch.setattr(topology_uisp, "uisp_configured", lambda: True)
    monkeypatch.setattr(
        topology_uisp.UispClient, "from_env", classmethod(lambda cls: MagicMock())
    )

    fake_db = MagicMock()

    @contextmanager
    def fake_lock(key, timeout_ms=None):
        yield fake_db, True

    monkeypatch.setattr(topology_uisp.db_session_adapter, "advisory_lock", fake_lock)
    monkeypatch.setattr(
        "app.services.topology.uisp_sync.sync",
        lambda db, client: {"created": 1, "failed": 629},
    )

    result = topology_uisp.run_uisp_topology_sync()

    assert result == {"created": 1, "failed": 629}
    assert stored == {"uisp_sync": {"created": 1, "failed": 629}}
