"""Domain settings storage and the open ``SettingDomain`` member type.

``SettingDomain`` used to be a closed ``enum.Enum`` stored as a native
PostgreSQL enum, so adding a domain meant an ``ALTER TYPE ... ADD VALUE``
migration in this hosting layer — see ``144_vas_wallets.py``,
``225_add_field_setting_domain.py`` and ``249_field_erp_sync_outbox.py``, whose
entire content is adding a member on behalf of some other module. That is the
shape ADR-0008 (fleet-wide: module-declared vocabularies, never
host-enumerated lists) forbids: the members of this vocabulary belong to the
domains that own the settings, not to this module.

So the type is now OPEN — a ``str`` subclass any value can construct — and the
authority moved to the declaration registry in
``app.services.setting_domain_registry``, which reads ``setting_domains`` off
the canonical SOT domain records. Validation happens at the WRITE boundary (the
ORM listener at the bottom of this module), not at construction: reads of rows
written under a since-undeclared domain must keep working, and a resolver must
be able to name a domain it is about to reject.

The class attributes below are ACCESSORS for the domains Sub declares today.
They exist so the ~1,300 existing ``SettingDomain.<name>`` call sites keep
working unchanged, and they are a documented, tested SUBSET of the declared
set: declaring a new domain must NOT require editing this module. See
``tests/architecture/test_setting_domain_registry.py``.
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.subscription_engine import SettingValueType, SettingValueTypeType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema


class SettingDomain(str):
    """An open setting-domain member.

    A ``str`` SUBCLASS rather than a bare alias so that ``.value`` keeps
    working at the call sites that came from the enum, and so a domain still
    reads as a domain in logs and error messages.

    Deliberately NOT interned: construction is open and takes untrusted input
    (a URL segment, a request body), so a member cache would be unbounded and
    attacker-growable. The consequence is that ``SettingDomain("auth") is
    SettingDomain.auth`` is FALSE where the enum made it true — compare with
    ``==``.
    """

    __slots__ = ()

    # Accessors for the domains declared today. A SUBSET of the registry, not
    # its definition — see the module docstring.
    auth: ClassVar["SettingDomain"]
    audit: ClassVar["SettingDomain"]
    billing: ClassVar["SettingDomain"]
    catalog: ClassVar["SettingDomain"]
    subscriber: ClassVar["SettingDomain"]
    imports: ClassVar["SettingDomain"]
    notification: ClassVar["SettingDomain"]
    network: ClassVar["SettingDomain"]
    network_monitoring: ClassVar["SettingDomain"]
    provisioning: ClassVar["SettingDomain"]
    geocoding: ClassVar["SettingDomain"]
    usage: ClassVar["SettingDomain"]
    radius: ClassVar["SettingDomain"]
    collections: ClassVar["SettingDomain"]
    lifecycle: ClassVar["SettingDomain"]
    projects: ClassVar["SettingDomain"]
    workflow: ClassVar["SettingDomain"]
    modules: ClassVar["SettingDomain"]
    inventory: ClassVar["SettingDomain"]
    comms: ClassVar["SettingDomain"]
    tr069: ClassVar["SettingDomain"]
    snmp: ClassVar["SettingDomain"]
    bandwidth: ClassVar["SettingDomain"]
    gis: ClassVar["SettingDomain"]
    scheduler: ClassVar["SettingDomain"]
    field: ClassVar["SettingDomain"]
    integration: ClassVar["SettingDomain"]

    @property
    def value(self) -> str:
        """The bare string, for call sites carried over from the enum."""

        return str(self)

    def __repr__(self) -> str:
        return f"SettingDomain.{str(self)}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: "GetCoreSchemaHandler"
    ) -> "CoreSchema":
        """Make the open type usable in Pydantic models.

        A bare ``str`` subclass has no core schema, so importing
        ``app.schemas.settings`` would raise ``PydanticSchemaGenerationError``.

        Permissive on purpose — it does NOT consult the registry. Validating
        here would make the schema layer depend on the service layer, and rows
        stored under a since-undeclared domain must still serialise on read.
        The registry is enforced at the write boundary below.
        """

        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


#: Domains with an accessor above. NOT the authority — the registry is.
#: ``subscription_engine`` was removed here: it had no spec, no route, no
#: reader and no writer, and its concern moved to the dedicated
#: ``subscription_engine_settings`` table long ago. Existing rows survive as
#: text and become unwritable, which is the intended outcome for a dead domain.
_ACCESSOR_NAMES: tuple[str, ...] = (
    "auth",
    "audit",
    "billing",
    "catalog",
    "subscriber",
    "imports",
    "notification",
    "network",
    "network_monitoring",
    "provisioning",
    "geocoding",
    "usage",
    "radius",
    "collections",
    "lifecycle",
    "projects",
    "workflow",
    "modules",
    "inventory",
    "comms",
    "tr069",
    "snmp",
    "bandwidth",
    "gis",
    "scheduler",
    "field",
    "integration",
)

for _name in _ACCESSOR_NAMES:
    setattr(SettingDomain, _name, SettingDomain(_name))
del _name


class SettingDomainType(TypeDecorator):
    """Store a domain as text, load it back as :class:`SettingDomain`.

    Without this a plain ``String`` column returns a bare ``str`` on load, and
    every ``.value`` call site inherited from the enum breaks.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, enum.Enum):  # tolerate a stray legacy member
            return str(value.value)
        return str(value)

    def process_result_value(self, value: object, dialect: object) -> object:
        if value is None:
            return None
        return SettingDomain(str(value))


class DomainSetting(Base):
    __tablename__ = "domain_settings"
    __table_args__ = (
        UniqueConstraint("domain", "key", name="uq_domain_settings_domain_key"),
        # A row carries a value in at least one column. The old form named the
        # type (`value_type = 'json'`), which is the same closed list migration
        # 512 removed from the column itself — a second JSON-stored type such
        # as `list` or `money` could not satisfy it.
        #
        # NOT "exactly one", which is what the kernel's equivalent constraint
        # says: there a type's `ValueTypeSpec.storage` picks its single column.
        # Sub writes a BOOLEAN to BOTH on purpose (see `normalize_for_db`), so
        # exactly-one would reject rows this codebase writes deliberately.
        # Tightening that is a change of storage convention and belongs to the
        # settings cutover, where the kernel becomes the writer.
        CheckConstraint(
            "value_text IS NOT NULL OR value_json IS NOT NULL",
            name="ck_domain_settings_value_alignment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Scope columns, declared so metadata matches the migrated schema and so
    # `dotmac_kernel.settings_models.DomainSetting` can read this table at all.
    # Sub reads none of them: it is a single-operator deployment, so every row
    # is platform scope. `scope_kind` defaults to "platform" rather than the
    # kernel's "tenant" — a Sub row has no tenant, and inheriting that default
    # would make every row claim a scope its own `tenant_id` contradicts.
    # See migration 507_domain_settings_scope_columns.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    scope_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="platform", server_default="platform"
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    domain: Mapped[SettingDomain] = mapped_column(
        SettingDomainType(120), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        SettingValueTypeType(40), default=SettingValueType.string
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def _reject_undeclared_domain(
    mapper: object, connection: object, target: DomainSetting
) -> None:
    """Fail a write whose domain no module declares.

    Registered on this MODEL rather than in the service layer because the
    service layer is not the only writer: seeds, the generic settings API, the
    admin web surface and several domain services all persist rows. The import
    is local to keep ``app.models`` free of a service-layer dependency at
    import time.
    """

    from app.services.setting_domain_registry import require_declared_domain

    require_declared_domain(target.domain)


def _reject_undeclared_value_type(
    mapper: object, connection: object, target: DomainSetting
) -> None:
    """Fail a write whose value type no installed module declares.

    The other half of migration ``512``. That migration removed the DATABASE's
    closed list — the native ``settingvaluetype`` enum and a CHECK naming
    ``json`` — so a kernel-declared type could be stored at all. Removing a
    constraint without replacing its guarantee would have made a typo a stored
    row, so the authority moves rather than disappears: it is now
    ``dotmac_kernel.setting_value_types``, because how a value is ENCODED is a
    fleet-wide fact and two products declaring incompatible versions of one
    type is the fork ADR-0008 exists to prevent (starter ADR-0006, "build once;
    an extension point is not a licence").

    Enforced at the WRITE boundary, on the model, for the same two reasons as
    :func:`_reject_undeclared_domain`: the service layer is not the only
    writer, and a row stored under a since-retired type must still READ.
    """

    from dotmac_kernel.setting_value_types import active_setting_value_types

    if target.value_type is None:
        return
    active_setting_value_types().require(str(target.value_type))


event.listen(DomainSetting, "before_insert", _reject_undeclared_domain)
event.listen(DomainSetting, "before_update", _reject_undeclared_domain)
event.listen(DomainSetting, "before_insert", _reject_undeclared_value_type)
event.listen(DomainSetting, "before_update", _reject_undeclared_value_type)
