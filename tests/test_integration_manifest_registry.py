from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    RuntimeManifest,
)
from app.services.integrations.registry import (
    connector_definition,
    connector_definitions,
    definitions_for_capability,
    discover_connectors,
    pinned_connector_definition,
    supported_connector_definitions,
)
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
    RunnerRegistry,
)
from app.services.web_integrations import build_marketplace_data

EXPECTED_MARKETPLACE = {
    "fiber.inquiry.http": ("Fiber Website Inquiry", "1.0.0", "messaging"),
    "lead.capture.http": ("Lead Capture Webhook", "1.0.0", "sales"),
    "whatsapp": ("WhatsApp", "1.1.0", "messaging"),
    "nextcloud.talk": ("Nextcloud Talk", "1.0.0", "messaging"),
    "meta.social": ("Meta Social Inbox", "1.1.0", "messaging"),
    "paystack": ("Paystack", "1.0.1", "payment"),
    "flutterwave": ("Flutterwave", "1.0.0", "payment"),
    "3cx": ("3CX", "1.0.0", "voice"),
    "freepbx": ("FreePBX", "1.0.0", "voice"),
}


def test_explicit_registry_preserves_marketplace_catalogue_parity() -> None:
    entries = {entry.key: entry for entry in discover_connectors()}

    assert set(entries) == set(EXPECTED_MARKETPLACE)
    for key, (name, version, connector_type) in EXPECTED_MARKETPLACE.items():
        entry = entries[key]
        assert (entry.name, entry.version, entry.connector_type) == (
            name,
            version,
            connector_type,
        )

    assert tuple(definition.key for definition in connector_definitions()) == (
        "fiber.inquiry.http",
        "lead.capture.http",
        # Inbound-only and deliberately catalogue-invisible: an operator does
        # not "install" the Integrator from the marketplace, so it is absent
        # from EXPECTED_MARKETPLACE above but present in the definition order.
        "dotmac.integrator.http",
        "webhook.http",
        "dotmac.crm",
        "whatsapp",
        "nextcloud.talk",
        "meta.social",
        "dotmac.erp",
        "paystack",
        "flutterwave",
        "3cx",
        "freepbx",
    )


def test_marketplace_projection_exposes_all_available_cards(db_session) -> None:
    data = build_marketplace_data(db_session)

    assert data["stats"] == {"available": 9, "installed": 0, "updates": 0}
    assert {card["key"] for card in data["marketplace_cards"]} == set(
        EXPECTED_MARKETPLACE
    )


def test_manifest_digest_is_deterministic_and_capabilities_are_queryable() -> None:
    paystack = connector_definition("PAYSTACK")
    assert paystack is not None
    assert len(paystack.digest) == 64
    assert paystack.digest == paystack.model_copy().digest
    assert paystack.capability("payments.intent.v1") is not None
    assert {
        definition.key
        for definition in definitions_for_capability("payments.intent.v1")
    } == {"paystack", "flutterwave"}
    assert {
        definition.key
        for definition in definitions_for_capability("crm.ticket_observation.v1")
    } == {"dotmac.crm"}
    assert {
        definition.key for definition in definitions_for_capability("events.deliver.v1")
    } == {"webhook.http"}


def test_exact_historical_paystack_pins_remain_bounded_and_executable() -> None:
    supported_paystack = [
        definition
        for definition in supported_connector_definitions()
        if definition.key == "paystack"
    ]

    assert [
        (definition.version, definition.digest) for definition in supported_paystack
    ] == [
        (
            "1.0.1",
            "d0dabfdf7ce77a65fa5a8259400d249fa27c69bdb695cfdeffd1a357bef0b425",
        ),
        (
            "1.0.0",
            "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e",
        ),
        (
            "1.0.0",
            "9f1e314860294696c825d8d49d300df903ced6c319b406f295047d25585e836c",
        ),
    ]
    for definition in supported_paystack:
        assert (
            pinned_connector_definition(
                definition.key,
                version=definition.version,
                manifest_digest=definition.digest,
            )
            is definition
        )


def test_exact_historical_meta_social_pins_remain_bounded_and_executable() -> None:
    supported_meta_social = [
        definition
        for definition in supported_connector_definitions()
        if definition.key == "meta.social"
    ]

    assert [
        (definition.version, definition.digest) for definition in supported_meta_social
    ] == [
        (
            "1.1.0",
            "dfa7b8229d870b1524ba8fcab76448c9ba3de2f4922ac6031105398811c993d6",
        ),
        (
            "1.0.0",
            "c2a13ab30bebb90b312ab4bbcf1d2feaa23ea706100b3e5ea35a101ec566cf08",
        ),
        (
            "1.0.0",
            "1186a1b861b561480e609363baf2822dbc7091857054ef076fb686e581e4bc5b",
        ),
    ]
    for definition in supported_meta_social:
        assert (
            pinned_connector_definition(
                definition.key,
                version=definition.version,
                manifest_digest=definition.digest,
            )
            is definition
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "Bad Key"),
        ("version", "1.0"),
    ],
)
def test_manifest_rejects_invalid_identity(field: str, value: str) -> None:
    kwargs = {
        "key": "valid-key",
        "name": "Valid",
        "version": "1.0.0",
        "connector_type": "custom",
        "description": "Valid connector.",
        "runtime": RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
    }
    kwargs[field] = value

    with pytest.raises(ValidationError):
        ConnectorManifest(**kwargs)


def test_manifest_rejects_catalogue_capability_and_unpinned_external_image() -> None:
    with pytest.raises(ValidationError):
        ConnectorManifest(
            key="catalog-only",
            name="Catalogue only",
            version="1.0.0",
            connector_type="custom",
            description="No runtime.",
            runtime=RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
            capabilities=(
                CapabilityManifest(
                    id="custom.execute.v1",
                    modes=(CapabilityMode.manual,),
                ),
            ),
        )

    with pytest.raises(ValidationError):
        RuntimeManifest(
            type=ConnectorRuntimeType.external_oci,
            image="registry.example/connector:latest",
            digest="latest",
        )


def test_operation_envelope_is_version_pinned() -> None:
    definition = connector_definition("whatsapp")
    assert definition is not None
    operation = OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test:message:1",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id="messaging.send.v1",
        connector_key=definition.key,
        connector_version=definition.version,
        manifest_digest=definition.digest,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.manual,
        idempotency_key="message:1",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={"message_id": "msg-1"},
    )

    assert operation.connector_version == "1.1.0"
    assert OperationStatus.reconciliation_required.value == "reconciliation_required"


def test_runner_registry_requires_explicit_unique_registration() -> None:
    runner = object()
    registry = RunnerRegistry()
    registry.register("whatsapp", runner)  # type: ignore[arg-type]

    assert registry.resolve("WHATSAPP") is runner
    assert registry.registered_keys() == ("whatsapp",)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("whatsapp", runner)  # type: ignore[arg-type]
    with pytest.raises(LookupError, match="no runner registered"):
        registry.resolve("paystack")
