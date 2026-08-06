"""Per-customer availability: infrastructure-based, tickets as the exception."""

import uuid
from datetime import UTC, datetime, timedelta

from app.services.topology import customer_availability as ca

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


class _Snap:
    def __init__(
        self, element_type, element_id, day, downtime, *, uptime_percent=100.0
    ):
        self.element_type = element_type
        self.element_id = element_id
        self.snapshot_date = day
        self.downtime_seconds = downtime
        self.uptime_percent = uptime_percent


class _Ticket:
    def __init__(self, *, title, created_at, resolved_at=None, ticket_type=None):
        self.id = uuid.uuid4()
        self.number = "TKT-1"
        self.title = title
        self.ticket_type = ticket_type
        self.created_at = created_at
        self.resolved_at = resolved_at
        self.closed_at = None


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, snaps=(), tickets=()):
        self._snaps = list(snaps)
        self._tickets = list(tickets)

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _Query(self._tickets if name == "Ticket" else self._snaps)


class _Sub:
    def __init__(self):
        self.id = uuid.uuid4()
        self.subscriber_id = uuid.uuid4()


def _elements(monkeypatch, elements, gap=None):
    monkeypatch.setattr(ca, "_serving_elements", lambda s, sub: (elements, gap))


def _pop(label="BTS Apo"):
    return ca.ServingElement("pop_site", uuid.uuid4(), label, "Base station")


# --- infrastructure is the base -------------------------------------------


def test_uptime_is_computed_from_serving_infrastructure(monkeypatch):
    el = _pop()
    _elements(monkeypatch, [el])
    day = NOW - timedelta(hours=12)
    session = _Session(snaps=[_Snap("pop_site", el.element_id, day, 3600)])

    report = ca.customer_availability(session, _Sub(), days=1, now=NOW)

    assert report.infrastructure_downtime_seconds == 3600
    assert report.infrastructure_uptime_percent < 100.0
    assert report.has_infrastructure_coverage is True


def test_overlapping_elements_count_once_per_day(monkeypatch):
    """A shared outage must not be double-counted across OLT and POP site."""
    pop = _pop()
    dev = ca.ServingElement("device", uuid.uuid4(), "OLT-1", "Access olt")
    _elements(monkeypatch, [pop, dev])
    day = NOW - timedelta(days=1)
    session = _Session(
        snaps=[
            _Snap("pop_site", pop.element_id, day, 3600),
            _Snap("device", dev.element_id, day, 1800),
        ]
    )

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)

    # worst-per-day, not 3600+1800
    assert report.infrastructure_downtime_seconds == 3600


def test_no_resolvable_path_is_reported_not_claimed_as_100(monkeypatch):
    _elements(monkeypatch, [], gap="no access device")
    report = ca.customer_availability(_Session(), _Sub(), days=30, now=NOW)

    assert report.has_infrastructure_coverage is False
    assert report.path_gap == "no access device"


def test_path_failure_still_returns_a_report(monkeypatch):
    def _boom(session, sub):
        raise RuntimeError("topology exploded")

    monkeypatch.setattr(ca, "_serving_elements", _boom)
    report = ca.customer_availability(_Session(), _Sub(), days=30, now=NOW)
    assert report.has_infrastructure_coverage is False


# --- individual provider-fault tickets are the exception -------------------


def test_provider_fault_ticket_adds_downtime(monkeypatch):
    _elements(monkeypatch, [])
    opened = NOW - timedelta(days=3)
    resolved = opened + timedelta(hours=5)
    session = _Session(
        tickets=[
            _Ticket(
                title="Customer link disconnection",
                created_at=opened,
                resolved_at=resolved,
            )
        ]
    )

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)

    assert report.ticket_downtime_seconds == 5 * 3600
    assert len(report.tickets) == 1


def test_non_provider_fault_ticket_is_not_counted(monkeypatch):
    _elements(monkeypatch, [])
    opened = NOW - timedelta(days=2)
    session = _Session(
        tickets=[_Ticket(title="Please change my WiFi password", created_at=opened)]
    )

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)
    assert report.ticket_downtime_seconds == 0
    assert report.tickets == []


def test_open_ticket_counts_up_to_now(monkeypatch):
    _elements(monkeypatch, [])
    opened = NOW - timedelta(hours=4)
    session = _Session(tickets=[_Ticket(title="Fiber cut", created_at=opened)])

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)
    assert report.ticket_downtime_seconds == 4 * 3600


def test_ticket_downtime_is_clipped_to_the_period(monkeypatch):
    _elements(monkeypatch, [])
    opened = NOW - timedelta(days=40)  # before the 30d window
    resolved = NOW - timedelta(days=29)  # 1 day inside it
    session = _Session(
        tickets=[_Ticket(title="No internet", created_at=opened, resolved_at=resolved)]
    )

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)
    assert 0 < report.ticket_downtime_seconds <= 86400


# --- effective number ------------------------------------------------------


def test_effective_combines_infrastructure_and_tickets(monkeypatch):
    el = _pop()
    _elements(monkeypatch, [el])
    day = NOW - timedelta(days=2)
    opened = NOW - timedelta(days=3)
    session = _Session(
        snaps=[_Snap("pop_site", el.element_id, day, 3600)],
        tickets=[
            _Ticket(
                title="Fiber cut",
                created_at=opened,
                resolved_at=opened + timedelta(hours=1),
            )
        ],
    )

    report = ca.customer_availability(session, _Sub(), days=30, now=NOW)

    assert report.effective_downtime_seconds == 7200
    assert report.effective_uptime_percent < report.infrastructure_uptime_percent


def test_perfect_period_is_100_percent(monkeypatch):
    _elements(monkeypatch, [_pop()])
    report = ca.customer_availability(_Session(), _Sub(), days=30, now=NOW)
    assert report.infrastructure_uptime_percent == 100.0
    assert report.effective_uptime_percent == 100.0


def test_resolved_path_without_any_snapshots_is_not_coverage(monkeypatch):
    _elements(monkeypatch, [_pop()])

    report = ca.customer_availability(_Session(), _Sub(), days=30, now=NOW)

    assert report.infrastructure_observed_days == 0
    assert report.has_infrastructure_coverage is False


def test_partial_path_or_partial_period_never_becomes_uptime(monkeypatch):
    pop = _pop()
    dev = ca.ServingElement("device", uuid.uuid4(), "OLT-1", "Access olt")
    _elements(monkeypatch, [pop, dev])
    session = _Session(
        snaps=[_Snap("pop_site", pop.element_id, NOW - timedelta(hours=12), 0)]
    )

    report = ca.customer_availability(session, _Sub(), days=1, now=NOW)

    assert report.infrastructure_observed_days == 0
    assert report.has_infrastructure_coverage is False


# --- the model: device/session downtime is NOT availability ----------------


def test_customer_device_and_session_downtime_are_never_counted():
    """Availability is infrastructure-based. A customer's own ONT/router being
    off is not a provider failure, so session/CPE state must not be a source:
    the aggregator reads availability snapshots and support tickets only."""
    import inspect

    source = inspect.getsource(ca)

    assert "AvailabilitySnapshot" in source
    for forbidden in (
        "RadiusAccountingSession",
        "RadiusActiveSession",
        "radius_accounting_sessions",
        "OntUnit",
    ):
        assert forbidden not in source, f"{forbidden} must not feed availability"
