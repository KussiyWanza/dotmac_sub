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
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    TypeDecorator,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

_logger = logging.getLogger(__name__)
from app.models.subscription_engine import SettingValueType, SettingValueTypeType

#: The scope kind every Sub settings row lives at. ADR-0009: Sub provisions
#: exactly one tenant, the ISP operator, and its settings belong to it —
#: `platform` is the deployment-wide level BENEATH tenant in the kernel's
#: model, not a synonym for "this deployment". Migration 509 moved every row
#: here; this is what keeps new rows arriving in the same place.
TENANT_SCOPE = "tenant"
PLATFORM_SCOPE = "platform"

#: The sentinel standing in for NULL inside the uniqueness index, matching
#: migration 507. A real UUID so the COALESCE is type-correct, and one no
#: generator produces.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _operator_tenant_id() -> uuid.UUID:
    """The tenant a new settings row belongs to.

    Imported inside the callable to keep `app.models` free of a service-layer
    dependency at import time, exactly as `_reject_undeclared_domain` does.
    `OPERATOR_TENANT_ID` is a module constant, so this reads nothing and
    touches no session.
    """

    from app.services.operator_tenant import OPERATOR_TENANT_ID

    return OPERATOR_TENANT_ID


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
        # Mirrors migration 507 — name, columns and order. `(domain, key)`
        # alone was DROPPED there and this declaration outlived it: once a row
        # can exist at more than one scope that pair is not unique, so the
        # metadata-built lanes were constructing a schema the migration chain
        # does not have, and constraining writes Postgres allows.
        #
        # `COALESCE` removes the NULL rather than tolerating it. Postgres
        # treats every NULL inside a unique index as distinct from every other,
        # so a nullable scope column in a plain composite admits exactly the
        # duplicate rows the index exists to prevent.
        Index(
            "uq_domain_settings_scope_domain_key",
            "domain",
            "key",
            text(f"COALESCE(tenant_id, '{_NIL_UUID}')"),
            "scope_kind",
            text(f"COALESCE(scope_id, '{_NIL_UUID}')"),
            unique=True,
        ),
        # A scope kind and a tenant say the same thing and must not disagree.
        # `platform` is the deployment-wide level and has no tenant; every
        # finer kind names one. Without this the two columns drift into shapes
        # no reader can match — a row marked `tenant` with a NULL tenant is
        # invisible to the resolver's own lookup, which filters on both.
        #
        # This is `dotmac_kernel.setting_scopes.SettingScope.__post_init__`,
        # enforced where every writer passes rather than only where a caller
        # remembered to build a `SettingScope`.
        CheckConstraint(
            "(scope_kind = 'platform' AND tenant_id IS NULL) "
            "OR (scope_kind <> 'platform' AND tenant_id IS NOT NULL)",
            name="ck_domain_settings_scope_alignment",
        ),
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
    # Scope columns. Sub reads them through the kernel resolver, which since
    # the settings cutover asks for the OPERATOR TENANT's rows and falls back
    # to platform ones — so which scope a row lands at is not cosmetic.
    #
    # These defaulted to `platform`/NULL, which was true when migration 507
    # added them and stopped being true when 509 moved every existing row to
    # the operator tenant (ADR-0009: the ISP operator IS the one tenant, and
    # `platform` is the level beneath it, not a synonym for this deployment).
    # The defaults outlived that migration, so every setting created AFTER it
    # arrived at a different scope from every setting created before — a split
    # estate that resolution papers over, because a platform row is exactly
    # what a tenant read falls back to. It surfaces later and worse: as two
    # rows for one key that the uniqueness index permits and
    # `get_optional_by_key`, which filters on neither scope column, picks
    # between arbitrarily.
    #
    # The tenant is filled by `_default_tenant_to_the_operator` below rather
    # than by a column `default=`. A column default fires whenever the attribute
    # is `None` at flush and cannot see `scope_kind`, so it filled the operator
    # tenant into an explicitly PLATFORM row and produced the one shape the
    # CHECK forbids — turning "I asked for a platform row" into an opaque
    # constraint violation. Deriving one column from the other is the same move
    # the kernel makes in the opposite direction (`_default_scope_kind` reads
    # `tenant_id`), and it makes the pair coherent by construction.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # The ORM default and the SERVER default deliberately differ, and the CHECK
    # above is what keeps both honest.
    #
    # An application write means "the operator's setting", so the ORM default is
    # the tenant. A raw `INSERT` naming no scope is almost always a MIGRATION,
    # running at a point in the chain where the operator tenant does not exist
    # yet — `tenants` arrives in 508 and the row in 509 — so those rows must
    # land at `platform`, which references no tenant and which 509 and 514 then
    # move. A `tenant` server default would make every such insert produce a
    # `tenant` row with a NULL tenant: a CHECK violation, and before that a row
    # the resolver can never match, since it filters on both columns.
    scope_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TENANT_SCOPE,
        server_default=PLATFORM_SCOPE,
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


def _default_tenant_to_the_operator(
    mapper: object, connection: object, target: DomainSetting
) -> None:
    """Give a tenant-scoped row the operator tenant when it names none.

    ADR-0009: Sub provisions exactly one tenant and its settings belong to it,
    so an application write with no scope stated is the operator's setting.

    A LISTENER rather than a column `default=`, because the default cannot see
    the other column. It fires whenever `tenant_id` is `None` at flush —
    including when a caller explicitly asked for a PLATFORM row, which has no
    tenant by definition — and so produced `platform` + a tenant, the one shape
    `ck_domain_settings_scope_alignment` forbids. Asking for a platform row
    would have failed with an opaque constraint violation.

    Reading `scope_kind` makes the pair coherent by construction instead. It is
    the same derivation the kernel does in the opposite direction:
    `dotmac_kernel.settings_models._default_scope_kind` picks the KIND from the
    tenant, because there a row's tenancy is what the caller states first.

    INSERT only. On an insert this is choosing a default the caller did not
    state; on an update the caller has named both columns and the CHECK is the
    right arbiter — silently rewriting a stated `tenant_id` there would be the
    model overruling a writer rather than defaulting for one.
    """

    # `scope_kind` is still `None` here when the caller did not set it: column
    # defaults are applied while the INSERT is compiled, which is AFTER this
    # event. Unset therefore falls through and gets the tenant, which is what
    # the column default (`TENANT_SCOPE`) is about to say anyway. Only an
    # EXPLICIT platform row returns early.
    if target.scope_kind == PLATFORM_SCOPE:
        return
    if target.tenant_id is None:
        target.tenant_id = _operator_tenant_id()


event.listen(DomainSetting, "before_insert", _reject_undeclared_domain)
event.listen(DomainSetting, "before_update", _reject_undeclared_domain)
event.listen(DomainSetting, "before_insert", _reject_undeclared_value_type)
event.listen(DomainSetting, "before_update", _reject_undeclared_value_type)


def _collect_invalidations(session: object, *_args: object) -> None:
    """Note which settings this flush touched, to drop AFTER it commits.

    The one invalidation owner. Before this, ten call sites across six modules
    each remembered to invalidate — `domain_settings.py` five times,
    `module_manager.py` twice, `settings_secret_cleanup.py`,
    `credential_key_rotation.py` — which is a cached projection with ten writers
    and no owner, exactly what the source-of-truth standard names. The eleventh
    writer is the one that forgets.

    It lives on the MODEL for the third time in this file, for the reason the
    other two give: the service layer is not the only writer.

    Collected at FLUSH and dropped at COMMIT, in two halves, because timing is
    the whole difficulty. Invalidating during the flush leaves a window where
    the row is not visible to other transactions yet: a concurrent reader
    repopulates the cache with the OLD value, the commit lands, and the stale
    entry survives until its TTL. Collecting now is necessary because after the
    commit SQLAlchemy has expired the objects and `session.dirty` is empty.

    A rollback discards the collection with the session state, so nothing is
    invalidated for a write that never happened.

    An INSERT's `tenant_id` may still be None here: `before_flush` runs before
    `before_insert`, so `_default_tenant_to_the_operator` has not filled it in
    yet. That row is therefore invalidated at PLATFORM scope, which the kernel
    widens to every scope — over-invalidation, never staleness. Reading it after
    the default would be more precise and would require the collection to happen
    somewhere `session.dirty` is already empty.
    """

    touched = getattr(session, "info", None)
    if touched is None:  # pragma: no cover - a session-like without `info`
        return
    pending: set[tuple[str, str, object]] = touched.setdefault(
        "_settings_invalidate", set()
    )
    for instance in (
        *getattr(session, "new", ()),
        *getattr(session, "dirty", ()),
        *getattr(session, "deleted", ()),
    ):
        if isinstance(instance, DomainSetting):
            pending.add((str(instance.domain), instance.key, instance.tenant_id))


def _flush_invalidations(session: object) -> None:
    """Drop what the committed writes invalidated.

    Scope matters and the kernel's asymmetry is preserved rather than
    reimplemented: a TENANT write drops that tenant's entry, while a PLATFORM
    write drops EVERY scope's entry for that setting, because every tenant
    inherits the platform row when it has none of its own. `invalidate` is
    where that lives; this function only says which scope was written.

    Failure here is logged and swallowed. An invalidation that does not land
    leaves a stale read until the store's TTL, which is bad; raising from
    `after_commit` on a write that already succeeded is worse.
    """

    info = getattr(session, "info", None)
    if not info:
        return
    pending = info.pop("_settings_invalidate", None)
    if not pending:
        return

    from dotmac_kernel.setting_scopes import SettingScope
    from dotmac_kernel.settings_cache import invalidate

    for domain, key, tenant_id in pending:
        scope = (
            SettingScope.tenant(tenant_id)
            if tenant_id is not None
            else SettingScope.platform()
        )
        try:
            invalidate(domain, key, scope=scope)
        except Exception:  # noqa: BLE001 - see the docstring
            _logger.warning("settings cache invalidation failed for %s.%s", domain, key)


event.listen(Session, "before_flush", _collect_invalidations)
event.listen(Session, "after_commit", _flush_invalidations)

event.listen(DomainSetting, "before_insert", _default_tenant_to_the_operator)
