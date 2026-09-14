"""Focused lifecycle tests for the ONT Configure application owner."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import (
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningEvent,
    OntProvisioningEventStatus,
    OntSyncStatus,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
    PonPort,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_service_configuration import (
    OntServiceConfigurationHead,
    OntServiceConfigurationPhase,
    OntServiceConfigurationRevision,
)
from app.models.subscriber import Subscriber
from app.services.catalog.ip_block_choices import IpBlockPrefix
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.network.ont_service_configuration import (
    ConfigureCustomerWifiCommand,
    ConfigureOntServiceCommand,
    CustomerWifiConfigurationChange,
    ExecuteOntServiceConfigurationCommand,
    LanConfigurationChange,
    OntConfigurationChange,
    OntConfigurationNextAction,
    OntConfigurationSection,
    RetryOntServiceConfigurationCommand,
    VerifyOntServiceConfigurationReadbackCommand,
    WanConfigurationChange,
    WifiConfigurationChange,
    configure_customer_wifi,
    configure_ont_service,
    execute_ont_service_configuration,
    get_latest_ont_configuration_section_delivery,
    get_ont_service_configuration_eligibility,
    get_ont_service_configuration_projection,
    retry_ont_service_configuration,
    verify_ont_service_configuration_readback,
)
from app.services.network.reconcile.lifecycle import (
    RetireOntReconcileProjectionForInventory,
    retire_ont_reconcile_projection_for_inventory,
)
from app.services.owner_commands import CommandContext
from tests.subscription_fixture_helpers import activate_test_subscription


def _operation(db_session, ont: OntUnit, suffix: str) -> NetworkOperation:
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.pending,
        correlation_key=f"ont-config-test:{suffix}:{uuid.uuid4()}",
        input_payload={},
        initiated_by="test",
    )
    db_session.add(operation)
    db_session.flush()
    return operation


def _admission_scope(
    db_session,
    monkeypatch,
    *,
    olt_device,
    subscription,
    subscriber,
) -> tuple[uuid.UUID, uuid.UUID]:
    activate_test_subscription(db_session, subscription)
    olt_device.is_active = True
    pon = PonPort(
        olt_id=olt_device.id,
        name=f"0/1/{uuid.uuid4().int % 100000}",
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"ADMIT-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    ont_id = ont.id
    assignment_id = assignment.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": {"id": "test-pack"},
            "values": {"wan_mode": "dhcp", "wan_vlan": 321},
        },
    )
    return ont_id, assignment_id


def _configure_command(
    ont_id: uuid.UUID,
    *,
    idempotency_key: str,
    section: OntConfigurationSection = OntConfigurationSection.wan,
    change: OntConfigurationChange | None = None,
) -> ConfigureOntServiceCommand:
    command_id = uuid.uuid4()
    return ConfigureOntServiceCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="test:operator",
            scope="network:ont:write",
            reason="reviewed ONT customer-service configuration",
            idempotency_key=idempotency_key,
        ),
        ont_unit_id=ont_id,
        permission_granted=True,
        section=section,
        change=change
        or WanConfigurationChange(
            mode="dhcp",
            ip_protocol="ipv4",
            static_ip=None,
            static_subnet=None,
            static_gateway=None,
            static_dns=None,
        ),
    )


def _customer_wifi_command(
    subscriber_id: uuid.UUID,
    subscription_id: uuid.UUID,
    *,
    idempotency_key: str,
) -> ConfigureCustomerWifiCommand:
    command_id = uuid.uuid4()
    return ConfigureCustomerWifiCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="customer:test",
            scope="customer:device:wifi",
            reason="Customer requested a WiFi credential update",
            idempotency_key=idempotency_key,
        ),
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        change=CustomerWifiConfigurationChange(
            ssid="CustomerSSID",
            password=None,
        ),
    )


def test_configuration_eligibility_discloses_supported_owner_surface(
    db_session, monkeypatch, olt_device
):
    ont = OntUnit(
        serial_number=f"ELIG-{uuid.uuid4().hex[:10]}",
        is_active=True,
        olt_device_id=olt_device.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": SimpleNamespace(id="test-pack"),
            "values": {"wan_mode": "dhcp"},
        },
    )

    eligibility = get_ont_service_configuration_eligibility(
        db_session, ont_unit_id=ont.id
    )

    assert eligibility.routed_wan_configurable is True
    assert eligibility.lan_dhcp_configurable is True
    assert eligibility.bridge_mode_configurable is False
    assert eligibility.nat_toggle_configurable is False
    assert eligibility.nat_default_enabled is True
    assert eligibility.retain_config_on_move_supported is False
    assert "Routed DHCP" in eligibility.routed_wan_message
    assert "dedicated WAN-mode transition owner" in eligibility.bridge_mode_message
    assert "typed WAN intent" in eligibility.nat_message
    assert "inventory move flow" in eligibility.move_message


def test_configuration_admission_commits_intent_operation_and_dispatch_atomically(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    command = _configure_command(ont_id, idempotency_key="atomic-admission")

    outcome = configure_ont_service(db_session, command)

    head = db_session.get(OntServiceConfigurationHead, outcome.configuration_head_id)
    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == outcome.revision,
        )
    )
    intent = db_session.scalar(
        select(OntWanServiceInstance).where(
            OntWanServiceInstance.ont_id == ont_id,
            OntWanServiceInstance.subscription_id == subscription.id,
            OntWanServiceInstance.lifecycle_state == OntWanServiceLifecycle.active,
        )
    )
    operation = db_session.get(NetworkOperation, outcome.operation_id)
    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == outcome.operation_id
        )
    )

    assert head is not None and head.assignment_id == assignment_id
    assert operation is not None
    assert revision is not None and revision.operation_id == operation.id
    assert intent is not None and intent.s_vlan == 321
    assert operation.operation_type is NetworkOperationType.ont_service_config
    assert dispatch is not None
    assert dispatch.queue is None
    assert head.phase is OntServiceConfigurationPhase.queued


def test_configuration_admission_does_not_gate_on_subscription_status(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """Staff-initiated config is desired-state staging, not service delivery.

    A suspended (or otherwise non-active) subscription must not block the
    operator Configure path — delivery authorization is a separate concern
    owned by radius_access_state.py / ppp_delivery_authorization.py.
    """
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    subscription.status = SubscriptionStatus.suspended
    db_session.commit()
    command = _configure_command(ont_id, idempotency_key="suspended-admission")

    outcome = configure_ont_service(db_session, command)

    head = db_session.get(OntServiceConfigurationHead, outcome.configuration_head_id)
    operation = db_session.get(NetworkOperation, outcome.operation_id)

    assert head is not None and head.assignment_id == assignment_id
    assert operation is not None
    assert head.phase is OntServiceConfigurationPhase.queued


def test_configuration_admission_rejects_unresolvable_subscription(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """A dangling/unresolvable subscription reference is a distinct failure
    from an inactive-but-resolvable one, and keeps its own error code."""
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.assignment_subscription_snapshot",
        lambda *_args, **_kwargs: None,
    )
    command = _configure_command(ont_id, idempotency_key="unresolvable-subscription")

    with pytest.raises(DomainError) as excinfo:
        configure_ont_service(db_session, command)

    assert excinfo.value.code.endswith("subscription_missing")


def test_customer_wifi_admission_saves_secret_and_queues_without_device_io(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.resolve_effective_ont_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("WiFi admission must not resolve WAN configuration")
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.active_primary_internet_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("WiFi admission must not read PPP service intent")
        ),
    )
    monkeypatch.setattr(
        (
            "app.services.network.ont_service_configuration."
            "ensure_active_wan_service_intent_in_transaction"
        ),
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("WiFi admission must not write PPP service intent")
        ),
    )
    command_id = uuid.uuid4()
    outcome = configure_customer_wifi(
        db_session,
        ConfigureCustomerWifiCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="customer:test",
                scope="customer:device:wifi",
                reason="Customer requested a WiFi credential update",
                idempotency_key="customer-wifi-admission",
            ),
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            change=CustomerWifiConfigurationChange(
                ssid="CustomerSSID",
                password="CustomerSecret123",
            ),
        ),
    )

    ont = db_session.get(OntUnit, ont_id)
    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == outcome.operation_id
        )
    )
    operation = db_session.get(NetworkOperation, outcome.operation_id)
    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.operation_id == outcome.operation_id
        )
    )
    status = get_latest_ont_configuration_section_delivery(
        db_session,
        ont_unit_id=ont_id,
        section=OntConfigurationSection.wifi,
    )
    assert outcome.assignment_id == assignment_id
    assert outcome.phase is OntServiceConfigurationPhase.queued
    assert ont is not None
    assert ont.desired_config["wifi"]["ssid"] == "CustomerSSID"
    assert ont.desired_config["wifi"]["password"] != "CustomerSecret123"
    assert str(ont.desired_config["wifi"]["password"]).startswith("enc:")
    assert dispatch is not None
    assert operation is not None
    assert "wan_intent_id" not in operation.input_payload
    assert "effective_customer_vlan" not in operation.input_payload
    assert revision is not None
    assert "wan_intent_id" not in revision.desired_change_evidence
    assert status is not None
    assert status.operation_id == outcome.operation_id
    assert status.phase is OntServiceConfigurationPhase.queued


def test_customer_wifi_admission_names_no_active_assignment(
    db_session,
    subscriber,
    subscription,
):
    """Zero active-assignment candidates gets its own code, distinct from
    ambiguity, a missing ONT row, and an inactive subscription."""
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    db_session.commit()
    command = _customer_wifi_command(
        subscriber_id, subscription_id, idempotency_key="no-assignment-candidate"
    )

    with pytest.raises(DomainError) as excinfo:
        configure_customer_wifi(db_session, command)

    assert excinfo.value.code.endswith("customer_active_assignment_required")
    assert excinfo.value.details["candidate_count"] == 0
    assert excinfo.value.details["subscriber_id"] == str(subscriber_id)
    assert excinfo.value.details["subscription_id"] == str(subscription_id)


def test_customer_wifi_admission_names_ambiguous_assignment(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """More than one active-assignment candidate is a distinct cause from
    having zero candidates."""
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    second_ont = OntUnit(
        serial_number=f"AMBIG-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(second_ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=second_ont.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            active=True,
        )
    )
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    db_session.commit()

    command = _customer_wifi_command(
        subscriber_id, subscription_id, idempotency_key="ambiguous-assignment"
    )

    with pytest.raises(DomainError) as excinfo:
        configure_customer_wifi(db_session, command)

    assert excinfo.value.code.endswith("customer_ambiguous_assignment")
    assert excinfo.value.details["candidate_count"] == 2
    assert str(assignment_id) in excinfo.value.details["candidate_assignment_ids"]


def test_customer_wifi_admission_names_a_missing_ont_row(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """A dangling assignment whose ONT row is gone is distinct from having
    no assignment candidate at all."""
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    db_session.commit()
    # The deployed schema prevents manufacturing this drift through ordinary
    # writes. Simulate the authoritative ONT lookup missing after the exact
    # assignment candidate has been selected.
    monkeypatch.setattr(db_session, "scalar", lambda *_args, **_kwargs: None)

    command = _customer_wifi_command(
        subscriber_id, subscription_id, idempotency_key="ont-row-missing"
    )

    with pytest.raises(DomainError) as excinfo:
        configure_customer_wifi(db_session, command)

    assert excinfo.value.code.endswith("customer_assigned_ont_missing")
    assert excinfo.value.details["ont_unit_id"] == str(ont_id)
    assert excinfo.value.details["assignment_id"] == str(assignment_id)


def test_customer_wifi_admission_names_assignment_inconsistency(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """The subscription-scoped candidate and the ONT's own active-assignment
    set disagreeing is distinct from every other admission failure."""
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="Customer",
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=subscriber.reseller_id,
    )
    other_offer = CatalogOffer(
        name="Other Offer",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add_all([other_subscriber, other_offer])
    db_session.flush()
    other_subscription = Subscription(
        subscriber_id=other_subscriber.id,
        offer_id=other_offer.id,
        status=SubscriptionStatus.pending,
    )
    db_session.add(other_subscription)
    db_session.flush()
    activate_test_subscription(db_session, other_subscription)
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    other_subscriber_id = other_subscriber.id
    other_subscription_id = other_subscription.id
    db_session.commit()

    command = _customer_wifi_command(
        subscriber_id, subscription_id, idempotency_key="assignment-inconsistent"
    )

    original_scalars = db_session.scalars
    scalar_calls = 0

    def inconsistent_assignments(*args, **kwargs):
        nonlocal scalar_calls
        scalar_calls += 1
        result = original_scalars(*args, **kwargs)
        if scalar_calls != 2:
            return result
        assignments = list(result)
        return iter(
            [
                *assignments,
                SimpleNamespace(
                    id=uuid.uuid4(),
                    ont_unit_id=ont_id,
                    subscriber_id=other_subscriber_id,
                    subscription_id=other_subscription_id,
                    active=True,
                ),
            ]
        )

    monkeypatch.setattr(db_session, "scalars", inconsistent_assignments)

    with pytest.raises(DomainError) as excinfo:
        configure_customer_wifi(db_session, command)

    assert excinfo.value.code.endswith("customer_assignment_inconsistent")
    assert excinfo.value.details["candidate_assignment_id"] == str(assignment_id)
    assert excinfo.value.details["ont_unit_id"] == str(ont_id)
    assert len(excinfo.value.details["ont_active_assignment_ids"]) == 2


def test_customer_wifi_admission_names_inactive_subscription(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    """An inactive subscription keeps the original, most-different-in-kind
    ``customer_subscription_not_found`` code -- it is not an assignment
    cardinality problem."""
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    subscription.status = SubscriptionStatus.suspended
    subscriber_id = subscriber.id
    subscription_id = subscription.id
    db_session.commit()

    command = _customer_wifi_command(
        subscriber_id, subscription_id, idempotency_key="inactive-subscription"
    )

    with pytest.raises(DomainError) as excinfo:
        configure_customer_wifi(db_session, command)

    assert excinfo.value.code.endswith("customer_subscription_not_found")
    assert excinfo.value.details["subscription_status"] == "suspended"
    assert excinfo.value.details["subscriber_id"] == str(subscriber_id)


def test_configuration_admission_rollback_leaves_no_partial_records(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.stage_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated dispatch staging failure")
        ),
    )

    with pytest.raises(RuntimeError, match="dispatch staging failure"):
        configure_ont_service(
            db_session,
            _configure_command(ont_id, idempotency_key="atomic-rollback"),
        )

    assert db_session.scalar(select(func.count(OntServiceConfigurationHead.id))) == 0
    assert (
        db_session.scalar(
            select(func.count(OntWanServiceInstance.id)).where(
                OntWanServiceInstance.ont_id == ont_id
            )
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count(NetworkOperation.id)).where(
                NetworkOperation.operation_type
                == NetworkOperationType.ont_service_config
            )
        )
        == 0
    )
    assert db_session.scalar(select(func.count(NetworkOperationDispatch.id))) == 0


def test_duplicate_replays_and_changed_input_with_same_key_conflicts(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    original = _configure_command(ont_id, idempotency_key="stable-request-key")
    first = configure_ont_service(db_session, original)

    replay = configure_ont_service(
        db_session,
        _configure_command(ont_id, idempotency_key="stable-request-key"),
    )

    assert replay.replayed is True
    assert replay.operation_id == first.operation_id
    assert replay.revision == first.revision
    assert (
        db_session.scalar(select(func.count(OntServiceConfigurationRevision.id))) == 1
    )
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            _configure_command(
                ont_id,
                idempotency_key="stable-request-key",
                section=OntConfigurationSection.lan,
                change=LanConfigurationChange(
                    gateway_ip="192.168.44.1",
                    block_prefix=IpBlockPrefix.p24,
                    dhcp_enabled=True,
                    dhcp_start="192.168.44.10",
                    dhcp_end="192.168.44.200",
                ),
            ),
        )

    assert exc_info.value.code.endswith(".idempotency_conflict")
    assert (
        db_session.scalar(select(func.count(OntServiceConfigurationRevision.id))) == 1
    )


def test_failed_revision_blocks_same_material_but_not_deliberate_next_revision(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    original = _configure_command(ont_id, idempotency_key="revision-one")
    first = configure_ont_service(db_session, original)
    head = db_session.get(OntServiceConfigurationHead, first.configuration_head_id)
    revision_one = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == 1,
        )
    )
    operation_one = db_session.get(NetworkOperation, first.operation_id)
    assert head is not None and revision_one is not None and operation_one is not None
    head.phase = OntServiceConfigurationPhase.failed
    revision_one.phase = OntServiceConfigurationPhase.failed
    operation_one.status = NetworkOperationStatus.failed
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            _configure_command(ont_id, idempotency_key="ordinary-same-material"),
        )
    assert exc_info.value.code.endswith(".repair_required")

    second = configure_ont_service(
        db_session,
        _configure_command(
            ont_id,
            idempotency_key="revision-two",
            section=OntConfigurationSection.lan,
            change=LanConfigurationChange(
                gateway_ip="192.168.55.1",
                block_prefix=IpBlockPrefix.p24,
                dhcp_enabled=True,
                dhcp_start="192.168.55.10",
                dhcp_end="192.168.55.200",
            ),
        ),
    )

    assert second.revision == 2
    assert second.operation_id != first.operation_id
    assert revision_one.phase is OntServiceConfigurationPhase.superseded


def test_lan_32_is_not_an_operator_lan_block_choice(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            _configure_command(
                ont_id,
                idempotency_key="lan-32-refused",
                section=OntConfigurationSection.lan,
                change=LanConfigurationChange(
                    gateway_ip="198.51.100.10",
                    block_prefix=IpBlockPrefix.p32,
                    dhcp_enabled=True,
                    dhcp_start="198.51.100.10",
                    dhcp_end="198.51.100.10",
                ),
            ),
        )

    assert exc_info.value.code.endswith(".invalid_lan_block_prefix")
    assert db_session.get(OntUnit, ont_id).desired_config in (None, {})


def test_lan_operator_prefix_is_converted_to_mask_and_queued(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    outcome = configure_ont_service(
        db_session,
        _configure_command(
            ont_id,
            idempotency_key="lan-29-queued",
            section=OntConfigurationSection.lan,
            change=LanConfigurationChange(
                gateway_ip="198.51.100.1",
                block_prefix=IpBlockPrefix.p29,
                dhcp_enabled=True,
                dhcp_start="198.51.100.2",
                dhcp_end="198.51.100.6",
            ),
        ),
    )

    desired_lan = db_session.get(OntUnit, ont_id).desired_config["lan"]
    assert desired_lan["block_prefix"] == "/29"
    assert desired_lan["subnet"] == "255.255.255.248"
    assert outcome.phase is OntServiceConfigurationPhase.queued


def test_lan_operator_prefix_does_not_require_catalog_entitlement(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    outcome = configure_ont_service(
        db_session,
        _configure_command(
            ont_id,
            idempotency_key="lan-entitlement-not-required",
            section=OntConfigurationSection.lan,
            change=LanConfigurationChange(
                gateway_ip="198.51.100.1",
                block_prefix=IpBlockPrefix.p30,
                dhcp_enabled=False,
                dhcp_start=None,
                dhcp_end=None,
            ),
        ),
    )

    desired_lan = db_session.get(OntUnit, ont_id).desired_config["lan"]
    assert desired_lan["block_prefix"] == "/30"
    assert desired_lan["subnet"] == "255.255.255.252"
    assert outcome.phase is OntServiceConfigurationPhase.queued


def test_lan_dhcp_admission_does_not_require_ppp_authoritative_credential(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": {"id": "test-pack"},
            "values": {"wan_mode": "pppoe", "wan_vlan": 321},
        },
    )

    outcome = configure_ont_service(
        db_session,
        _configure_command(
            ont_id,
            idempotency_key="lan-dhcp-no-ppp-credential",
            section=OntConfigurationSection.lan,
            change=LanConfigurationChange(
                gateway_ip="198.51.100.1",
                block_prefix=IpBlockPrefix.p29,
                dhcp_enabled=True,
                dhcp_start="198.51.100.2",
                dhcp_end="198.51.100.6",
            ),
        ),
    )

    desired_lan = db_session.get(OntUnit, ont_id).desired_config["lan"]
    assert desired_lan["dhcp_enabled"] is True
    assert desired_lan["dhcp_start"] == "198.51.100.2"
    assert desired_lan["dhcp_end"] == "198.51.100.6"
    assert outcome.phase is OntServiceConfigurationPhase.queued


def _lifecycle(
    db_session,
    ont: OntUnit,
    assignment: OntAssignment,
    *,
    phase: OntServiceConfigurationPhase,
    suffix: str,
    section: OntConfigurationSection = OntConfigurationSection.wan,
    desired_change_evidence: dict[str, object] | None = None,
) -> tuple[
    OntServiceConfigurationHead,
    OntServiceConfigurationRevision,
    NetworkOperation,
]:
    operation = _operation(db_session, ont, suffix)
    head = OntServiceConfigurationHead(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        current_revision=1,
        latest_operation_id=operation.id,
        phase=phase,
    )
    db_session.add(head)
    db_session.flush()
    revision = OntServiceConfigurationRevision(
        head_id=head.id,
        assignment_id=assignment.id,
        revision=1,
        section=section.value,
        command_fingerprint=suffix.ljust(64, "0")[:64],
        idempotency_key=f"key-{suffix}",
        desired_change_evidence=desired_change_evidence or {},
        operation_id=operation.id,
        phase=phase,
    )
    db_session.add(revision)
    db_session.flush()
    return head, revision, operation


def test_wifi_admission_records_redacted_delivery_evidence(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    command = _configure_command(
        ont_id,
        idempotency_key="wifi-redacted-delivery-evidence",
        section=OntConfigurationSection.wifi,
        change=WifiConfigurationChange(
            enabled=True,
            ssid="Subscriber WiFi",
            channel="auto",
            security_mode="WPA2-PSK",
            password="not-persisted-in-evidence",
        ),
    )

    outcome = configure_ont_service(db_session, command)

    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.operation_id == outcome.operation_id
        )
    )
    assert revision is not None
    assert revision.desired_change_evidence["wifi.password"] == "changed"
    assert "not-persisted-in-evidence" not in str(revision.desired_change_evidence)


def test_wifi_worker_passes_typed_redacted_delivery_scope(db_session, monkeypatch):
    ont = OntUnit(serial_number=f"WIFI-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="wifi-scope",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={
            "wifi.enabled": True,
            "wifi.ssid": "Subscriber WiFi",
            "wifi.channel": "auto",
            "wifi.security_mode": "WPA2-PSK",
            "wifi.password": "changed",
        },
    )
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session.commit()
    calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test WiFi delivery scope",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="wifi-delivery-scope",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.verified
    scope = calls[-1]["wifi_delivery_scope"]
    assert scope is not None
    assert scope.changed_fields == frozenset(
        {
            "wifi_enabled",
            "wifi_ssid",
            "wifi_channel",
            "wifi_security_mode",
            "wifi_password_ref",
        }
    )
    assert "Subscriber WiFi" not in str(scope)


def test_lan_worker_forces_write_and_reports_exact_readback_unavailable(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"LAN-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="lan-mask",
        section=OntConfigurationSection.lan,
        desired_change_evidence={
            "lan.ip": "198.51.100.1",
            "lan.subnet": "255.255.255.248",
            "lan.block_prefix": "/29",
            "lan.dhcp_enabled": True,
            "lan.dhcp_start": "198.51.100.2",
            "lan.dhcp_end": "198.51.100.6",
        },
    )
    db_session.commit()
    calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)
    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test forced LAN delivery",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="lan-delivery-scope",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert calls[-1]["force_lan_config"] is True
    assert outcome.phase is OntServiceConfigurationPhase.delivered_unverified
    assert revision.verified_at is None
    assert head.failure_code == "exact_lan_readback_unavailable"
    assert operation.status is NetworkOperationStatus.succeeded


def test_lan_worker_verifies_when_exact_lan_readback_is_exposed(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"LANOK-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="lan-exact-readback",
        section=OntConfigurationSection.lan,
        desired_change_evidence={
            "lan.ip": "198.51.100.1",
            "lan.subnet": "255.255.255.248",
            "lan.block_prefix": "/29",
            "lan.dhcp_enabled": True,
            "lan.dhcp_start": "198.51.100.2",
            "lan.dhcp_end": "198.51.100.6",
        },
    )
    db_session.commit()

    def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
            observed_after=SimpleNamespace(
                acs=SimpleNamespace(
                    acs_observed_lan_gateway_ip="198.51.100.1",
                    acs_observed_dhcp_subnet_mask="255.255.255.248",
                    acs_observed_dhcp_enabled=True,
                    acs_observed_dhcp_pool_min="198.51.100.2",
                    acs_observed_dhcp_pool_max="198.51.100.6",
                )
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test exact LAN readback",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="lan-exact-readback",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.verified
    assert revision.verified_at is not None
    assert head.failure_code is None
    assert operation.status is NetworkOperationStatus.succeeded


def test_lan_worker_ignores_unrelated_ppp_residual_drift_after_delivery(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"LANPPP-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="lan-ppp-residual",
        section=OntConfigurationSection.lan,
        desired_change_evidence={
            "lan.ip": "198.51.100.1",
            "lan.subnet": "255.255.255.248",
            "lan.block_prefix": "/29",
            "lan.dhcp_enabled": True,
            "lan.dhcp_start": "198.51.100.2",
            "lan.dhcp_end": "198.51.100.6",
        },
    )
    db_session.commit()

    def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            success=True,
            sync_status="out_of_sync",
            drift_after=(
                SimpleNamespace(field="ppp_delivery[AcsSetPppoe]", surface="acs"),
            ),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test forced LAN delivery",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="lan-delivery-with-ppp-residual",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.delivered_unverified
    assert head.failure_code == "exact_lan_readback_unavailable"
    assert operation.status is NetworkOperationStatus.succeeded


def test_lan_worker_treats_acs_connection_request_failure_as_pending_drain(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"LANCR-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="lan-cr-pending",
        section=OntConfigurationSection.lan,
        desired_change_evidence={
            "lan.ip": "198.51.100.1",
            "lan.subnet": "255.255.255.248",
            "lan.block_prefix": "/29",
            "lan.dhcp_enabled": True,
            "lan.dhcp_start": "198.51.100.2",
            "lan.dhcp_end": "198.51.100.6",
        },
    )
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()

    def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=("lan.dhcp_enabled",),
            failure=SimpleNamespace(
                reason="acs_cr_failed",
                message=(
                    "setParameterValues queued but Connection Request failed: "
                    "Connection request error: Unexpected status code 401."
                ),
                evidence=None,
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test LAN CR pending",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="lan-cr-pending",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.readback_pending
    assert "accepted by ACS" in outcome.message
    assert head.waiting_reason == "awaiting_acs_task_drain"
    assert head.failure_code is None
    assert operation.status is NetworkOperationStatus.waiting


def test_lan_cr_drain_exhaustion_reports_acs_blocker(db_session, monkeypatch):
    ont = OntUnit(serial_number=f"LANCRWAIT-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.readback_pending,
        suffix="lan-cr-drain-exhausted",
        section=OntConfigurationSection.lan,
        desired_change_evidence={
            "lan.ip": "198.51.100.1",
            "lan.subnet": "255.255.255.248",
            "lan.block_prefix": "/29",
            "lan.dhcp_enabled": True,
            "lan.dhcp_start": "198.51.100.2",
            "lan.dhcp_end": "198.51.100.6",
        },
    )
    head.waiting_reason = "awaiting_acs_task_drain"
    operation.status = NetworkOperationStatus.waiting
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()

    def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=("lan.dhcp_enabled",),
            failure=SimpleNamespace(
                reason="blocked_out_of_sync",
                message=(
                    "ONT is out_of_sync (last_error: setParameterValues queued "
                    "but Connection Request failed: Device is offline. Force OLT "
                    "`ont reset` to drain.)"
                ),
                evidence=None,
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test LAN CR drain exhausted",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="lan-cr-drain-exhausted",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            verification_attempt=3,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.failed
    assert "GenieACS Connection Request" in outcome.message
    assert head.failure_code == "acs_cr_failed"
    assert head.waiting_reason is None
    assert operation.status is NetworkOperationStatus.failed


def _assert_cr_failure_absorbed_as_pending_drain(
    db_session,
    monkeypatch,
    *,
    section: OntConfigurationSection,
    suffix: str,
    desired_change_evidence: dict[str, object],
):
    """Shared proof for the section-agnostic ``acs_cr_failed`` absorption
    path: a queued-but-CR-failed GenieACS delivery must land in
    ``readback_pending`` with structured queued-task evidence recorded,
    regardless of which section (WiFi/WAN/management) issued the write.
    """
    ont = OntUnit(
        serial_number=f"{suffix.upper()}-{uuid.uuid4().hex[:10]}", is_active=True
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix=suffix,
        section=section,
        desired_change_evidence=desired_change_evidence,
    )
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()

    failure_message = (
        "setParameterValues queued but Connection Request failed: "
        "Connection request error: Unexpected status code 401."
    )

    def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=(f"{section.value}.changed_field",),
            failure=SimpleNamespace(
                reason="acs_cr_failed",
                message=failure_message,
                evidence=None,
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason=f"test {section.value} CR pending",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key=f"{suffix}-cr-pending",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.readback_pending
    assert "accepted by ACS" in outcome.message
    assert head.waiting_reason == "awaiting_acs_task_drain"
    assert head.failure_code is None
    assert operation.status is NetworkOperationStatus.waiting

    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == operation_id,
            NetworkOperationDispatch.dispatch_key == "verify:1",
        )
    )
    assert dispatch is not None

    event = db_session.scalar(
        select(OntProvisioningEvent)
        .where(OntProvisioningEvent.configuration_head_id == head_id)
        .order_by(OntProvisioningEvent.created_at.desc())
    )
    assert event is not None
    assert event.status is OntProvisioningEventStatus.waiting
    assert event.event_data is not None
    assert event.event_data["acs_task_queued_despite_cr_failure"] is True
    assert event.event_data["section"] == section.value
    assert event.event_data["failure_reason"] == "acs_cr_failed"
    assert event.event_data["failure_message"] == failure_message
    assert (
        event.event_data["confirmation_path"]
        == "readback_only_verification_after_fresh_inform"
    )
    return ont_id, head_id, operation_id, revision_number


def test_wifi_worker_treats_acs_connection_request_failure_as_pending_drain(
    db_session, monkeypatch
):
    _assert_cr_failure_absorbed_as_pending_drain(
        db_session,
        monkeypatch,
        section=OntConfigurationSection.wifi,
        suffix="wifi-cr-pending",
        desired_change_evidence={"wifi.ssid": "NewSSID"},
    )


def test_wan_worker_treats_acs_connection_request_failure_as_pending_drain(
    db_session, monkeypatch
):
    _assert_cr_failure_absorbed_as_pending_drain(
        db_session,
        monkeypatch,
        section=OntConfigurationSection.wan,
        suffix="wan-cr-pending",
        desired_change_evidence={"wan.vlan": 42},
    )


def test_management_worker_treats_acs_connection_request_failure_as_pending_drain(
    db_session, monkeypatch
):
    _assert_cr_failure_absorbed_as_pending_drain(
        db_session,
        monkeypatch,
        section=OntConfigurationSection.management,
        suffix="mgmt-cr-pending",
        desired_change_evidence={"management.acs_url": "https://acs.example/"},
    )


def test_acs_cr_failure_retry_dispatch_is_readback_only_never_a_second_write(
    db_session, monkeypatch
):
    """Critical convergence proof: once a non-LAN section's ``acs_cr_failed``
    delivery is absorbed into ``readback_pending``, the automatically staged
    retry (``verify:1``) must re-enter ``_execution_locked`` with
    ``readback_only=True`` — the exact flag
    ``tests/test_reconcile_core.py``'s structural write-incapability tests
    (``test_readback_only_never_calls_set_parameter_values`` and
    ``test_readback_only_never_reaches_apply_plan_even_with_genuine_drift``)
    prove makes a second ``setParameterValues`` structurally unreachable in
    ``reconcile_ont`` itself. This test proves the ONT service-configuration
    worker actually passes that flag on the automatic retry; the two tests
    above prove what that flag guarantees once passed.
    """
    ont = OntUnit(serial_number=f"WIFIRETRY-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.readback_pending,
        suffix="wifi-cr-retry",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={"wifi.ssid": "NewSSID"},
    )
    head.waiting_reason = "awaiting_acs_task_drain"
    operation.status = NetworkOperationStatus.waiting
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()

    reconcile_calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        reconcile_calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test WiFi CR retry is readback-only",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="wifi-cr-retry",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            verification_attempt=1,
        ),
    )

    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["readback_only"] is True
    assert outcome.phase is OntServiceConfigurationPhase.verified


def test_auto_triggered_verify_operation_execution_is_readback_only_and_converges(
    db_session, monkeypatch
):
    """The automatic post-Inform trigger (``app.services.tr069.receive_inform``
    -> ``verify_ont_service_configuration_readback``) queues an
    ``ont_service_config_verify.v1`` dispatch, which the Celery
    ``verify_readback`` task executes with ``force_readback_only=True`` fixed
    and never caller-overridable (see
    ``tests/architecture/test_ont_service_configuration_boundary.py::
    test_verification_dispatch_is_structurally_readback_only``). This proves
    that when that queued operation actually executes, ``_execution_locked``
    computes ``readback_only=True`` unconditionally (regardless of
    ``verification_attempt``) and converges -- so the automated recovery path
    this Inform trigger completes never risks a second ``setParameterValues``,
    for a non-LAN section (WiFi), mirroring the manual recovery run for Jabi.
    """
    ont = OntUnit(
        serial_number=f"WIFIAUTOVERIFY-{uuid.uuid4().hex[:10]}", is_active=True
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="wifi-auto-verify",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={"wifi.ssid": "NewSSID"},
    )
    head.waiting_reason = "awaiting_dispatch"
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()

    reconcile_calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        reconcile_calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session_adapter.release_read_transaction(db_session)

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test auto-triggered verify is readback-only",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="wifi-auto-verify",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            verification_attempt=0,
            explicit_repair=True,
            force_readback_only=True,
        ),
    )

    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["readback_only"] is True
    assert outcome.phase is OntServiceConfigurationPhase.verified


def test_projection_surfaces_interrupted_latest_operation_as_retryable(db_session):
    ont = OntUnit(serial_number=f"INTERRUPT-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, _revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="interrupted-projection",
    )
    operation.status = NetworkOperationStatus.failed
    operation.error = "Command execution state is unknown. Review current device state before retrying."
    operation.output_payload = {
        "message": "Command execution state is unknown. Review current device state before retrying."
    }
    db_session.flush()

    projection = get_ont_service_configuration_projection(
        db_session, ont_unit_id=ont.id
    )

    assert head.phase is OntServiceConfigurationPhase.queued
    assert projection.phase is OntServiceConfigurationPhase.failed
    assert projection.waiting_reason is None
    assert projection.failure_code == "operation_interrupted"
    assert "Review current device state" in str(projection.failure_message)
    assert (
        projection.next_action is OntConfigurationNextAction.retry_current_configuration
    )


def test_retry_accepts_interrupted_current_operation(db_session):
    ont = OntUnit(serial_number=f"RETRYINT-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, old_operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="interrupted-retry",
    )
    old_operation.status = NetworkOperationStatus.failed
    old_operation.error = (
        "Worker acknowledgement became stale before command completion."
    )
    ont_id = ont.id
    head_id = head.id
    revision_number = revision.revision
    old_operation_id = old_operation.id
    db_session.commit()
    command_id = uuid.uuid4()

    outcome = retry_ont_service_configuration(
        db_session,
        RetryOntServiceConfigurationCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="test:operator",
                scope="network:ont:write",
                reason="reviewed interrupted worker recovery",
                idempotency_key="retry-interrupted-current-revision",
            ),
            ont_unit_id=ont_id,
            expected_head_id=head_id,
            expected_revision=revision_number,
        ),
    )

    refreshed_head = db_session.get(OntServiceConfigurationHead, head_id)
    refreshed_revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head_id,
            OntServiceConfigurationRevision.revision == revision_number,
        )
    )
    assert outcome.operation_id != old_operation_id
    assert outcome.phase is OntServiceConfigurationPhase.queued
    assert refreshed_head.latest_operation_id == outcome.operation_id
    assert refreshed_revision.operation_id == outcome.operation_id
    assert (
        db_session.get(NetworkOperation, old_operation_id).status
        is NetworkOperationStatus.failed
    )


def test_inventory_retirement_clears_only_current_projection_and_keeps_history(
    db_session, monkeypatch
):
    emitted: list[object] = []
    monkeypatch.setattr(
        "app.services.network.reconcile.lifecycle.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    ont = OntUnit(serial_number=f"RETIRE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.failed,
        suffix="retire",
    )
    event = OntProvisioningEvent(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        configuration_head_id=head.id,
        configuration_revision=revision.revision,
        operation_id=operation.id,
        step_name="ont_service_configuration",
        action="configuration_phase_changed",
        status=OntProvisioningEventStatus.failed,
        message="historical failure",
    )
    db_session.add(event)
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "current assignment failed"
    ont.reconcile_assignment_id = assignment.id
    ont.reconcile_configuration_head_id = head.id
    ont.reconcile_desired_revision = 1
    ont.reconcile_operation_id = operation.id
    db_session.flush()

    outcome = retire_ont_reconcile_projection_for_inventory(
        db_session,
        RetireOntReconcileProjectionForInventory(
            ont_unit_id=ont.id,
            assignment_ids=(assignment.id,),
            actor="test:inventory",
            reason="returned_to_inventory",
        ),
    )

    assert outcome.retired_head_ids == (head.id,)
    assert head.phase is OntServiceConfigurationPhase.retired
    assert revision.phase is OntServiceConfigurationPhase.retired
    assert ont.sync_status is OntSyncStatus.synced
    assert ont.last_error is None
    assert ont.reconcile_assignment_id is None
    assert db_session.get(OntProvisioningEvent, event.id) is event
    assert emitted


def test_current_projection_excludes_legacy_failure(db_session):
    ont = OntUnit(serial_number=f"REUSE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    new_assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(new_assignment)
    db_session.flush()
    new_head, new_revision, new_operation = _lifecycle(
        db_session,
        ont,
        new_assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="new",
    )
    db_session.add_all(
        (
            OntProvisioningEvent(
                ont_unit_id=ont.id,
                assignment_id=None,
                configuration_head_id=None,
                configuration_revision=None,
                operation_id=None,
                step_name="ont_service_configuration",
                action="configuration_phase_changed",
                status=OntProvisioningEventStatus.failed,
                message="old assignment failure",
            ),
            OntProvisioningEvent(
                ont_unit_id=ont.id,
                assignment_id=new_assignment.id,
                configuration_head_id=new_head.id,
                configuration_revision=new_revision.revision,
                operation_id=new_operation.id,
                step_name="ont_service_configuration",
                action="configuration_phase_changed",
                status=OntProvisioningEventStatus.waiting,
                message="current assignment queued",
            ),
        )
    )
    db_session.flush()

    projection = get_ont_service_configuration_projection(
        db_session, ont_unit_id=ont.id
    )

    assert projection.assignment_id == new_assignment.id
    assert projection.configuration_head_id == new_head.id
    assert projection.phase is OntServiceConfigurationPhase.queued
    assert [item.message for item in projection.current_events] == [
        "current assignment queued"
    ]
    assert "old assignment failure" in {
        item.message for item in projection.historical_events
    }
    assert projection.failure_message is None


def test_configure_without_active_assignment_fails_before_mutation(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"UNASSIGNED-{uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    ont_id = ont.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration._command_fingerprint",
        lambda _command: "f" * 64,
    )
    command_id = uuid.uuid4()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            ConfigureOntServiceCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor="test:operator",
                    scope="network:ont:write",
                    reason="test unassigned refusal",
                    idempotency_key="test-unassigned",
                ),
                ont_unit_id=ont_id,
                permission_granted=True,
                section=OntConfigurationSection.wan,
                change=WanConfigurationChange(
                    mode="pppoe",
                    ip_protocol="ipv4",
                    static_ip=None,
                    static_subnet=None,
                    static_gateway=None,
                    static_dns=None,
                ),
            ),
        )

    assert exc_info.value.code.endswith(".active_assignment_required")
    assert db_session.scalar(select(func.count(OntServiceConfigurationHead.id))) == 0
    assert (
        db_session.scalar(
            select(func.count(NetworkOperation.id)).where(
                NetworkOperation.operation_type
                == NetworkOperationType.ont_service_config
            )
        )
        == 0
    )


def test_stale_worker_refuses_released_assignment_before_device_call(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"STALE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="stale",
    )
    assignment.active = False
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont",
        lambda *_args, **_kwargs: pytest.fail("stale worker contacted device"),
    )
    command_id = uuid.uuid4()

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test stale worker refusal",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="stale-worker",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.stale is True
    assert outcome.executed is False
    assert outcome.phase is OntServiceConfigurationPhase.superseded
    assert (
        db_session.get(NetworkOperation, operation_id).status
        is NetworkOperationStatus.canceled
    )


def test_readback_pending_is_not_verified_until_exact_revision_readback_succeeds(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"READBACK-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="readback",
    )
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": 1,
    }
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    db_session.commit()
    calls: list[dict] = []

    def pending_reconcile(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=("wan.mode",),
            failure=SimpleNamespace(
                reason="verification_mismatch",
                message="Device write queued; waiting for a fresh observation.",
                evidence={"readback_pending": True},
            ),
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont", pending_reconcile
    )
    first_command_id = uuid.uuid4()
    pending = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="initial delivery",
                command_id=first_command_id,
                correlation_id=operation_id,
                idempotency_key="readback-initial",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=1,
        ),
    )

    assert pending.phase is OntServiceConfigurationPhase.readback_pending
    assert calls[-1]["readback_only"] is False

    def verified_reconcile(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont", verified_reconcile
    )
    verify_command_id = uuid.uuid4()
    verified = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="fresh readback",
                command_id=verify_command_id,
                correlation_id=operation_id,
                idempotency_key="readback-attempt-one",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=1,
            verification_attempt=1,
        ),
    )

    assert verified.phase is OntServiceConfigurationPhase.verified
    assert calls[-1]["readback_only"] is True
    exact_revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head_id,
            OntServiceConfigurationRevision.revision == 1,
        )
    )
    assert exact_revision is not None
    assert exact_revision.phase is OntServiceConfigurationPhase.verified
    assert exact_revision.verified_at is not None
    assert (
        db_session.get(NetworkOperation, operation_id).status
        is NetworkOperationStatus.succeeded
    )


def test_failed_current_revision_requires_explicit_retry_command(db_session):
    ont = OntUnit(serial_number=f"RETRY-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, old_operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.failed,
        suffix="retry",
    )
    old_operation.status = NetworkOperationStatus.failed
    ont_id = ont.id
    head_id = head.id
    revision_number = revision.revision
    old_operation_id = old_operation.id
    db_session.commit()
    command_id = uuid.uuid4()

    outcome = retry_ont_service_configuration(
        db_session,
        RetryOntServiceConfigurationCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="test:operator",
                scope="network:ont:write",
                reason="reviewed current revision repair",
                idempotency_key="retry-current-revision",
            ),
            ont_unit_id=ont_id,
            expected_head_id=head_id,
            expected_revision=revision_number,
        ),
    )

    assert outcome.revision == 1
    assert outcome.operation_id != old_operation_id
    assert outcome.phase is OntServiceConfigurationPhase.queued
    assert (
        db_session.get(OntServiceConfigurationHead, head_id).latest_operation_id
        == outcome.operation_id
    )


# ── VerifyOntServiceConfigurationReadbackCommand ────────────────────────────


def _verify_setup(
    db_session,
    *,
    suffix: str,
    section: OntConfigurationSection = OntConfigurationSection.wifi,
    desired_change_evidence: dict[str, object] | None = None,
    fresh_inform: bool = True,
):
    ont = OntUnit(serial_number=f"VERIFY-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.failed,
        suffix=suffix,
        section=section,
        desired_change_evidence=desired_change_evidence
        or {"wifi.ssid": "Auto Computer"},
    )
    operation.status = NetworkOperationStatus.failed
    failed_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    operation.completed_at = failed_at
    ont.acs_last_inform_at = (
        failed_at + timedelta(minutes=10)
        if fresh_inform
        else failed_at - timedelta(minutes=10)
    )
    db_session.flush()
    # No commit here (unlike earlier helpers that owned their own commit):
    # ``execute_owner_command`` requires a transaction-free session at
    # entry, and this ORM session expires instances on commit. Committing
    # here and then reading ``.id``/``.revision`` back off the returned,
    # now-expired instances would trigger an implicit SQLAlchemy autobegin
    # refresh query -- leaving a *new*, unrelated transaction open by the
    # time a test calls into ``verify_ont_service_configuration_readback``.
    # Callers capture the plain ids they need and commit themselves,
    # exactly once, immediately before invoking the owner command.
    return ont, assignment, head, revision, operation


def _verify_command(
    *, ont_id, head_id, revision_number, failed_operation_id, idempotency_key: str
) -> VerifyOntServiceConfigurationReadbackCommand:
    command_id = uuid.uuid4()
    return VerifyOntServiceConfigurationReadbackCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="test:operator",
            scope="network:ont:write",
            reason="reviewed readback verification of a confirmed-delivered failure",
            idempotency_key=idempotency_key,
        ),
        ont_unit_id=ont_id,
        expected_head_id=head_id,
        expected_revision=revision_number,
        failed_operation_id=failed_operation_id,
    )


def test_verify_rejects_when_head_is_not_failed(db_session):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session, suffix="not-failed"
    )
    head.phase = OntServiceConfigurationPhase.queued
    ont_id, head_id, revision_number, operation_id = (
        ont.id,
        head.id,
        revision.revision,
        operation.id,
    )
    db_session.commit()

    with pytest.raises(DomainError) as excinfo:
        verify_ont_service_configuration_readback(
            db_session,
            _verify_command(
                ont_id=ont_id,
                head_id=head_id,
                revision_number=revision_number,
                failed_operation_id=operation_id,
                idempotency_key="verify-not-failed",
            ),
        )
    assert "verification_not_eligible" in str(excinfo.value.code)


def test_verify_rejects_operation_id_mismatch(db_session):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session, suffix="mismatch"
    )
    other_operation = _operation(db_session, ont, "mismatch-other")
    ont_id, head_id, revision_number, other_operation_id = (
        ont.id,
        head.id,
        revision.revision,
        other_operation.id,
    )
    db_session.commit()

    with pytest.raises(DomainError) as excinfo:
        verify_ont_service_configuration_readback(
            db_session,
            _verify_command(
                ont_id=ont_id,
                head_id=head_id,
                revision_number=revision_number,
                failed_operation_id=other_operation_id,
                idempotency_key="verify-mismatch",
            ),
        )
    assert "verification_operation_mismatch" in str(excinfo.value.code)


def test_verify_reports_still_unverified_without_a_fresh_inform(db_session):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session, suffix="stale-inform", fresh_inform=False
    )
    ont_id, head_id, revision_number, operation_id = (
        ont.id,
        head.id,
        revision.revision,
        operation.id,
    )
    db_session.commit()

    outcome = verify_ont_service_configuration_readback(
        db_session,
        _verify_command(
            ont_id=ont_id,
            head_id=head_id,
            revision_number=revision_number,
            failed_operation_id=operation_id,
            idempotency_key="verify-stale-inform",
        ),
    )

    assert "still_unverified" in outcome.message
    assert outcome.phase is OntServiceConfigurationPhase.failed
    # No new operation was created: the referenced failure is untouched.
    assert (
        db_session.get(OntServiceConfigurationHead, head_id).latest_operation_id
        == operation_id
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(NetworkOperation)
            .where(NetworkOperation.target_id == ont_id)
        )
        == 1
    )


def test_verify_ssid_convergence_marks_verified_through_reconcile(
    db_session, monkeypatch
):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session,
        suffix="converged",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={"wifi.ssid": "Auto Computer"},
    )
    ont_id, head_id, revision_number = ont.id, head.id, revision.revision
    failed_operation_id = operation.id
    db_session.commit()

    # Capture plain values BEFORE the verify call, read back through the
    # session rather than the identity-mapped ``operation`` instance: a
    # comparison against ``operation`` itself after the call would be
    # tautological (``x == x``, always true, proving nothing), and the
    # pre-flush Python value on ``operation`` is tz-aware while the value
    # ``db_session.get(...)`` later returns is the DB-normalized (tz-naive)
    # representation — reading both sides through the session keeps the
    # comparison meaningful instead of comparing aware to naive.
    refreshed_operation = db_session.get(NetworkOperation, failed_operation_id)
    original_completed_at = refreshed_operation.completed_at
    original_status = refreshed_operation.status
    original_error = refreshed_operation.error
    original_output_payload = refreshed_operation.output_payload
    db_session.commit()

    outcome = verify_ont_service_configuration_readback(
        db_session,
        _verify_command(
            ont_id=ont_id,
            head_id=head_id,
            revision_number=revision_number,
            failed_operation_id=failed_operation_id,
            idempotency_key="verify-converged",
        ),
    )
    assert outcome.phase is OntServiceConfigurationPhase.queued
    new_operation_id = outcome.operation_id
    assert new_operation_id != failed_operation_id
    assert (
        db_session.get(NetworkOperation, new_operation_id).redrive_of_id
        == failed_operation_id
    )
    # The read above expired this session's instances and autobegan a fresh
    # transaction on top of the completed verify command's own commit; clear
    # it before the next owner command, which requires a transaction-free
    # session at entry.
    db_session.commit()

    reconcile_calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        reconcile_calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)

    execute_outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test readback verification execution",
                command_id=uuid.uuid4(),
                correlation_id=new_operation_id,
                idempotency_key="verify-converged-execute",
            ),
            ont_unit_id=ont_id,
            operation_id=new_operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            force_readback_only=True,
        ),
    )

    assert execute_outcome.phase is OntServiceConfigurationPhase.verified
    assert reconcile_calls[-1]["readback_only"] is True
    # The reconcile lifecycle owner decides sync_status; this call proves the
    # verification path went THROUGH ``reconcile_ont`` rather than assigning
    # ``OntUnit.sync_status`` itself.
    assert "lifecycle_binding" in reconcile_calls[-1]

    # The original failed operation is preserved untouched — compared
    # against plain values captured before the verify call, not the same
    # identity-mapped ORM instance mutated in place.
    db_session.expire_all()
    original = db_session.get(NetworkOperation, failed_operation_id)
    assert original.status is NetworkOperationStatus.failed
    assert original.status == original_status
    assert original.completed_at == original_completed_at
    assert original.error == original_error
    assert original.output_payload == original_output_payload


def test_verify_residual_drift_reports_failed_not_verified(db_session, monkeypatch):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session,
        suffix="mismatch-ssid",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={"wifi.ssid": "Auto Computer"},
    )
    ont_id, head_id, revision_number = ont.id, head.id, revision.revision
    failed_operation_id = operation.id
    db_session.commit()

    outcome = verify_ont_service_configuration_readback(
        db_session,
        _verify_command(
            ont_id=ont_id,
            head_id=head_id,
            revision_number=revision_number,
            failed_operation_id=failed_operation_id,
            idempotency_key="verify-ssid-mismatch",
        ),
    )
    new_operation_id = outcome.operation_id

    from app.services.network.reconcile import ReconcileFailure, ReconcileFailureReason

    def reconciled(*_args, **kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=(SimpleNamespace(field="wifi_ssid"),),
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.VERIFICATION_MISMATCH,
                message="SSID still does not match.",
                evidence={},
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)

    execute_outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test readback verification execution",
                command_id=uuid.uuid4(),
                correlation_id=new_operation_id,
                idempotency_key="verify-ssid-mismatch-execute",
            ),
            ont_unit_id=ont_id,
            operation_id=new_operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            force_readback_only=True,
        ),
    )

    assert execute_outcome.phase is OntServiceConfigurationPhase.failed


def test_verify_readback_pending_reports_failed_without_apply_redispatch(
    db_session, monkeypatch
):
    """Required-fix regression test: a readback-only verification that gets
    ``readback_pending`` evidence must NOT auto re-dispatch
    ``ont_service_config_apply_v1`` (the shared dispatch key parsing that
    only keeps that hop readback-only is exactly the "flag deep in shared
    code" shape this feature was built to avoid depending on) and must not
    let a later ``BLOCKED_OUT_OF_SYNC`` refusal overwrite this diagnosis.
    """
    ont, assignment, head, revision, operation = _verify_setup(
        db_session,
        suffix="readback-pending",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={"wifi.ssid": "Auto Computer"},
    )
    ont_id, head_id, revision_number = ont.id, head.id, revision.revision
    failed_operation_id = operation.id
    db_session.commit()

    outcome = verify_ont_service_configuration_readback(
        db_session,
        _verify_command(
            ont_id=ont_id,
            head_id=head_id,
            revision_number=revision_number,
            failed_operation_id=failed_operation_id,
            idempotency_key="verify-readback-pending",
        ),
    )
    new_operation_id = outcome.operation_id

    from app.services.network.reconcile import ReconcileFailure, ReconcileFailureReason

    def reconciled(*_args, **kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=(SimpleNamespace(field="wifi_ssid"),),
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.VERIFICATION_MISMATCH,
                message="Current revision is still awaiting device readback.",
                evidence={"readback_pending": True, "drift_fields": ["acs:wifi_ssid"]},
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)

    execute_outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test readback verification execution",
                command_id=uuid.uuid4(),
                correlation_id=new_operation_id,
                idempotency_key="verify-readback-pending-execute",
            ),
            ont_unit_id=ont_id,
            operation_id=new_operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            force_readback_only=True,
        ),
    )

    assert execute_outcome.phase is OntServiceConfigurationPhase.failed
    assert "still_unverified" in execute_outcome.message
    refreshed_head = db_session.get(OntServiceConfigurationHead, head_id)
    assert refreshed_head.failure_code == "verification_mismatch"
    # No new dispatch was staged for this operation — no ``verify:1`` hop
    # onto ``ont_service_config_apply_v1``.
    dispatch_keys = db_session.scalars(
        select(NetworkOperationDispatch.dispatch_key).where(
            NetworkOperationDispatch.operation_id == new_operation_id
        )
    ).all()
    assert all(not key.startswith("verify:") for key in dispatch_keys)


def test_verify_write_only_password_reports_delivered_unverified(
    db_session, monkeypatch
):
    ont, assignment, head, revision, operation = _verify_setup(
        db_session,
        suffix="wifi-password",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={
            "wifi.ssid": "Auto Computer",
            "wifi.password": "changed",
        },
    )
    ont_id, head_id, revision_number = ont.id, head.id, revision.revision
    failed_operation_id = operation.id
    db_session.commit()

    outcome = verify_ont_service_configuration_readback(
        db_session,
        _verify_command(
            ont_id=ont_id,
            head_id=head_id,
            revision_number=revision_number,
            failed_operation_id=failed_operation_id,
            idempotency_key="verify-password",
        ),
    )
    new_operation_id = outcome.operation_id

    def reconciled(*_args, **kwargs):
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)

    execute_outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test readback verification execution",
                command_id=uuid.uuid4(),
                correlation_id=new_operation_id,
                idempotency_key="verify-password-execute",
            ),
            ont_unit_id=ont_id,
            operation_id=new_operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
            force_readback_only=True,
        ),
    )

    assert execute_outcome.phase is OntServiceConfigurationPhase.delivered_unverified
    refreshed_head = db_session.get(OntServiceConfigurationHead, head_id)
    assert refreshed_head.failure_code == "wifi_password_write_only_unverifiable"
