"""The one service that decides whether we may contact someone.

**Owner of:** "may we send `<category>` to `<address>` on `<channel>`?"

Before this, that question had no owner. Marketing eligibility was decided inside
the campaign segment filter (``comms_campaigns._segment_query``), where opting in
was an *optional checkbox*, and there was no unsubscribe ledger at all. So the
answer depended on who was asking, and a customer who unsubscribed from one
sender stayed reachable by every other.

Every sender now asks this module, and it reads one table
(``communication_suppressions``).

THE DISTINCTION THAT MATTERS
----------------------------
Marketing consent and transactional consent are **not** the same thing, and
collapsing them turns a consent ledger into a billing incident.

An unsubscribe is a refusal of *marketing*. It is not permission to stop sending
someone their invoice, their outage notice, or their service credentials -- we
have a contractual and regulatory duty to send those, and a customer who clicks
"unsubscribe" on a promo has not waived it.

So a suppression carries a scope:

* ``marketing`` -- blocks marketing only. This is what unsubscribe sets.
* ``all``       -- blocks everything, transactional included. Reserved for
                   addresses we genuinely must not send to: hard bounces, spam
                   complaints, legal erasure. **Never** set by a customer
                   clicking unsubscribe.

Every category the platform sends today (billing, account, service, connectivity,
credentials, device, fup, usage, olt, infrastructure) is transactional. Marketing
is the new thing campaigns introduce, so the default for an unknown category is
*transactional* -- fail towards delivering the invoice, not towards silence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import (
    CommunicationSuppression,
    NotificationChannel,
    SuppressionReason,
    SuppressionScope,
)

logger = logging.getLogger(__name__)

#: Categories that are marketing, and therefore stoppable by an unsubscribe.
#: Everything else is transactional and is NOT stopped by one. Keep this list
#: short and explicit: adding a category here makes it suppressible, and getting
#: that wrong the other way (marking `billing` marketing) would silently stop
#: sending invoices.
MARKETING_CATEGORIES: frozenset[str] = frozenset({"marketing", "campaign", "promotion"})

_DIGITS = re.compile(r"\D+")


def is_marketing(category: str | None) -> bool:
    """Transactional unless explicitly marketing.

    Defaulting the other way would mean any new or misspelled category silently
    becomes suppressible -- i.e. a typo could stop someone's invoices.
    """
    return (category or "").strip().lower() in MARKETING_CATEGORIES


def normalize_address(channel: NotificationChannel | str, address: str | None) -> str:
    """Canonical form of a recipient, so a suppression cannot be dodged by case
    or punctuation. ``Foo@Bar.com`` and ``foo@bar.com`` are one address;
    ``+234 801 234 5678`` and ``2348012345678`` are one number.
    """
    value = (address or "").strip()
    if not value:
        return ""
    channel_value = (
        channel.value if isinstance(channel, NotificationChannel) else str(channel)
    )
    if channel_value in {"sms", "whatsapp"}:
        return _DIGITS.sub("", value)
    return value.lower()


def _coerce_channel(channel: NotificationChannel | str) -> NotificationChannel:
    if isinstance(channel, NotificationChannel):
        return channel
    return NotificationChannel(str(channel))


def may_send(
    db: Session,
    *,
    channel: NotificationChannel | str,
    address: str | None,
    category: str | None,
) -> bool:
    """The question. One answer, one table, every sender.

    A ``marketing``-scoped suppression stops marketing and nothing else.
    An ``all``-scoped suppression stops everything.
    """
    return (
        suppression_reason(
            db,
            channel=channel,
            address=address,
            category=category,
        )
        is None
    )


def suppression_reason(
    db: Session,
    *,
    channel: NotificationChannel | str,
    address: str | None,
    category: str | None,
) -> str | None:
    """Return the canonical suppression reason, or ``None`` when sendable."""
    normalized = normalize_address(channel, address)
    if not normalized:
        # No address is a delivery bug, not a consent decision -- let the sender
        # fail loudly on its own terms rather than silently classing it as
        # "suppressed".
        return None

    row = db.scalars(
        select(CommunicationSuppression).where(
            CommunicationSuppression.channel == _coerce_channel(channel),
            CommunicationSuppression.address == normalized,
        )
    ).first()
    if row is None:
        return None
    if row.scope is SuppressionScope.all or is_marketing(category):
        return row.reason.value
    return None


def filter_eligible(
    db: Session,
    *,
    channel: NotificationChannel | str,
    addresses: Iterable[str],
    category: str | None,
) -> list[str]:
    """Bulk form, for audience building. Same rule, one query.

    Campaigns must not hand-roll this: a per-recipient loop calling ``may_send``
    is a different code path that will drift from this one.
    """
    resolved = _coerce_channel(channel)
    wanted = {normalize_address(resolved, a): a for a in addresses if a}
    if not wanted:
        return []

    blocked = suppression_reasons_for_addresses(
        db,
        channel=resolved,
        addresses=wanted.values(),
        category=category,
    )
    return [original for norm, original in wanted.items() if norm not in blocked]


def suppression_reasons_for_addresses(
    db: Session,
    *,
    channel: NotificationChannel | str,
    addresses: Iterable[str],
    category: str | None,
) -> dict[str, str]:
    """Return canonical-address suppression reasons with one database query."""

    resolved = _coerce_channel(channel)
    normalized = {
        normalize_address(resolved, address)
        for address in addresses
        if normalize_address(resolved, address)
    }
    if not normalized:
        return {}

    rows = db.scalars(
        select(CommunicationSuppression).where(
            CommunicationSuppression.channel == resolved,
            CommunicationSuppression.address.in_(normalized),
        )
    ).all()
    marketing = is_marketing(category)
    return {
        row.address: row.reason.value
        for row in rows
        if row.scope is SuppressionScope.all or marketing
    }


def suppress(
    db: Session,
    *,
    channel: NotificationChannel | str,
    address: str,
    scope: SuppressionScope = SuppressionScope.marketing,
    reason: SuppressionReason = SuppressionReason.unsubscribe,
    subscriber_id: UUID | str | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> CommunicationSuppression:
    """Record a suppression. Idempotent on (channel, address).

    Re-suppressing an address *escalates* scope (marketing -> all) but never
    de-escalates: a hard bounce must not be downgraded to a marketing-only block
    by a later unsubscribe click.
    """
    resolved = _coerce_channel(channel)
    normalized = normalize_address(resolved, address)
    if not normalized:
        raise ValueError("Cannot suppress an empty address.")

    existing = db.scalars(
        select(CommunicationSuppression).where(
            CommunicationSuppression.channel == resolved,
            CommunicationSuppression.address == normalized,
        )
    ).first()

    if existing is not None:
        if (
            existing.scope is SuppressionScope.marketing
            and scope is SuppressionScope.all
        ):
            existing.scope = scope
            existing.reason = reason
            existing.note = note or existing.note
            # Persist the escalation. Without this the mutation lives only in
            # the Session; with autoflush off it is silently discarded, the row
            # stays `marketing`, and we resume sending invoices to an address
            # that hard-bounced.
            db.flush()
        return existing

    row = CommunicationSuppression(
        channel=resolved,
        address=normalized,
        raw_address=address,
        subscriber_id=UUID(str(subscriber_id)) if subscriber_id else None,
        scope=scope,
        reason=reason,
        note=note,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def suppress_committed(db: Session, **kwargs) -> CommunicationSuppression:
    row = suppress(db, **kwargs)
    db.commit()
    db.refresh(row)
    return row


def unsuppress(
    db: Session, *, channel: NotificationChannel | str, address: str
) -> bool:
    """Remove a suppression (re-subscribe). True if one was removed."""
    resolved = _coerce_channel(channel)
    normalized = normalize_address(resolved, address)
    row = db.scalars(
        select(CommunicationSuppression).where(
            CommunicationSuppression.channel == resolved,
            CommunicationSuppression.address == normalized,
        )
    ).first()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def unsuppress_committed(
    db: Session, *, channel: NotificationChannel | str, address: str
) -> bool:
    removed = unsuppress(db, channel=channel, address=address)
    db.commit()
    return removed


def unsuppress_marketing(
    db: Session, *, channel: NotificationChannel | str, address: str
) -> bool:
    """Remove only a marketing-scoped suppression.

    Campaign administration is not authority to clear a hard bounce,
    complaint, or erasure row whose ``all`` scope also protects transactional
    delivery. Those rows are managed by the canonical notifications surface.
    """
    resolved = _coerce_channel(channel)
    normalized = normalize_address(resolved, address)
    row = db.scalars(
        select(CommunicationSuppression).where(
            CommunicationSuppression.channel == resolved,
            CommunicationSuppression.address == normalized,
        )
    ).first()
    if row is None or row.scope is not SuppressionScope.marketing:
        return False
    db.delete(row)
    db.flush()
    return True


def unsuppress_marketing_committed(
    db: Session, *, channel: NotificationChannel | str, address: str
) -> bool:
    removed = unsuppress_marketing(db, channel=channel, address=address)
    db.commit()
    return removed


def unsuppress_by_id(db: Session, suppression_id: UUID | str) -> bool:
    """Remove a suppression by its durable identifier."""
    row = db.get(CommunicationSuppression, UUID(str(suppression_id)))
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def unsuppress_by_id_committed(db: Session, suppression_id: UUID | str) -> bool:
    removed = unsuppress_by_id(db, suppression_id)
    db.commit()
    return removed


def list_suppressions(
    db: Session,
    *,
    channel: NotificationChannel | str | None = None,
    scope: SuppressionScope | None = None,
    subscriber_id: UUID | str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CommunicationSuppression]:
    query = select(CommunicationSuppression)
    if channel is not None:
        query = query.where(
            CommunicationSuppression.channel == _coerce_channel(channel)
        )
    if scope is not None:
        query = query.where(CommunicationSuppression.scope == scope)
    if subscriber_id is not None:
        query = query.where(
            CommunicationSuppression.subscriber_id == UUID(str(subscriber_id))
        )
    return list(
        db.scalars(
            query.order_by(CommunicationSuppression.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
