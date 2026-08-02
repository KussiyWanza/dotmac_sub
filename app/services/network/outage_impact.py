"""Outage impact resolver SOT."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.bulk_actions import membership_scope_token
from app.services.common import coerce_uuid
from app.services.topology.affected import affected_customers, fdh_impact_rows


@dataclass(frozen=True)
class OutageImpact:
    scope_type: str
    scope_id: object
    affected_count: int
    payload: dict

    @property
    def has_customer_impact(self) -> bool:
        return self.affected_count > 0


@dataclass(frozen=True)
class CabinetAudienceMember:
    """One affected subscription behind an FDH, with contact identity."""

    subscription_id: UUID
    subscriber_id: UUID | None
    subscriber_name: str | None
    email: str | None
    service_address: str | None


@dataclass(frozen=True)
class CabinetAudience:
    """The exact, tokenized audience behind one FDH cabinet.

    ``scope_token`` fingerprints the exact subscription-ID membership
    (order-independent SHA-256 via ``membership_scope_token``) — the evidence
    hash consumers compare between preview and execution so a send can never
    ride on a stale audience.
    """

    fdh_id: UUID
    fdh_label: str
    members: tuple[CabinetAudienceMember, ...]
    subscription_ids: tuple[UUID, ...]
    scope_token: str


def resolve_fdh_audience(db: Session, fdh: FdhCabinet | str) -> CabinetAudience:
    """Typed audience for one FDH: exact subscriptions + membership token.

    Built on the same ``fdh_impact_rows`` resolver as the outage-impact page
    and the CRM API, so every surface sees one audience for one cabinet.
    """
    fdh_obj = (
        fdh if isinstance(fdh, FdhCabinet) else db.get(FdhCabinet, coerce_uuid(fdh))
    )
    if fdh_obj is None:
        raise ValueError("fdh cabinet not found")
    rows = fdh_impact_rows(db, fdh_obj)
    members = tuple(
        sorted(
            (
                CabinetAudienceMember(
                    subscription_id=row["subscription_id"],
                    subscriber_id=row.get("subscriber_id"),
                    subscriber_name=row.get("subscriber_name"),
                    email=(row.get("email") or "").strip() or None,
                    service_address=row.get("service_address"),
                )
                for row in rows
            ),
            key=lambda member: str(member.subscription_id),
        )
    )
    subscription_ids = tuple(member.subscription_id for member in members)
    return CabinetAudience(
        fdh_id=fdh_obj.id,
        fdh_label=fdh_obj.code or fdh_obj.name,
        members=members,
        subscription_ids=subscription_ids,
        scope_token=membership_scope_token(
            f"fdh-cabinet:{fdh_obj.id}",
            [str(subscription_id) for subscription_id in subscription_ids],
        ),
    )


def resolve_node_impact(db: Session, node: NetworkDevice | str) -> OutageImpact:
    node_obj = (
        node
        if isinstance(node, NetworkDevice)
        else db.get(NetworkDevice, coerce_uuid(node))
    )
    if node_obj is None:
        raise ValueError("network device not found")
    payload = affected_customers(db, node=node_obj)
    return OutageImpact(
        scope_type="node",
        scope_id=node_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )


def resolve_basestation_impact(db: Session, basestation: PopSite | str) -> OutageImpact:
    basestation_obj = (
        basestation
        if isinstance(basestation, PopSite)
        else db.get(PopSite, coerce_uuid(basestation))
    )
    if basestation_obj is None:
        raise ValueError("basestation not found")
    payload = affected_customers(db, basestation=basestation_obj)
    return OutageImpact(
        scope_type="basestation",
        scope_id=basestation_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )


def resolve_fdh_impact(db: Session, fdh: FdhCabinet | str) -> OutageImpact:
    fdh_obj = (
        fdh if isinstance(fdh, FdhCabinet) else db.get(FdhCabinet, coerce_uuid(fdh))
    )
    if fdh_obj is None:
        raise ValueError("fdh cabinet not found")
    payload = affected_customers(db, fdh=fdh_obj)
    return OutageImpact(
        scope_type="fdh",
        scope_id=fdh_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )
