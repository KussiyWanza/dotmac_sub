"""Transport shape of one Integrator-delivered `messaging.receive.v1` envelope.

This is the wire contract only. It carries no destination: a field naming a
conversation, a team, a queue or a subscriber is deliberately absent, because
`dotmac_integration.destination_binding` establishes where an observation lands
from a trusted binding, never from the message. `scope` is Sub's opaque name for
that destination stream. The Integrator carries it without interpreting it —
see the module docstring of
`app.services.team_inbox_integrator_envelope`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MESSAGING_RECEIVE_CAPABILITY = "messaging.receive.v1"
#: The contract versions this deployment has actually deployed. An envelope
#: naming any other version is refused rather than best-effort parsed.
SUPPORTED_CONTRACT_VERSIONS: frozenset[int] = frozenset({1})


class IntegratorScope(BaseModel):
    """The destination product's stream name, carried opaquely by Integrator."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=60)
    ref: str = Field(min_length=1, max_length=160)


class IntegratorLocation(BaseModel):
    """Provider-observed coordinates, not a presentation string."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(strict=True, ge=-90, le=90)
    longitude: float = Field(strict=True, ge=-180, le=180)
    name: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)


class IntegratorAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str = Field(min_length=1, max_length=40)
    file_name: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=160)
    provider_media_id: str | None = Field(default=None, max_length=255)
    source_url: str | None = Field(default=None, max_length=2000)
    caption: str | None = Field(default=None, max_length=2000)
    file_size: int | None = Field(default=None, ge=0)
    download_status: str | None = Field(default=None, max_length=40)
    location: IntegratorLocation | None = None


class IntegratorContactProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    profile_pic: str | None = Field(default=None, max_length=2000)


class IntegratorMessageObservation(BaseModel):
    """One inbound message, already translated out of the provider's wire form."""

    model_config = ConfigDict(extra="forbid")

    contact_address: str = Field(min_length=1, max_length=320)
    body: str = Field(max_length=10_000)
    contact_name: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=500)
    external_message_id: str = Field(min_length=1, max_length=255)
    external_thread_id: str | None = Field(default=None, max_length=255)
    provider_account_id: str | None = Field(default=None, max_length=255)
    external_account_id: str | None = Field(default=None, max_length=255)
    page_id: str | None = Field(default=None, max_length=255)
    instagram_account_id: str | None = Field(default=None, max_length=255)
    surface: str | None = Field(default=None, max_length=60)
    permalink_url: str | None = Field(default=None, max_length=2000)
    media_url: str | None = Field(default=None, max_length=2000)
    contact_profile: IntegratorContactProfile | None = None
    attachments: tuple[IntegratorAttachment, ...] = ()

    @model_validator(mode="after")
    def require_observed_content(self) -> IntegratorMessageObservation:
        if not self.body.strip() and not self.attachments:
            raise ValueError("A message requires text or at least one attachment.")
        return self


class IntegratorDeliveryReceiptObservation(BaseModel):
    """One provider delivery-state report for a message Sub previously sent."""

    model_config = ConfigDict(extra="forbid")

    external_message_id: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=40)
    recipient_id: str | None = Field(default=None, max_length=255)
    error_codes: tuple[str, ...] = ()


class IntegratorObservationEnvelope(BaseModel):
    """The provider-neutral capability envelope Sub's port accepts."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=1, max_length=120)
    contract_version: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=80)
    provider_account_scope: str = Field(min_length=1, max_length=160)
    provider_event_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=40)
    observed_at: datetime
    payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: IntegratorScope
    message: IntegratorMessageObservation | None = None
    delivery_receipt: IntegratorDeliveryReceiptObservation | None = None


class IntegratorObservationReceipt(BaseModel):
    """What the port answers. It reports, and never decides, the consequence."""

    observation_id: str
    outcome: str
    processing_status: str
    replayed: bool


class IntegratorMirrorFieldDisagreement(BaseModel):
    field: str
    integrator: str | None
    sub: str | None


class IntegratorMirrorReport(BaseModel):
    """Read-only parity verdict for one envelope against Sub's own receiver."""

    verdict: str
    identity: str
    counterpart_identity: str | None
    blocking_reasons: tuple[str, ...]
    disagreements: tuple[IntegratorMirrorFieldDisagreement, ...]
    agrees: bool


class ProductPortDescriptorV1(BaseModel):
    """Sub's authenticated declaration of its product-port destination."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["dotmac.io/product-port-descriptor/v1"]
    application: Literal["sub"]
    owner_module: str = Field(min_length=1, max_length=160)
    capability_id: Literal["messaging.receive.v1"]
    capability_summary: str = Field(min_length=1, max_length=500)
    contract_version: Literal[1]
    destination_binding_id: UUID
    delivery_path: str = Field(pattern=r"^/")
    mirror_path: str = Field(pattern=r"^/")
    destination_scope: IntegratorScope
    activation_state: Literal[
        "configured_disabled", "enabled", "quarantined", "retired"
    ]
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
