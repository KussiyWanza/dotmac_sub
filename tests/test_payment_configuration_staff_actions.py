from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.billing import (
    CollectionAccount,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentProvider,
)
from app.services import payment_configuration_staff_actions as actions
from app.services.owner_commands import CommandContext


def _context(resource: actions.PaymentConfigurationResource) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope=actions.action_scope(resource),
        reason="pytest reviewed payment configuration",
        idempotency_key=f"pytest:{command_id}",
    )


def _confirm(db, preview):
    return actions.confirm_staff_action(
        db,
        actions.ConfirmPaymentConfigurationStaffAction(
            resource=preview.resource,
            resource_id=preview.resource_id,
            action=preview.action,
            preview_fingerprint=preview.fingerprint,
            confirmed=True,
            actor_id="pytest-user",
            context=_context(preview.resource),
        ),
    )


def _account(db, name: str, *, active: bool = True) -> CollectionAccount:
    account = CollectionAccount(
        name=f"{name}-{uuid4().hex[:6]}",
        currency="NGN",
        bank_name="Test Bank",
        account_name=name,
        account_number=uuid4().hex[:10],
        is_active=active,
    )
    db.add(account)
    db.commit()
    return account


def _channel(db, name: str, *, provider=None, active=True) -> PaymentChannel:
    channel = PaymentChannel(
        name=f"{name}-{uuid4().hex[:6]}",
        provider_id=provider.id if provider else None,
        is_active=active,
    )
    db.add(channel)
    db.commit()
    return channel


def test_collection_account_deactivation_cascades_mappings_and_audits(db_session):
    account = _account(db_session, "Primary")
    _account(db_session, "Replacement")
    channel = _channel(db_session, "Bank transfer")
    mapping = PaymentChannelAccount(
        channel_id=channel.id,
        collection_account_id=account.id,
        is_active=True,
        is_default=True,
    )
    db_session.add(mapping)
    db_session.commit()

    preview = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.collection_account,
        resource_id=account.id,
        action=actions.PaymentConfigurationAction.deactivate,
    )
    assert preview.allowed is True
    db_session.commit()
    _confirm(db_session, preview)

    db_session.refresh(account)
    db_session.refresh(mapping)
    assert account.is_active is False
    assert mapping.is_active is False
    assert mapping.is_default is False
    event = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_type == "collection_account",
            AuditEvent.entity_id == str(account.id),
        )
    )
    assert event is not None
    assert event.metadata_["preview_fingerprint"] == preview.fingerprint


def test_last_currency_destination_fails_closed(db_session):
    account = _account(db_session, "Only destination")
    preview = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.collection_account,
        resource_id=account.id,
        action=actions.PaymentConfigurationAction.deactivate,
    )
    assert preview.allowed is False
    assert "last customer transfer destination" in (preview.blocked_reason or "")


def test_stale_mapping_preview_is_rejected_without_mutation(db_session):
    account = _account(db_session, "Destination")
    channel = _channel(db_session, "Cash")
    mapping = PaymentChannelAccount(
        channel_id=channel.id,
        collection_account_id=account.id,
        priority=1,
        is_active=False,
    )
    db_session.add(mapping)
    db_session.commit()
    preview = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.channel_mapping,
        resource_id=mapping.id,
        action=actions.PaymentConfigurationAction.activate,
    )
    mapping.priority = 2
    db_session.commit()

    with pytest.raises(
        actions.PaymentConfigurationStaffActionError,
        match="changed after preview",
    ):
        _confirm(db_session, preview)
    db_session.refresh(mapping)
    assert mapping.is_active is False


def test_default_mapping_requires_reviewed_replacement(db_session):
    account_one = _account(db_session, "One")
    account_two = _account(db_session, "Two")
    channel = _channel(db_session, "Transfer")
    first = PaymentChannelAccount(
        channel_id=channel.id,
        collection_account_id=account_one.id,
        currency="NGN",
        is_active=True,
        is_default=True,
    )
    second = PaymentChannelAccount(
        channel_id=channel.id,
        collection_account_id=account_two.id,
        currency="NGN",
        is_active=True,
        is_default=False,
    )
    db_session.add_all([first, second])
    db_session.commit()

    blocked = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.channel_mapping,
        resource_id=first.id,
        action=actions.PaymentConfigurationAction.deactivate,
    )
    assert blocked.allowed is False

    replacement = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.channel_mapping,
        resource_id=second.id,
        action=actions.PaymentConfigurationAction.make_default,
    )
    db_session.commit()
    _confirm(db_session, replacement)
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_default is False
    assert second.is_default is True


def test_channel_default_does_not_claim_checkout_routing(db_session):
    provider = PaymentProvider(name=f"Provider-{uuid4().hex[:6]}")
    db_session.add(provider)
    db_session.commit()
    first = _channel(db_session, "Provider channel one", provider=provider)
    second = _channel(db_session, "Provider channel two", provider=provider)
    first.is_default = True
    db_session.commit()

    preview = actions.preview_staff_action(
        db_session,
        resource=actions.PaymentConfigurationResource.payment_channel,
        resource_id=second.id,
        action=actions.PaymentConfigurationAction.make_default,
    )
    assert any(
        fact.label == "Checkout routing" and fact.value.startswith("Unchanged")
        for fact in preview.facts
    )
    db_session.commit()
    _confirm(db_session, preview)
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_default is False
    assert second.is_default is True
