"""Typed catalogue-backed IPv4 block choices for service configuration."""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    CatalogOffer,
    OfferStatus,
    Subscription,
    SubscriptionStatus,
)


class IpBlockPrefix(StrEnum):
    p32 = "/32"
    p30 = "/30"
    p29 = "/29"
    p28 = "/28"
    p27 = "/27"
    p26 = "/26"
    p25 = "/25"
    p24 = "/24"

    @property
    def prefix_length(self) -> int:
        return int(self.value.removeprefix("/"))

    @property
    def subnet_mask(self) -> str:
        return str(ipaddress.IPv4Network((0, self.prefix_length)).netmask)

    @property
    def address_count(self) -> int:
        return 1 << (32 - self.prefix_length)

    @classmethod
    def from_mask(cls, value: str | None) -> IpBlockPrefix | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("/"):
            try:
                return cls(text)
            except ValueError:
                return None
        if "/" in text:
            try:
                return cls(f"/{ipaddress.IPv4Network(text, strict=False).prefixlen}")
            except ValueError:
                return None
        try:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{text}").prefixlen
            return cls(f"/{prefix}")
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class CatalogIpBlockChoice:
    prefix: IpBlockPrefix
    subnet_mask: str
    address_count: int
    offer_ids: tuple[uuid.UUID, ...]
    offer_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubscriberIpBlockEntitlement:
    subscription_id: uuid.UUID
    offer_id: uuid.UUID
    offer_name: str
    prefix: IpBlockPrefix


def offer_plan_metadata(description: str | None) -> dict[str, str | None]:
    """Interpret the plan markers owned by the catalogue policy boundary."""

    metadata: dict[str, str | None] = {"plan_kind": None, "ip_block_size": None}
    for line in str(description or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("[plan_kind:") and lowered.endswith("]"):
            metadata["plan_kind"] = stripped[11:-1].strip() or None
        if lowered.startswith("[ip_block_size:") and lowered.endswith("]"):
            metadata["ip_block_size"] = stripped[15:-1].strip() or None
    return metadata


def active_catalog_ip_block_choices(db: Session) -> tuple[CatalogIpBlockChoice, ...]:
    """Return de-duplicated IP block sizes represented by active offers."""

    offers = tuple(
        db.scalars(
            select(CatalogOffer)
            .where(
                CatalogOffer.is_active.is_(True),
                CatalogOffer.status == OfferStatus.active,
            )
            .order_by(CatalogOffer.name, CatalogOffer.id)
        )
    )
    grouped: dict[IpBlockPrefix, list[CatalogOffer]] = {}
    for offer in offers:
        metadata = offer_plan_metadata(offer.description)
        if str(metadata["plan_kind"] or "").strip().lower() != "ip_address":
            continue
        try:
            prefix = IpBlockPrefix(str(metadata["ip_block_size"] or ""))
        except ValueError:
            continue
        grouped.setdefault(prefix, []).append(offer)

    return tuple(
        CatalogIpBlockChoice(
            prefix=prefix,
            subnet_mask=prefix.subnet_mask,
            address_count=prefix.address_count,
            offer_ids=tuple(offer.id for offer in grouped[prefix]),
            offer_names=tuple(offer.name for offer in grouped[prefix]),
        )
        for prefix in IpBlockPrefix
        if prefix in grouped
    )


def subscriber_ip_block_entitlements(
    db: Session, subscriber_id: uuid.UUID
) -> tuple[SubscriberIpBlockEntitlement, ...]:
    """Return the subscriber's active catalogue IP-block subscriptions."""

    rows = tuple(
        db.execute(
            select(Subscription, CatalogOffer)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.status == SubscriptionStatus.active,
                CatalogOffer.is_active.is_(True),
                CatalogOffer.status == OfferStatus.active,
            )
            .order_by(Subscription.created_at, Subscription.id)
        )
    )
    entitlements: list[SubscriberIpBlockEntitlement] = []
    for subscription, offer in rows:
        metadata = offer_plan_metadata(offer.description)
        if str(metadata["plan_kind"] or "").strip().lower() != "ip_address":
            continue
        try:
            prefix = IpBlockPrefix(str(metadata["ip_block_size"] or ""))
        except ValueError:
            continue
        entitlements.append(
            SubscriberIpBlockEntitlement(
                subscription_id=subscription.id,
                offer_id=offer.id,
                offer_name=offer.name,
                prefix=prefix,
            )
        )
    return tuple(entitlements)


__all__ = [
    "CatalogIpBlockChoice",
    "IpBlockPrefix",
    "SubscriberIpBlockEntitlement",
    "active_catalog_ip_block_choices",
    "offer_plan_metadata",
    "subscriber_ip_block_entitlements",
]
