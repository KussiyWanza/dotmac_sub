"""What has been sold behind a shared segment, against what it can carry.

Owner of the question "does this segment have room". It owns **no storage**:
capacity is a fact about hardware and already belongs to the thing that has it.

    PON port        pon_ports.downstream_mbps / upstream_mbps
    interface       device_interfaces.speed_mbps
    link / uplink   network_topology_links.capacity_bps

This resolver reads whichever applies and applies planning policy on top. An
earlier draft stored its own copy of those figures in a ``capacity_domains``
table; that was a second authority over facts that already had one, and would
have drifted from them.

Three rules shape the verdicts.

**Committed capacity is not sold capacity.** A best-effort tier sells a
ceiling; a dedicated tier with a CIR *reserves* its rate. Reserved bandwidth is
unavailable to anyone else whether or not it is in use, so it is checked
against physical capacity first and yields ``overcommitted`` — which no
oversubscription allowance can rescue.

**An unmeasured segment returns UNKNOWN, never OK.** The check exists to stop
overselling; a missing capacity figure is exactly the state in which
overselling hides, so it must not read as a pass.

**Sold is not used.** Everything here derives from what was sold. Measured
utilisation lives in ``bandwidth_samples`` and is a better input where coverage
allows; this module deliberately does not conflate the two.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.catalog import (
    CatalogOffer,
    GuaranteedSpeedType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OntAssignment, PonPort
from app.services.domain_errors import DomainError

#: Fallback when a port carries no explicit target. Deliberately 1:1 — the
#: safest assumption for a segment nobody has made a planning decision about is
#: that it may not be oversold at all.
DEFAULT_OVERSUBSCRIPTION = Decimal("1")

#: Sold above this share of the allowance flags a segment before it breaches.
#: A check that only fires after the fact is a report, not a control.
_AT_RISK_SHARE = Decimal("0.85")


class CapacityError(DomainError):
    """Unusable capacity input (adapter: HTTP 400)."""


class CapacityVerdict(enum.StrEnum):
    """Whether a segment can take more."""

    ok = "ok"
    at_risk = "at_risk"
    oversubscribed = "oversubscribed"
    #: Committed rates alone exceed physical capacity. Distinct from
    #: oversubscribed because no amount of statistical multiplexing helps: the
    #: promises cannot all be kept simultaneously.
    overcommitted = "overcommitted"
    #: Never silently treated as ok — an unmeasured segment is where
    #: overselling hides.
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class SegmentUsage:
    """One segment's position. Immutable: it is evidence for a sales decision."""

    segment_id: object
    segment_name: str
    #: None until surveyed — an unmeasured segment, not one with no capacity.
    downstream_mbps: int | None
    upstream_mbps: int | None
    target_oversubscription: Decimal
    subscriber_count: int
    #: Sum of peak (ceiling) rates sold behind this segment.
    sold_downstream_mbps: int
    sold_upstream_mbps: int
    #: Sum of rates that are RESERVED, not merely allowed.
    committed_downstream_mbps: int
    committed_upstream_mbps: int
    verdict: CapacityVerdict

    @property
    def sellable_downstream_mbps(self) -> Decimal | None:
        """Capacity times the allowance — the budget sold peaks draw on.

        ``None`` on an unsurveyed segment. Zero would read as "full" and a
        guess as room that may not exist; neither is the truth, which is that
        nobody has measured it.
        """
        if not self.downstream_mbps:
            return None
        return Decimal(self.downstream_mbps) * self.target_oversubscription

    @property
    def headroom_downstream_mbps(self) -> Decimal | None:
        sellable = self.sellable_downstream_mbps
        if sellable is None:
            return None
        return sellable - Decimal(self.sold_downstream_mbps)

    @property
    def committed_share(self) -> Decimal | None:
        """Reserved share of physical capacity. Above 1 nothing can save it."""
        if not self.downstream_mbps:
            return None
        return Decimal(self.committed_downstream_mbps) / Decimal(self.downstream_mbps)


def committed_for(offer: CatalogOffer) -> tuple[int, int]:
    """(upstream, downstream) Mbps this offer RESERVES, not merely allows."""
    if offer.guaranteed_speed is GuaranteedSpeedType.fixed:
        floor = offer.guaranteed_speed_limit_at
        if not floor:
            return 0, 0
        return (
            min(int(floor), offer.speed_upload_mbps or 0),
            min(int(floor), offer.speed_download_mbps or 0),
        )
    if offer.guaranteed_speed is GuaranteedSpeedType.relative:
        percent = offer.guaranteed_speed_limit_at
        if not percent:
            return 0, 0
        percent = max(0, min(int(percent), 100))
        return (
            (offer.speed_upload_mbps or 0) * percent // 100,
            (offer.speed_download_mbps or 0) * percent // 100,
        )
    return 0, 0


def verdict_for(
    *,
    downstream_mbps: int | None,
    sold_downstream_mbps: int,
    committed_downstream_mbps: int,
    target_oversubscription: Decimal,
) -> CapacityVerdict:
    # An unsurveyed segment cannot be judged. Returning ok here would make the
    # ports nobody has measured look like the safest on the network.
    if not downstream_mbps:
        return CapacityVerdict.unknown
    # Checked before oversubscription: committed rates exceeding physical
    # capacity cannot be rescued by an allowance, because they are reserved
    # whether or not anyone is transmitting.
    if committed_downstream_mbps > downstream_mbps:
        return CapacityVerdict.overcommitted
    sellable = Decimal(downstream_mbps) * target_oversubscription
    if not sellable:
        return CapacityVerdict.unknown
    sold = Decimal(sold_downstream_mbps)
    if sold > sellable:
        return CapacityVerdict.oversubscribed
    if sold >= sellable * _AT_RISK_SHARE:
        return CapacityVerdict.at_risk
    return CapacityVerdict.ok


def _offers_by_pon_port(db: Session) -> dict[object, list[CatalogOffer]]:
    """PON port id -> the offers of the active subscriptions behind it.

    ONT assignment -> subscriber -> active subscription is the only populated
    route from a customer to a shared access segment today.
    """
    rows = (
        db.query(OntAssignment.pon_port_id, CatalogOffer)
        .join(Subscription, Subscription.subscriber_id == OntAssignment.subscriber_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .filter(
            OntAssignment.pon_port_id.isnot(None),
            OntAssignment.active.is_(True),
            Subscription.status == SubscriptionStatus.active,
        )
        .all()
    )
    grouped: dict[object, list[CatalogOffer]] = {}
    for pon_port_id, offer in rows:
        grouped.setdefault(pon_port_id, []).append(offer)
    return grouped


def pon_port_usage(db: Session) -> list[SegmentUsage]:
    """Every active PON port with what is sold behind it.

    Capacity is read from the port itself. Ports nobody has surveyed appear
    with an ``unknown`` verdict rather than being omitted — the survey backlog
    is part of the answer, not noise to filter out.
    """
    by_port = _offers_by_pon_port(db)
    usages: list[SegmentUsage] = []

    ports = (
        db.query(PonPort)
        .filter(PonPort.is_active.is_(True))
        .order_by(PonPort.name)
        .all()
    )
    for port in ports:
        offers = by_port.get(port.id, [])
        sold_down = sum(offer.speed_download_mbps or 0 for offer in offers)
        sold_up = sum(offer.speed_upload_mbps or 0 for offer in offers)
        committed = [committed_for(offer) for offer in offers]
        committed_up = sum(item[0] for item in committed)
        committed_down = sum(item[1] for item in committed)
        target = (
            Decimal(str(port.target_oversubscription))
            if port.target_oversubscription is not None
            else DEFAULT_OVERSUBSCRIPTION
        )

        usages.append(
            SegmentUsage(
                segment_id=port.id,
                segment_name=port.name,
                downstream_mbps=port.downstream_mbps,
                upstream_mbps=port.upstream_mbps,
                target_oversubscription=target,
                subscriber_count=len(offers),
                sold_downstream_mbps=sold_down,
                sold_upstream_mbps=sold_up,
                committed_downstream_mbps=committed_down,
                committed_upstream_mbps=committed_up,
                verdict=verdict_for(
                    downstream_mbps=port.downstream_mbps,
                    sold_downstream_mbps=sold_down,
                    committed_downstream_mbps=committed_down,
                    target_oversubscription=target,
                ),
            )
        )
    return usages


def can_accept(
    usage: SegmentUsage, offer: CatalogOffer
) -> tuple[bool, CapacityVerdict, str]:
    """Would adding ``offer`` to this segment still be within budget?

    Returns the verdict the segment WOULD have, so a service order can record
    the finding rather than only a yes/no. An unknown segment refuses: the
    order should carry "capacity not established", not a silent pass.
    """
    if usage.verdict is CapacityVerdict.unknown:
        return False, CapacityVerdict.unknown, "segment capacity is not established"

    _committed_up, committed_down = committed_for(offer)
    projected = verdict_for(
        downstream_mbps=usage.downstream_mbps,
        sold_downstream_mbps=usage.sold_downstream_mbps
        + (offer.speed_download_mbps or 0),
        committed_downstream_mbps=usage.committed_downstream_mbps + committed_down,
        target_oversubscription=usage.target_oversubscription,
    )
    if projected is CapacityVerdict.overcommitted:
        return (
            False,
            projected,
            "reserved rates would exceed the segment's physical capacity",
        )
    if projected is CapacityVerdict.oversubscribed:
        return False, projected, "sold capacity would exceed the planning target"
    return True, projected, "within the planning target"
