"""Per-customer service availability for a period (agent-facing).

Answers "what availability did this customer actually get?" when they complain
about downtime, using the operator's own measured data rather than an argument.

**Availability is infrastructure-based, not device-based.** A customer's own
ONT/router being off — power cut, unplugged, CPE fault — is not a provider
availability failure, so RADIUS session gaps and ONT-offline time are
deliberately NOT counted. The base number is the availability of the shared
infrastructure serving them (BTS/POP site, OLT, PON port, upstream nodes), which
``AvailabilitySnapshot`` already measures per element per day.

**The exception is an individual provider-fault ticket.** A last-mile fault the
provider owns and was told about — fibre cut to the premises, realignment,
customer link disconnection — is downtime the customer is entitled to count,
evidenced by a support ticket. Its open→resolved span inside the period is added.

Overlapping infrastructure is combined as *worst-per-day*, not summed: if the
OLT and the POP site are both down in the same window the customer is down once,
not twice.

Nothing here decides outage truth or path — ``topology.customer_path`` and the
availability snapshots own that. This composes them for one customer, read-only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.network_monitoring import AvailabilitySnapshot

logger = logging.getLogger(__name__)

#: AvailabilitySnapshot.element_type values we can attribute to a customer.
ELEMENT_DEVICE = "device"
ELEMENT_POP_SITE = "pop_site"
ELEMENT_PON_PORT = "pon_port"


@dataclass
class ServingElement:
    """One infrastructure element whose availability this customer depends on."""

    element_type: str
    element_id: uuid.UUID
    label: str
    role: str
    downtime_seconds: int = 0

    @property
    def trend_url(self) -> str:
        return (
            "/admin/network/performance/trend"
            f"?element_type={self.element_type}&element_id={self.element_id}"
        )


@dataclass
class ProviderFaultTicket:
    """An individual, provider-attributable fault counted against availability."""

    ticket_id: uuid.UUID
    number: str | None
    title: str
    ticket_type: str | None
    opened_at: datetime
    resolved_at: datetime | None
    downtime_seconds: int

    @property
    def url(self) -> str:
        return f"/admin/support/tickets/{self.ticket_id}"


@dataclass
class CustomerAvailability:
    """One customer's availability for a period, with its evidence."""

    period_days: int
    period_start: datetime
    period_end: datetime
    period_seconds: int
    serving_elements: list[ServingElement] = field(default_factory=list)
    infrastructure_downtime_seconds: int = 0
    infrastructure_observed_days: int = 0
    tickets: list[ProviderFaultTicket] = field(default_factory=list)
    ticket_downtime_seconds: int = 0
    path_gap: str | None = None

    @property
    def has_infrastructure_coverage(self) -> bool:
        """Whether every serving element has trustworthy evidence every day.

        Anything less is partial monitoring, so missing days or path elements
        must not be silently counted as uptime.
        """

        return (
            bool(self.serving_elements)
            and self.period_days > 0
            and self.infrastructure_observed_days >= self.period_days
        )

    @property
    def infrastructure_uptime_percent(self) -> float:
        return self._percent(self.infrastructure_downtime_seconds)

    @property
    def effective_downtime_seconds(self) -> int:
        return self.infrastructure_downtime_seconds + self.ticket_downtime_seconds

    @property
    def effective_uptime_percent(self) -> float:
        return self._percent(self.effective_downtime_seconds)

    def _percent(self, downtime: int) -> float:
        if self.period_seconds <= 0:
            return 100.0
        pct = 100.0 * (1.0 - (downtime / self.period_seconds))
        return round(max(0.0, min(100.0, pct)), 3)


def _serving_elements(
    session: Session, subscription
) -> tuple[list[ServingElement], str | None]:
    """Resolve the infrastructure this customer sits behind."""
    from app.services.topology.customer_path import resolve_customer_path

    path = resolve_customer_path(session, subscription)
    elements: list[ServingElement] = []
    seen: set[tuple[str, uuid.UUID]] = set()

    def _add(element_type: str, obj, role: str) -> None:
        if obj is None:
            return
        element_id = getattr(obj, "id", None)
        if element_id is None or (element_type, element_id) in seen:
            return
        seen.add((element_type, element_id))
        label = (
            getattr(obj, "name", None)
            or getattr(obj, "hostname", None)
            or getattr(obj, "label", None)
            or str(element_id)[:8]
        )
        elements.append(
            ServingElement(
                element_type=element_type,
                element_id=element_id,
                label=str(label),
                role=role,
            )
        )

    _add(ELEMENT_POP_SITE, path.basestation, "Base station")
    _add(ELEMENT_PON_PORT, path.pon_port, "PON port")
    if path.access_device_kind in {"olt", "ap"}:
        _add(ELEMENT_DEVICE, path.access_device, f"Access {path.access_device_kind}")
    _add(ELEMENT_DEVICE, path.node, "Access node")
    for hop in path.upstream_chain:
        _add(ELEMENT_DEVICE, hop, "Upstream")

    return elements, path.gap


def _infrastructure_downtime(
    session: Session,
    elements: list[ServingElement],
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """Worst-per-day downtime across the serving elements, summed.

    Snapshots are daily aggregates, so per-day overlap between two elements
    cannot be resolved exactly; taking the worst element per day is the honest
    combination — a shared outage counts once, not once per element.
    """
    if not elements:
        return 0, 0

    keys = [(e.element_type, e.element_id) for e in elements]
    rows = (
        session.query(AvailabilitySnapshot)
        .filter(AvailabilitySnapshot.snapshot_date >= start)
        .filter(AvailabilitySnapshot.snapshot_date < end)
        .all()
    )
    wanted = set(keys)
    by_day: dict[object, int] = {}
    observed_keys_by_day: dict[object, set[tuple[str, uuid.UUID]]] = {}
    per_element: dict[tuple[str, uuid.UUID], int] = {}

    for row in rows:
        key = (row.element_type, row.element_id)
        if key not in wanted:
            continue
        if row.uptime_percent is None:
            continue
        downtime = int(row.downtime_seconds or 0)
        day = (
            row.snapshot_date.date()
            if hasattr(row.snapshot_date, "date")
            else row.snapshot_date
        )
        by_day[day] = max(by_day.get(day, 0), downtime)
        observed_keys_by_day.setdefault(day, set()).add(key)
        per_element[key] = per_element.get(key, 0) + downtime

    for element in elements:
        element.downtime_seconds = per_element.get(
            (element.element_type, element.element_id), 0
        )

    fully_observed_days = sum(
        1 for observed_keys in observed_keys_by_day.values() if wanted <= observed_keys
    )
    return sum(by_day.values()), fully_observed_days


def _provider_fault_tickets(
    session: Session,
    subscriber_id,
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[ProviderFaultTicket], int]:
    """Individual provider-attributable faults overlapping the period."""
    from app.models.support import Ticket
    from app.services.customer_service_state import is_infrastructure_down_ticket

    if subscriber_id is None:
        return [], 0

    rows = (
        session.query(Ticket)
        .filter(Ticket.subscriber_id == subscriber_id)
        .filter(Ticket.created_at <= end)
        .all()
    )

    tickets: list[ProviderFaultTicket] = []
    total = 0
    for row in rows:
        if not is_infrastructure_down_ticket(row):
            continue
        opened = _as_utc(row.created_at)
        if opened is None:
            continue
        closed = _as_utc(row.resolved_at or row.closed_at) or end
        if closed <= start:
            continue
        span_start = max(opened, start)
        span_end = min(closed, end)
        seconds = max(0, int((span_end - span_start).total_seconds()))
        if seconds <= 0:
            continue
        tickets.append(
            ProviderFaultTicket(
                ticket_id=row.id,
                number=row.number,
                title=row.title,
                ticket_type=row.ticket_type,
                opened_at=opened,
                resolved_at=_as_utc(row.resolved_at or row.closed_at),
                downtime_seconds=seconds,
            )
        )
        total += seconds

    tickets.sort(key=lambda t: t.opened_at, reverse=True)
    return tickets, total


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def customer_availability(
    session: Session,
    subscription,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> CustomerAvailability:
    """Availability this customer received over the trailing ``days``.

    Read-only and non-raising at the path layer: an unresolvable topology path
    yields a report with no infrastructure coverage rather than an error, so the
    agent still sees the ticket evidence.
    """
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)

    try:
        elements, gap = _serving_elements(session, subscription)
    except Exception:
        logger.exception(
            "Customer availability: path resolution failed for subscription %s",
            getattr(subscription, "id", None),
        )
        elements, gap = [], "path resolution failed"

    infra_downtime, observed_days = _infrastructure_downtime(
        session, elements, start=start, end=end
    )
    tickets, ticket_downtime = _provider_fault_tickets(
        session,
        getattr(subscription, "subscriber_id", None),
        start=start,
        end=end,
    )

    return CustomerAvailability(
        period_days=days,
        period_start=start,
        period_end=end,
        period_seconds=int((end - start).total_seconds()),
        serving_elements=elements,
        infrastructure_downtime_seconds=infra_downtime,
        infrastructure_observed_days=observed_days,
        tickets=tickets,
        ticket_downtime_seconds=ticket_downtime,
        path_gap=gap,
    )
