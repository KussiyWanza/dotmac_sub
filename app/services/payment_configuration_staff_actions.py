"""Reviewed lifecycle commands for payment configuration.

This coordinator owns staff decisions that change whether collection accounts,
settlement-attribution channels, and their mappings participate operationally.
Connector-backed payment routing remains a separate customer-checkout owner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    CollectionAccount,
    CollectionAccountType,
    Payment,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
)
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.collection_accounts import CollectionAccounts
from app.services.billing.payments import PaymentChannelAccounts, PaymentChannels
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.payment_configuration_staff_actions"
CONCERN = "reviewed payment configuration lifecycle and audit coordination"
ACCOUNT_ACTION_SCOPE = "billing:account:write"
CHANNEL_ACTION_SCOPE = "billing:channel:write"
_CONFIRM = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="confirm_payment_configuration_staff_action",
)


class PaymentConfigurationResource(StrEnum):
    collection_account = "collection_account"
    payment_channel = "payment_channel"
    channel_mapping = "channel_mapping"


class PaymentConfigurationAction(StrEnum):
    activate = "activate"
    deactivate = "deactivate"
    make_default = "make_default"


def action_scope(resource: PaymentConfigurationResource) -> str:
    if resource is PaymentConfigurationResource.collection_account:
        return ACCOUNT_ACTION_SCOPE
    return CHANNEL_ACTION_SCOPE


class PaymentConfigurationStaffActionError(DomainError):
    """Stable staff-action failure."""


@dataclass(frozen=True, slots=True)
class ImpactFact:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class PaymentConfigurationActionPreview:
    resource: PaymentConfigurationResource
    resource_id: UUID
    resource_name: str
    action: PaymentConfigurationAction
    current_active: bool
    resulting_active: bool
    allowed: bool
    blocked_reason: str | None
    facts: tuple[ImpactFact, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ConfirmPaymentConfigurationStaffAction:
    resource: PaymentConfigurationResource
    resource_id: UUID
    action: PaymentConfigurationAction
    preview_fingerprint: str
    confirmed: bool
    actor_id: str
    context: CommandContext


def _error(
    suffix: str, message: str, **details: object
) -> PaymentConfigurationStaffActionError:
    return PaymentConfigurationStaffActionError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _query_one(db: Session, model, resource_id: UUID, *, lock: bool):
    query = db.query(model).filter(model.id == resource_id)
    if lock:
        query = query.with_for_update()
    return query.one_or_none()


def _rows(query, *, lock: bool):
    return (query.with_for_update() if lock else query).all()


def _fingerprint(
    *,
    resource: PaymentConfigurationResource,
    resource_id: UUID,
    action: PaymentConfigurationAction,
    snapshot: dict[str, object],
) -> str:
    payload = {
        "resource": resource.value,
        "resource_id": str(resource_id),
        "action": action.value,
        "snapshot": snapshot,
    }
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def _finish_preview(
    *,
    resource: PaymentConfigurationResource,
    resource_id: UUID,
    resource_name: str,
    action: PaymentConfigurationAction,
    current_active: bool,
    resulting_active: bool,
    allowed: bool,
    blocked_reason: str | None,
    facts: tuple[ImpactFact, ...],
    snapshot: dict[str, object],
) -> PaymentConfigurationActionPreview:
    return PaymentConfigurationActionPreview(
        resource=resource,
        resource_id=resource_id,
        resource_name=resource_name,
        action=action,
        current_active=current_active,
        resulting_active=resulting_active,
        allowed=allowed,
        blocked_reason=blocked_reason,
        facts=facts,
        fingerprint=_fingerprint(
            resource=resource,
            resource_id=resource_id,
            action=action,
            snapshot=snapshot,
        ),
    )


def _collection_account_preview(
    db: Session,
    resource_id: UUID,
    action: PaymentConfigurationAction,
    *,
    lock: bool,
) -> PaymentConfigurationActionPreview:
    account = _query_one(db, CollectionAccount, resource_id, lock=lock)
    if account is None:
        raise _error("not_found", "Collection account not found.")
    if action is PaymentConfigurationAction.make_default:
        raise _error(
            "invalid_action",
            "Collection-account presentment order is controlled by display priority.",
        )
    mappings = _rows(
        db.query(PaymentChannelAccount).filter(
            PaymentChannelAccount.collection_account_id == account.id
        ),
        lock=lock,
    )
    presentable = (
        account.account_type is CollectionAccountType.bank
        and bool(account.bank_name)
        and bool(account.account_name)
        and bool(account.account_number)
    )
    active_destinations = (
        _rows(
            db.query(CollectionAccount).filter(
                CollectionAccount.currency == account.currency,
                CollectionAccount.account_type == CollectionAccountType.bank,
                CollectionAccount.bank_name.is_not(None),
                CollectionAccount.account_name.is_not(None),
                CollectionAccount.account_number.is_not(None),
                CollectionAccount.is_active.is_(True),
                CollectionAccount.id != account.id,
            ),
            lock=lock,
        )
        if presentable
        else []
    )
    resulting_active = action is PaymentConfigurationAction.activate
    allowed = resulting_active != account.is_active
    blocked_reason = (
        None
        if allowed
        else (
            "This collection account is already active."
            if resulting_active
            else "This collection account is already inactive."
        )
    )
    if not resulting_active and presentable and not active_destinations:
        allowed = False
        blocked_reason = (
            f"Activate another {account.currency} collection account before "
            "removing the last customer transfer destination."
        )
    active_mappings = [item for item in mappings if item.is_active]
    facts = (
        ImpactFact(
            "Customer presentment",
            (
                (
                    f"Available as a {account.currency} transfer destination"
                    if resulting_active
                    else f"Removed from {account.currency} transfer instructions"
                )
                if presentable
                else "Not a complete customer transfer destination"
            ),
        ),
        ImpactFact(
            "Settlement mappings",
            (
                f"{len(active_mappings)} active mapping(s) will be deactivated"
                if not resulting_active
                else f"{len(active_mappings)} existing active mapping(s) unchanged"
            ),
        ),
        ImpactFact("Historical payments", "Preserved; no payment rows are rewritten"),
        ImpactFact(
            "Checkout routing",
            "Unchanged; connector-backed Payment Routing owns checkout",
        ),
    )
    return _finish_preview(
        resource=PaymentConfigurationResource.collection_account,
        resource_id=account.id,
        resource_name=account.name,
        action=action,
        current_active=account.is_active,
        resulting_active=resulting_active,
        allowed=allowed,
        blocked_reason=blocked_reason,
        facts=facts,
        snapshot={
            "active": account.is_active,
            "currency": account.currency,
            "presentment_priority": account.presentment_priority,
            "presentable": presentable,
            "active_mapping_ids": sorted(str(item.id) for item in active_mappings),
            "other_active_destination_ids": sorted(
                str(item.id) for item in active_destinations
            ),
        },
    )


def _payment_channel_preview(
    db: Session,
    resource_id: UUID,
    action: PaymentConfigurationAction,
    *,
    lock: bool,
) -> PaymentConfigurationActionPreview:
    channel = _query_one(db, PaymentChannel, resource_id, lock=lock)
    if channel is None:
        raise _error("not_found", "Payment channel not found.")
    provider_peers = _rows(
        db.query(PaymentChannel).filter(
            PaymentChannel.provider_id == channel.provider_id,
            PaymentChannel.id != channel.id,
        ),
        lock=lock,
    )
    mappings = _rows(
        db.query(PaymentChannelAccount).filter(
            PaymentChannelAccount.channel_id == channel.id
        ),
        lock=lock,
    )
    payment_count = (
        db.query(Payment.id).filter(Payment.payment_channel_id == channel.id).count()
    )
    method_count = (
        db.query(PaymentMethod.id)
        .filter(
            PaymentMethod.payment_channel_id == channel.id,
            PaymentMethod.is_active.is_(True),
        )
        .count()
    )
    if action is PaymentConfigurationAction.make_default:
        resulting_active = True
        allowed = channel.provider_id is not None and not channel.is_default
        blocked_reason = None
        if channel.provider_id is None:
            blocked_reason = "Only provider-linked channels can be provider defaults."
        elif channel.is_default:
            blocked_reason = "This channel is already the provider default."
    else:
        resulting_active = action is PaymentConfigurationAction.activate
        allowed = resulting_active != channel.is_active
        blocked_reason = (
            None
            if allowed
            else (
                "This channel is already active."
                if resulting_active
                else "This channel is already inactive."
            )
        )
        other_active = [peer for peer in provider_peers if peer.is_active]
        if (
            not resulting_active
            and channel.provider_id is not None
            and not other_active
        ):
            allowed = False
            blocked_reason = (
                "Activate another channel for this provider before deactivating "
                "its last settlement-attribution identity."
            )
    facts = (
        ImpactFact(
            "Settlement attribution",
            "Enabled" if resulting_active else "Disabled for new payment records",
        ),
        ImpactFact(
            "Provider default",
            (
                "This channel becomes the provider default"
                if action is PaymentConfigurationAction.make_default
                else ("Yes" if channel.is_default else "No")
            ),
        ),
        ImpactFact(
            "Mappings",
            f"{sum(item.is_active for item in mappings)} active of {len(mappings)}",
        ),
        ImpactFact(
            "Recorded history",
            f"{payment_count} payment(s) and {method_count} active method(s) preserved",
        ),
        ImpactFact(
            "Checkout routing",
            "Unchanged; connector-backed Payment Routing owns checkout",
        ),
    )
    return _finish_preview(
        resource=PaymentConfigurationResource.payment_channel,
        resource_id=channel.id,
        resource_name=channel.name,
        action=action,
        current_active=channel.is_active,
        resulting_active=resulting_active,
        allowed=allowed,
        blocked_reason=blocked_reason,
        facts=facts,
        snapshot={
            "active": channel.is_active,
            "default": channel.is_default,
            "provider_id": channel.provider_id,
            "peer_states": sorted(
                (str(peer.id), peer.is_active, peer.is_default)
                for peer in provider_peers
            ),
            "mapping_states": sorted(
                (str(item.id), item.is_active, item.is_default) for item in mappings
            ),
            "payment_count": payment_count,
            "method_count": method_count,
        },
    )


def _channel_mapping_preview(
    db: Session,
    resource_id: UUID,
    action: PaymentConfigurationAction,
    *,
    lock: bool,
) -> PaymentConfigurationActionPreview:
    mapping = _query_one(db, PaymentChannelAccount, resource_id, lock=lock)
    if mapping is None:
        raise _error("not_found", "Payment-channel mapping not found.")
    channel = _query_one(db, PaymentChannel, mapping.channel_id, lock=lock)
    account = _query_one(
        db, CollectionAccount, mapping.collection_account_id, lock=lock
    )
    if channel is None or account is None:
        raise _error("invalid_mapping", "Mapping references missing configuration.")
    peers = _rows(
        db.query(PaymentChannelAccount).filter(
            PaymentChannelAccount.channel_id == mapping.channel_id,
            PaymentChannelAccount.currency == mapping.currency,
            PaymentChannelAccount.id != mapping.id,
        ),
        lock=lock,
    )
    if action is PaymentConfigurationAction.make_default:
        resulting_active = True
        allowed = mapping.is_active and not mapping.is_default
        blocked_reason = None
        if not mapping.is_active:
            blocked_reason = "Activate this mapping before making it default."
        elif mapping.is_default:
            blocked_reason = "This mapping is already default."
    else:
        resulting_active = action is PaymentConfigurationAction.activate
        allowed = resulting_active != mapping.is_active
        blocked_reason = (
            None
            if allowed
            else (
                "This mapping is already active."
                if resulting_active
                else "This mapping is already inactive."
            )
        )
        if resulting_active and (not channel.is_active or not account.is_active):
            allowed = False
            blocked_reason = (
                "Activate both the payment channel and collection account first."
            )
        if (
            not resulting_active
            and mapping.is_default
            and any(peer.is_active for peer in peers)
        ):
            allowed = False
            blocked_reason = (
                "Make another active mapping the default before deactivating this one."
            )
    facts = (
        ImpactFact("Channel", channel.name),
        ImpactFact("Collection account", f"{account.name} ({account.currency})"),
        ImpactFact("Currency scope", mapping.currency or "All currencies"),
        ImpactFact("Priority", str(mapping.priority)),
        ImpactFact(
            "Attribution state",
            "Active" if resulting_active else "Inactive for new settlement records",
        ),
        ImpactFact(
            "Checkout routing",
            "Unchanged; this is settlement attribution only",
        ),
    )
    return _finish_preview(
        resource=PaymentConfigurationResource.channel_mapping,
        resource_id=mapping.id,
        resource_name=f"{channel.name} → {account.name}",
        action=action,
        current_active=mapping.is_active,
        resulting_active=resulting_active,
        allowed=allowed,
        blocked_reason=blocked_reason,
        facts=facts,
        snapshot={
            "active": mapping.is_active,
            "default": mapping.is_default,
            "channel_active": channel.is_active,
            "account_active": account.is_active,
            "currency": mapping.currency,
            "priority": mapping.priority,
            "peer_states": sorted(
                (str(peer.id), peer.is_active, peer.is_default) for peer in peers
            ),
        },
    )


def preview_staff_action(
    db: Session,
    *,
    resource: PaymentConfigurationResource,
    resource_id: UUID,
    action: PaymentConfigurationAction,
    lock: bool = False,
) -> PaymentConfigurationActionPreview:
    if resource is PaymentConfigurationResource.collection_account:
        return _collection_account_preview(db, resource_id, action, lock=lock)
    if resource is PaymentConfigurationResource.payment_channel:
        return _payment_channel_preview(db, resource_id, action, lock=lock)
    return _channel_mapping_preview(db, resource_id, action, lock=lock)


def _stage_confirmation(
    db: Session,
    command: ConfirmPaymentConfigurationStaffAction,
) -> PaymentConfigurationActionPreview:
    if command.context.scope != action_scope(command.resource):
        raise _error("invalid_scope", "Payment configuration action scope is invalid.")
    if not command.actor_id.strip():
        raise _error("invalid_actor", "Authorized staff actor is required.")
    if not command.confirmed:
        raise _error(
            "confirmation_required",
            "Confirm the displayed impact before applying this action.",
        )
    preview = preview_staff_action(
        db,
        resource=command.resource,
        resource_id=command.resource_id,
        action=command.action,
        lock=True,
    )
    if not hmac.compare_digest(preview.fingerprint, command.preview_fingerprint):
        raise _error(
            "stale_preview",
            "Payment configuration changed after preview; review the new impact.",
        )
    if not preview.allowed:
        raise _error(
            "action_not_available",
            preview.blocked_reason or "Payment configuration action is unavailable.",
        )

    if command.resource is PaymentConfigurationResource.collection_account:
        account = db.get(CollectionAccount, command.resource_id)
        if account is None:
            raise _error("not_found", "Collection account not found.")
        CollectionAccounts.stage_active(account, active=preview.resulting_active)
        if not preview.resulting_active:
            mappings = db.query(PaymentChannelAccount).filter(
                PaymentChannelAccount.collection_account_id == account.id,
                PaymentChannelAccount.is_active.is_(True),
            )
            for linked_mapping in mappings.all():
                PaymentChannelAccounts.stage_active(linked_mapping, active=False)
    elif command.resource is PaymentConfigurationResource.payment_channel:
        channel = db.get(PaymentChannel, command.resource_id)
        if channel is None:
            raise _error("not_found", "Payment channel not found.")
        if command.action is PaymentConfigurationAction.make_default:
            PaymentChannels.stage_default(db, channel)
        else:
            PaymentChannels.stage_active(channel, active=preview.resulting_active)
    else:
        mapping = db.get(PaymentChannelAccount, command.resource_id)
        if mapping is None:
            raise _error("not_found", "Payment-channel mapping not found.")
        if command.action is PaymentConfigurationAction.make_default:
            PaymentChannelAccounts.stage_default(db, mapping)
        else:
            PaymentChannelAccounts.stage_active(
                mapping, active=preview.resulting_active
            )

    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            action=f"payment_configuration.{command.action.value}",
            entity_type=command.resource.value,
            entity_id=str(command.resource_id),
            request_id=str(command.context.correlation_id),
            metadata_={
                "owner": OWNER,
                "resource_name": preview.resource_name,
                "current_active": preview.current_active,
                "resulting_active": preview.resulting_active,
                "preview_fingerprint": preview.fingerprint,
                "command_id": str(command.context.command_id),
                "scope": command.context.scope,
                "impact": {fact.label: fact.value for fact in preview.facts},
            },
        ),
    )
    return preview


def confirm_staff_action(
    db: Session,
    command: ConfirmPaymentConfigurationStaffAction,
) -> PaymentConfigurationActionPreview:
    return execute_owner_command(
        db,
        definition=_CONFIRM,
        context=command.context,
        operation=lambda: _stage_confirmation(db, command),
    )
