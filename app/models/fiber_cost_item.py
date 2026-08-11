"""What a fiber installation costs, as data rather than as code.

The drop-cost estimator on the fiber map used four hardcoded components — drop
cable per metre, labour per metre, an ONT device, an installation base fee —
and each was baked into three layers at once: a `SettingSpec`, a reader in
`web_network_fiber`, and the arithmetic in `templates/admin/network/fiber/map
.html`. Adding a splice closure, a pole, or a permit fee meant editing all
three and shipping a release.

That is the host-enumerated vocabulary ADR-0008 forbids, spread across a
settings module, a service and a template — none of which owns what a fiber
install costs. Here the components are ROWS: a new one is data, and the
estimator sums whatever is active.

## `unit` is deliberately a closed enum

The open-vocabulary rule constrains vocabularies whose members belong to other
modules. This one does not: `unit` says how the estimator MULTIPLIES an amount,
so its members are bounded by arithmetic this code can actually do. A new
member is a new calculation — a deliberate change, not a data one. Same
distinction `dotmac_kernel.settings_models.SettingChangeAction` draws.

## Amounts are seeded absent, on purpose

The four settings this replaces defaulted to `2.50`, `1.50`, `85.00` and
`50.00` — values that read as USD and were rendered as naira, so the estimator
quoted ₦85 for an ONT. An amount with no currency looks correct in every
currency, which is exactly why nothing caught it.

So the seed creates the ITEMS — code, label, unit — and no amounts. Until an
operator sets them the estimator reports itself unconfigured, which is true and
loud, rather than quoting a number nobody chose. `amount` is therefore
nullable: "not priced yet" is a real state and must not be spelled `0`, which
is a legitimate price.

Currency is not stored per item. One estimate mixing currencies is meaningless,
and the screen already labels the whole estimate with one — so it comes from
`billing/default_currency`, the deployment's own answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.schemas.fiber_cost_calculation import FiberCostUnit


class FiberCostItem(Base):
    """One priced component of a fiber drop installation."""

    __tablename__ = "fiber_cost_items"
    __table_args__ = (
        UniqueConstraint("code", name="uq_fiber_cost_items_code"),
        CheckConstraint(
            "amount IS NULL OR amount >= 0",
            name="ck_fiber_cost_items_amount_nonnegative",
        ),
        CheckConstraint("version >= 1", name="ck_fiber_cost_items_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Stable identifier for the component. The estimate's line items are keyed
    #: on it, so renaming one is a data migration rather than an edit.
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[FiberCostUnit] = mapped_column(
        # Components are composable rows; this enum is only the closed set of
        # arithmetic operators the estimator can execute. `PER_METER` is the
        # Python symbol for the stable contract value `per_meter`, not a
        # component identifier. `values_callable` ensures SQLAlchemy persists
        # that stable value rather than the implementation symbol. Otherwise
        # migration 519's seeded rows fail to map back and the map page 500s.
        Enum(
            FiberCostUnit,
            name="fibercostunit",
            native_enum=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    #: NULL means "not priced yet", which is distinct from a price of zero — a
    #: free component is a real answer an operator may give.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Display order on the estimate, so the breakdown reads the way an
    #: installer thinks about it rather than by insertion time.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    description: Mapped[str | None] = mapped_column(Text)
    #: Optimistic concurrency token. Every owner-command update increments it;
    #: a form based on an older value fails closed instead of restoring stale
    #: prices or activation state over a newer operator decision.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_priced(self) -> bool:
        """Whether this component can contribute to an estimate at all."""

        return self.amount is not None
