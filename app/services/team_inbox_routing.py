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
    TeamInboxAiRoute,
    TeamInboxChannelRoute,
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
    outbound_email_sender_key: str | None


@dataclass(frozen=True)
class ChannelRouteRow:
    id: str
    channel_type: str
    provider: str
    account_scope: str
    display_name: str | None
    service_team_id: str
    service_team_name: str | None
    allow_ai_routing: bool
    priority: int
    is_active: bool


@dataclass(frozen=True)
class AiRouteRow:
    id: str
    channel_type: str
    intent_key: str
    display_name: str | None
    service_team_id: str
    service_team_name: str | None
    confidence_threshold: float
    priority: int
    is_active: bool


@dataclass(frozen=True)
class ChannelRoutingDecision:
    primary_service_team_id: str | None
    channel_service_team_id: str | None
    ai_service_team_id: str | None
    channel_route_id: str | None
    ai_route_id: str | None
    ai_routing_allowed: bool
    ai_intent_key: str | None
    ai_confidence: float | None
    reason: str


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
            outbound_email_sender_key=str(
                (route.metadata_ or {}).get(
                    team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY
                )
                or ""
            ).strip()
            or None,
        )
        for route, team in query.all()
    ]


def _normalize_key(value: str | None) -> str | None:
    text = "_".join(str(value or "").strip().lower().replace("-", "_").split())
    return text or None


def _normalize_provider(value: str | None) -> str:
    return _normalize_key(value) or "default"


def _normalize_account_scope(value: str | None) -> str:
    return str(value or "default").strip()[:160] or "default"


def _valid_channel(value: str | None, *, allow_any: bool = False) -> str:
    normalized = _normalize_key(value)
    if allow_any and normalized == "any":
        return "any"
    from app.models.team_inbox import InboxChannelType

    allowed = {item.value for item in InboxChannelType}
    if normalized not in allowed:
        raise EmailRouteError("Choose a supported inbox channel.")
    return normalized


def _active_team(db: Session, service_team_id: str | UUID):
    from app.models.service_team import ServiceTeam

    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        raise EmailRouteError("Choose a service team.")
    team = db.get(ServiceTeam, UUID(team_uuid))
    if team is None or not team.is_active:
        raise EmailRouteError("Service team not found.")
    return team


def list_channel_routes(
    db: Session, *, include_inactive: bool = True
) -> list[ChannelRouteRow]:
    from app.models.service_team import ServiceTeam

    query = (
        db.query(TeamInboxChannelRoute, ServiceTeam)
        .outerjoin(ServiceTeam, ServiceTeam.id == TeamInboxChannelRoute.service_team_id)
        .order_by(
            TeamInboxChannelRoute.priority.asc(),
            TeamInboxChannelRoute.channel_type.asc(),
            TeamInboxChannelRoute.provider.asc(),
            TeamInboxChannelRoute.account_scope.asc(),
        )
    )
    if not include_inactive:
        query = query.filter(TeamInboxChannelRoute.is_active.is_(True))
    return [
        ChannelRouteRow(
            id=str(route.id),
            channel_type=route.channel_type,
            provider=route.provider,
            account_scope=route.account_scope,
            display_name=route.display_name,
            service_team_id=str(route.service_team_id),
            service_team_name=team.name if team is not None else None,
            allow_ai_routing=bool(route.allow_ai_routing),
            priority=int(route.priority),
            is_active=bool(route.is_active),
        )
        for route, team in query.all()
    ]


def create_channel_route(
    db: Session,
    *,
    channel_type: str,
    provider: str | None,
    account_scope: str | None,
    service_team_id: str | UUID,
    display_name: str | None = None,
    allow_ai_routing: bool = True,
    priority: int = 100,
) -> TeamInboxChannelRoute:
    channel = _valid_channel(channel_type)
    provider_key = _normalize_provider(provider)
    scope = _normalize_account_scope(account_scope)
    team = _active_team(db, service_team_id)
    existing = (
        db.query(TeamInboxChannelRoute)
        .filter(TeamInboxChannelRoute.channel_type == channel)
        .filter(TeamInboxChannelRoute.provider == provider_key)
        .filter(TeamInboxChannelRoute.account_scope == scope)
        .one_or_none()
    )
    if existing is not None:
        raise EmailRouteError("This channel account is already routed.")
    route = TeamInboxChannelRoute(
        channel_type=channel,
        provider=provider_key,
        account_scope=scope,
        display_name=str(display_name or "").strip()[:160] or None,
        service_team_id=team.id,
        allow_ai_routing=bool(allow_ai_routing),
        priority=int(priority),
        is_active=True,
    )
    db.add(route)
    db.flush()
    return route


def update_channel_route(
    db: Session,
    route_id: str | UUID,
    *,
    service_team_id: str | UUID | None = None,
    display_name: str | None = None,
    allow_ai_routing: bool | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> TeamInboxChannelRoute:
    route_uuid = _coerce_uuid(route_id)
    route = db.get(TeamInboxChannelRoute, UUID(route_uuid)) if route_uuid else None
    if route is None:
        raise EmailRouteError("Channel route not found.")
    if service_team_id:
        route.service_team_id = _active_team(db, service_team_id).id
    if display_name is not None:
        route.display_name = str(display_name or "").strip()[:160] or None
    if allow_ai_routing is not None:
        route.allow_ai_routing = bool(allow_ai_routing)
    if priority is not None:
        route.priority = int(priority)
    if is_active is not None:
        route.is_active = bool(is_active)
    db.flush()
    return route


def delete_channel_route(db: Session, route_id: str | UUID) -> None:
    update_channel_route(db, route_id, is_active=False)


def list_ai_routes(db: Session, *, include_inactive: bool = True) -> list[AiRouteRow]:
    from app.models.service_team import ServiceTeam

    query = (
        db.query(TeamInboxAiRoute, ServiceTeam)
        .outerjoin(ServiceTeam, ServiceTeam.id == TeamInboxAiRoute.service_team_id)
        .order_by(
            TeamInboxAiRoute.priority.asc(),
            TeamInboxAiRoute.channel_type.asc(),
            TeamInboxAiRoute.intent_key.asc(),
        )
    )
    if not include_inactive:
        query = query.filter(TeamInboxAiRoute.is_active.is_(True))
    return [
        AiRouteRow(
            id=str(route.id),
            channel_type=route.channel_type,
            intent_key=route.intent_key,
            display_name=route.display_name,
            service_team_id=str(route.service_team_id),
            service_team_name=team.name if team is not None else None,
            confidence_threshold=float(route.confidence_threshold),
            priority=int(route.priority),
            is_active=bool(route.is_active),
        )
        for route, team in query.all()
    ]


def create_ai_route(
    db: Session,
    *,
    channel_type: str,
    intent_key: str,
    service_team_id: str | UUID,
    display_name: str | None = None,
    confidence_threshold: float = 0.75,
    priority: int = 100,
) -> TeamInboxAiRoute:
    channel = _valid_channel(channel_type, allow_any=True)
    intent = _normalize_key(intent_key)
    if not intent:
        raise EmailRouteError("AI intent is required.")
    team = _active_team(db, service_team_id)
    threshold = min(max(float(confidence_threshold), 0.0), 1.0)
    existing = (
        db.query(TeamInboxAiRoute)
        .filter(TeamInboxAiRoute.channel_type == channel)
        .filter(TeamInboxAiRoute.intent_key == intent)
        .one_or_none()
    )
    if existing is not None:
        raise EmailRouteError("This AI intake route already exists.")
    route = TeamInboxAiRoute(
        channel_type=channel,
        intent_key=intent,
        display_name=str(display_name or "").strip()[:160] or None,
        service_team_id=team.id,
        confidence_threshold=threshold,
        priority=int(priority),
        is_active=True,
    )
    db.add(route)
    db.flush()
    return route


def update_ai_route(
    db: Session,
    route_id: str | UUID,
    *,
    service_team_id: str | UUID | None = None,
    display_name: str | None = None,
    confidence_threshold: float | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> TeamInboxAiRoute:
    route_uuid = _coerce_uuid(route_id)
    route = db.get(TeamInboxAiRoute, UUID(route_uuid)) if route_uuid else None
    if route is None:
        raise EmailRouteError("AI route not found.")
    if service_team_id:
        route.service_team_id = _active_team(db, service_team_id).id
    if display_name is not None:
        route.display_name = str(display_name or "").strip()[:160] or None
    if confidence_threshold is not None:
        route.confidence_threshold = min(max(float(confidence_threshold), 0.0), 1.0)
    if priority is not None:
        route.priority = int(priority)
    if is_active is not None:
        route.is_active = bool(is_active)
    db.flush()
    return route


def delete_ai_route(db: Session, route_id: str | UUID) -> None:
    update_ai_route(db, route_id, is_active=False)


def _metadata_text(metadata: dict | None, *keys: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _metadata_float(metadata: dict | None, *keys: str) -> float | None:
    value = _metadata_text(metadata, *keys)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def resolve_channel_routing_decision(
    db: Session,
    *,
    channel_type: str,
    provider: str | None = None,
    account_scope: str | None = None,
    fallback_service_team_id: str | UUID | None = None,
    metadata: dict | None = None,
) -> ChannelRoutingDecision:
    channel = _valid_channel(channel_type)
    provider_key = _normalize_provider(provider)
    scope = _normalize_account_scope(account_scope)
    route = (
        db.query(TeamInboxChannelRoute)
        .filter(TeamInboxChannelRoute.is_active.is_(True))
        .filter(TeamInboxChannelRoute.channel_type == channel)
        .filter(TeamInboxChannelRoute.provider == provider_key)
        .filter(TeamInboxChannelRoute.account_scope == scope)
        .order_by(TeamInboxChannelRoute.priority.asc())
        .first()
    )
    channel_team_id = str(route.service_team_id) if route is not None else None
    ai_allowed = route.allow_ai_routing if route is not None else True
    base_team_id = channel_team_id or _coerce_uuid(fallback_service_team_id)
    intent = _normalize_key(
        _metadata_text(metadata, "ai_intent", "ai_category", "intent", "category")
    )
    confidence = _metadata_float(
        metadata, "ai_confidence", "classification_confidence", "confidence"
    )
    ai_route = None
    if ai_allowed and intent and confidence is not None:
        ai_route = (
            db.query(TeamInboxAiRoute)
            .filter(TeamInboxAiRoute.is_active.is_(True))
            .filter(TeamInboxAiRoute.intent_key == intent)
            .filter(TeamInboxAiRoute.channel_type.in_((channel, "any")))
            .filter(TeamInboxAiRoute.confidence_threshold <= confidence)
            .order_by(
                TeamInboxAiRoute.priority.asc(),
                TeamInboxAiRoute.channel_type.desc(),
            )
            .first()
        )
    if ai_route is not None:
        return ChannelRoutingDecision(
            primary_service_team_id=str(ai_route.service_team_id),
            channel_service_team_id=channel_team_id,
            ai_service_team_id=str(ai_route.service_team_id),
            channel_route_id=str(route.id) if route is not None else None,
            ai_route_id=str(ai_route.id),
            ai_routing_allowed=ai_allowed,
            ai_intent_key=intent,
            ai_confidence=confidence,
            reason="ai_intake_route",
        )
    return ChannelRoutingDecision(
        primary_service_team_id=base_team_id,
        channel_service_team_id=channel_team_id,
        ai_service_team_id=None,
        channel_route_id=str(route.id) if route is not None else None,
        ai_route_id=None,
        ai_routing_allowed=ai_allowed,
        ai_intent_key=intent,
        ai_confidence=confidence,
        reason="channel_route" if channel_team_id else "fallback_route",
    )


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
    outbound_email_sender_key: str | None = None,
    update_outbound_email_sender: bool = False,
) -> TeamInboxEmailRoute:
    route_uuid = _coerce_uuid(route_id)
    route = db.get(TeamInboxEmailRoute, UUID(route_uuid)) if route_uuid else None
    if route is None:
        raise EmailRouteError("Email route not found.")

    if update_outbound_email_sender:
        from app.services.email import list_smtp_sender_options

        sender_key = str(outbound_email_sender_key or "").strip().lower()
        active_keys = {option.sender_key for option in list_smtp_sender_options(db)}
        if sender_key and sender_key not in active_keys:
            raise EmailRouteError("Choose an active SMTP sender profile.")
        metadata = dict(route.metadata_ or {})
        if sender_key:
            metadata[team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY] = sender_key
        else:
            metadata.pop(team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY, None)
        route.metadata_ = metadata or None

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
