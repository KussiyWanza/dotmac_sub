from __future__ import annotations

from dataclasses import dataclass
from email.utils import getaddresses
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationTeam,
    InboxTeamRole,
    InboxTeamSource,
    TeamInboxEmailRoute,
)
from app.services import team_outbound


@dataclass(frozen=True)
class EmailTeamRecipientMatch:
    service_team_id: str
    email_address: str
    recipient_kind: str
    is_primary_route: bool
    priority: int
    metadata: dict


@dataclass(frozen=True)
class EmailTeamRoutingPlan:
    primary_service_team_id: str | None
    participant_service_team_ids: list[str]
    matches: list[EmailTeamRecipientMatch]
    unmatched_recipients: list[str]


def normalize_email_address(value: str | None) -> str | None:
    if not value:
        return None
    parsed = getaddresses([value])
    address = parsed[0][1] if parsed else value
    normalized = address.strip().lower()
    return normalized or None


def normalize_email_addresses(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for _name, address in getaddresses([str(raw)]):
            value = normalize_email_address(address)
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
    return normalized


def _coerce_uuid(value: str | UUID | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def owned_mailbox_addresses(db: Session) -> frozenset[str]:
    """Every normalized address that belongs to us rather than to a customer.

    The routing table is the register of our mailboxes, so this owner answers
    "is this one of ours?" — a participant projection must not admit our own
    support address as a party to the conversation, and neither should any
    later reply-to or recipient rule.

    Includes deactivated routes deliberately: a mailbox we have retired is
    still not a customer, and old messages carry it in their headers.
    """
    routed = {
        str(address).strip().lower()
        for (address,) in db.query(TeamInboxEmailRoute.email_address).all()
        if str(address or "").strip()
    }
    from app.config import settings

    configured = {
        normalized
        for normalized in (
            normalize_email_address(value)
            for value in settings.team_inbox_smtp_inbound_recipients.split(",")
        )
        if normalized
    }
    probe = normalize_email_address(settings.team_inbox_smtp_probe_recipient)
    if probe:
        configured.add(probe)
    return frozenset(routed | configured)


def default_service_team_id(db: Session) -> str | None:
    """Owning team for inbound traffic that carries no address to route on.

    Email routes on the recipient mailbox; WhatsApp and the Meta social
    channels have no equivalent, so without a declared default they arrive
    owned by nobody. This reads the one setting and confirms the team is still
    active — it does not invent a second routing authority beside
    ``TeamInboxEmailRoute``.
    """
    from app.config import settings
    from app.models.service_team import ServiceTeam

    configured = _coerce_uuid(
        settings.team_inbox_channel_fallback_service_team_id or None
    )
    if configured is None:
        return None
    team = db.get(ServiceTeam, UUID(configured))
    if team is None or not team.is_active:
        return None
    return configured


def build_email_team_routing_plan(
    db: Session,
    *,
    to_addresses: list[str] | tuple[str, ...] | None = None,
    cc_addresses: list[str] | tuple[str, ...] | None = None,
    fallback_service_team_id: str | UUID | None = None,
) -> EmailTeamRoutingPlan:
    to_normalized = normalize_email_addresses(to_addresses)
    cc_normalized = normalize_email_addresses(cc_addresses)
    all_recipients = list(dict.fromkeys([*to_normalized, *cc_normalized]))
    fallback_team_id = _coerce_uuid(fallback_service_team_id)

    if not all_recipients:
        return EmailTeamRoutingPlan(
            primary_service_team_id=fallback_team_id,
            participant_service_team_ids=[fallback_team_id] if fallback_team_id else [],
            matches=[],
            unmatched_recipients=[],
        )

    routes = (
        db.query(TeamInboxEmailRoute)
        .filter(TeamInboxEmailRoute.is_active.is_(True))
        .filter(TeamInboxEmailRoute.email_address.in_(all_recipients))
        .all()
    )

    matched_addresses = {route.email_address for route in routes}
    matches = [
        EmailTeamRecipientMatch(
            service_team_id=str(route.service_team_id),
            email_address=route.email_address,
            recipient_kind="to" if route.email_address in to_normalized else "cc",
            is_primary_route=route.is_primary,
            priority=route.priority,
            metadata=route.metadata_ if isinstance(route.metadata_, dict) else {},
        )
        for route in routes
    ]
    matches.sort(
        key=lambda match: (
            0 if match.recipient_kind == "to" else 1,
            match.priority,
            0 if match.is_primary_route else 1,
            match.email_address,
        )
    )

    participant_ids: list[str] = []
    for match in matches:
        if match.service_team_id not in participant_ids:
            participant_ids.append(match.service_team_id)
    if fallback_team_id and fallback_team_id not in participant_ids:
        participant_ids.append(fallback_team_id)

    primary_team_id = matches[0].service_team_id if matches else fallback_team_id
    return EmailTeamRoutingPlan(
        primary_service_team_id=primary_team_id,
        participant_service_team_ids=participant_ids,
        matches=matches,
        unmatched_recipients=[
            address for address in all_recipients if address not in matched_addresses
        ],
    )


def apply_email_routing_plan(
    db: Session,
    *,
    conversation: InboxConversation,
    plan: EmailTeamRoutingPlan,
) -> InboxConversation:
    primary_team_id = (
        str(conversation.primary_service_team_id)
        if conversation.primary_service_team_id is not None
        else _coerce_uuid(plan.primary_service_team_id)
    )
    if primary_team_id:
        conversation.primary_service_team_id = UUID(primary_team_id)

    for team_id in plan.participant_service_team_ids:
        normalized_team_id = _coerce_uuid(team_id)
        if not normalized_team_id:
            continue
        team_uuid = UUID(normalized_team_id)
        role = (
            InboxTeamRole.owner.value
            if normalized_team_id == primary_team_id
            else InboxTeamRole.participant.value
        )
        match = next(
            (
                item
                for item in plan.matches
                if item.service_team_id == normalized_team_id
            ),
            None,
        )
        source = (
            InboxTeamSource.recipient_to.value
            if match and match.recipient_kind == "to"
            else InboxTeamSource.recipient_cc.value
            if match and match.recipient_kind == "cc"
            else InboxTeamSource.routing_rule.value
        )
        metadata = dict(match.metadata) if match and match.metadata else {}
        if match:
            metadata["route_email_address"] = match.email_address
            metadata["route_recipient_kind"] = match.recipient_kind
        metadata = _outbound_metadata(metadata)
        link = (
            db.query(InboxConversationTeam)
            .filter(InboxConversationTeam.conversation_id == conversation.id)
            .filter(InboxConversationTeam.service_team_id == team_uuid)
            .first()
        )
        if link is None:
            db.add(
                InboxConversationTeam(
                    conversation_id=conversation.id,
                    service_team_id=team_uuid,
                    role=role,
                    source=source,
                    is_active=True,
                    metadata_=metadata or None,
                )
            )
            continue
        link.role = role
        link.source = source
        link.is_active = True
        if metadata:
            link.metadata_ = {**(link.metadata_ or {}), **metadata}
    db.flush()
    return conversation


def _outbound_metadata(metadata: dict) -> dict:
    allowed = {
        team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY,
        team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY,
        "route_email_address",
        "route_recipient_kind",
        *team_outbound.LEGACY_EMAIL_SENDER_METADATA_KEYS,
    }
    return {key: value for key, value in metadata.items() if key in allowed and value}


class EmailRouteError(ValueError):
    """Rejected email-route change, safe for an admin adapter to render."""


@dataclass(frozen=True)
class EmailRouteRow:
    id: str
    service_team_id: str
    service_team_name: str | None
    email_address: str
    is_primary: bool
    priority: int
    is_active: bool


def list_email_routes(
    db: Session, *, include_inactive: bool = True
) -> list[EmailRouteRow]:
    """Every configured inbound mailbox, newest team grouping first.

    This table decides which service team owns an inbound email. It had a model
    and a consumer (``build_email_team_routing_plan``) but no API, so the only
    way to populate it was a direct INSERT — which is why production carried no
    rows against live mailboxes.
    """
    from app.models.service_team import ServiceTeam

    query = (
        db.query(TeamInboxEmailRoute, ServiceTeam)
        .outerjoin(ServiceTeam, ServiceTeam.id == TeamInboxEmailRoute.service_team_id)
        .order_by(
            TeamInboxEmailRoute.priority.asc(),
            TeamInboxEmailRoute.email_address.asc(),
        )
    )
    if not include_inactive:
        query = query.filter(TeamInboxEmailRoute.is_active.is_(True))
    return [
        EmailRouteRow(
            id=str(route.id),
            service_team_id=str(route.service_team_id),
            service_team_name=team.name if team is not None else None,
            email_address=route.email_address,
            is_primary=bool(route.is_primary),
            priority=int(route.priority),
            is_active=bool(route.is_active),
        )
        for route, team in query.all()
    ]


def _demote_other_primaries(
    db: Session, *, service_team_id: UUID, keep_id: UUID | None = None
) -> None:
    """One primary per team: a second primary would make routing ambiguous."""
    query = (
        db.query(TeamInboxEmailRoute)
        .filter(TeamInboxEmailRoute.service_team_id == service_team_id)
        .filter(TeamInboxEmailRoute.is_primary.is_(True))
    )
    if keep_id is not None:
        query = query.filter(TeamInboxEmailRoute.id != keep_id)
    for row in query.all():
        row.is_primary = False


def create_email_route(
    db: Session,
    *,
    service_team_id: str | UUID,
    email_address: str,
    is_primary: bool = False,
    priority: int = 100,
) -> TeamInboxEmailRoute:
    from app.models.service_team import ServiceTeam

    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        raise EmailRouteError("Choose a service team for this mailbox.")
    normalized = normalize_email_address(email_address)
    if not normalized:
        raise EmailRouteError("Enter a valid mailbox address.")

    team = db.get(ServiceTeam, UUID(team_uuid))
    if team is None or not team.is_active:
        raise EmailRouteError("Service team not found.")

    existing = (
        db.query(TeamInboxEmailRoute)
        .filter(TeamInboxEmailRoute.service_team_id == UUID(team_uuid))
        .filter(TeamInboxEmailRoute.email_address == normalized)
        .one_or_none()
    )
    if existing is not None:
        raise EmailRouteError(f"{normalized} is already routed to this team.")

    if is_primary:
        _demote_other_primaries(db, service_team_id=UUID(team_uuid))

    route = TeamInboxEmailRoute(
        service_team_id=UUID(team_uuid),
        email_address=normalized,
        is_primary=bool(is_primary),
        priority=int(priority),
        is_active=True,
    )
    db.add(route)
    db.flush()
    return route


def update_email_route(
    db: Session,
    route_id: str | UUID,
    *,
    is_primary: bool | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> TeamInboxEmailRoute:
    route_uuid = _coerce_uuid(route_id)
    route = db.get(TeamInboxEmailRoute, UUID(route_uuid)) if route_uuid else None
    if route is None:
        raise EmailRouteError("Email route not found.")

    if priority is not None:
        route.priority = int(priority)
    if is_active is not None:
        route.is_active = bool(is_active)
        # A deactivated mailbox cannot remain the team's primary route.
        if not route.is_active:
            route.is_primary = False
    if is_primary is not None and is_primary and route.is_active:
        _demote_other_primaries(
            db, service_team_id=route.service_team_id, keep_id=route.id
        )
        route.is_primary = True
    elif is_primary is not None and not is_primary:
        route.is_primary = False
    db.flush()
    return route


def delete_email_route(db: Session, route_id: str | UUID) -> None:
    """Deactivate rather than drop: an inactive route keeps the audit trail of
    which mailbox once belonged to which team."""
    update_email_route(db, route_id, is_active=False, is_primary=False)
