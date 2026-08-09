import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema


class SettingValueType(str):
    """An open setting-value-type member.

    Was a four-member ``enum.Enum`` stored as the native ``settingvaluetype``
    PostgreSQL enum, with a CHECK constraint on ``domain_settings`` naming
    ``json`` literally. Between them a fifth type was unstorable no matter who
    declared it --- the same closed-list defect ``SettingDomain`` had, one
    column across, and the same one ADR-0008 forbids.

    It is not hypothetical. Four Sub settings hold LISTS and thirteen hold money
    amounts as decimal strings with the currency in a separate setting; the
    kernel declares ``list`` and ``money`` and neither could be written here.
    See ``512_open_setting_value_type_vocabulary``.

    A ``str`` SUBCLASS rather than a bare alias, for the same reasons as
    :class:`~app.models.domain_settings.SettingDomain`: ``.value`` keeps
    working at the call sites that came from the enum, and a type still reads
    as a type in logs.

    Deliberately NOT interned, so ``SettingValueType("json") is
    SettingValueType.json`` is FALSE where the enum made it true --- compare
    with ``==``.

    **Where the authority now lives.** The declared set belongs to
    ``dotmac_kernel.setting_value_types``: a value type describes how a value
    is encoded, which is a fleet-wide fact, and two products declaring
    incompatible versions of one is exactly what ADR-0008 exists to prevent.
    Sub therefore declares NO registry of its own. This slice removes the
    database's closed list; the write-boundary check against the kernel
    registry arrives with the settings cutover, which is the change that makes
    ``dotmac_kernel`` importable from ``app``. Until then the specs in
    ``app/services/settings_spec.py`` remain the practical constraint --- every
    settings write coerces through a declared spec.

    The class attributes below are ACCESSORS for the types in use today, so the
    existing ``SettingValueType.<name>`` call sites keep working. They are a
    subset of the declared set, not its definition.
    """

    __slots__ = ()

    string: ClassVar["SettingValueType"]
    integer: ClassVar["SettingValueType"]
    boolean: ClassVar["SettingValueType"]
    json: ClassVar["SettingValueType"]

    @property
    def value(self) -> str:
        """The bare string, for call sites carried over from the enum."""

        return str(self)

    def __repr__(self) -> str:
        return f"SettingValueType.{str(self)}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: "GetCoreSchemaHandler"
    ) -> "CoreSchema":
        """Make the open type usable in Pydantic models.

        Permissive on purpose, like ``SettingDomain``: a row written under a
        type this deployment no longer knows must still serialise on read.
        """

        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


for _name in ("string", "integer", "boolean", "json"):
    setattr(SettingValueType, _name, SettingValueType(_name))
del _name


class SettingValueTypeType(TypeDecorator):
    """Store a value type as text, load it back as :class:`SettingValueType`.

    Without this a plain ``String`` column returns a bare ``str`` on load and
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
        return SettingValueType(str(value))


class SubscriptionEngine(Base):
    __tablename__ = "subscription_engines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    settings = relationship("SubscriptionEngineSetting", back_populates="engine")


class SubscriptionEngineSetting(Base):
    __tablename__ = "subscription_engine_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    engine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_engines.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        SettingValueTypeType(40), default=SettingValueType.string
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict | None] = mapped_column(JSON)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    engine = relationship("SubscriptionEngine", back_populates="settings")
