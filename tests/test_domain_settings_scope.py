"""Every settings row Sub writes belongs to the operator tenant.

ADR-0009: Sub provisions exactly one tenant, the ISP operator, and its settings
belong to it. `platform` is the kernel's deployment-wide level BENEATH tenant,
not a synonym for "this deployment" — so a row landing there is not a harmless
labelling difference, it is a row at the wrong level of the resolution chain.

Migration 509 moved every row to the operator tenant and the model's DEFAULT
stayed at `platform`, so settings written after that migration arrived
somewhere else from settings written before it. Resolution hides the split
(a platform row is exactly what a tenant read falls back to), which is why the
guard has to be here rather than in a resolution test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from app.models.domain_settings import (
    PLATFORM_SCOPE,
    TENANT_SCOPE,
    DomainSetting,
    SettingDomain,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate
from app.services import domain_settings as domain_settings_service
from app.services.operator_tenant import OPERATOR_TENANT_ID


def _raw_row(
    *,
    scope_kind: str,
    key: str,
    tenant_id: str | None = None,
    value_text: str = "x",
) -> dict[str, object]:
    """Parameters for a settings row written the way a MIGRATION writes one.

    `created_at`/`updated_at` are NOT NULL with python-side defaults, so
    bypassing the ORM means supplying them — the same shape
    `tests/test_setting_domain_write_boundary.py` uses.
    """

    now = datetime.now(UTC)
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "kind": scope_kind,
        "domain": str(SettingDomain.gis),
        "key": key,
        "value_type": SettingValueType.string.value,
        "value_text": value_text,
        "secret": False,
        "active": True,
        "now": now,
    }


def test_a_created_setting_belongs_to_the_operator_tenant(db_session):
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    created = settings.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="scope_probe_created",
            value_type=SettingValueType.integer,
            value_text="60",
        ),
    )

    assert created.scope_kind == TENANT_SCOPE
    assert created.tenant_id == OPERATOR_TENANT_ID


def test_an_upserted_setting_belongs_to_the_operator_tenant(db_session):
    """The seed's path, and the one that writes most rows.

    `ensure_by_key` reaches `create` for a key with no row, which is how a
    newly declared setting first appears — and therefore the path by which the
    estate would have kept accumulating platform rows.
    """

    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    row = settings.ensure_by_key(
        db_session,
        key="scope_probe_ensured",
        value_type=SettingValueType.string,
        value_text="anything",
    )

    assert row.scope_kind == TENANT_SCOPE
    assert row.tenant_id == OPERATOR_TENANT_ID


def test_the_kernel_resolves_a_row_written_at_that_scope(db_session):
    """The two halves must agree.

    The cutover resolves through `dotmac_kernel.settings_resolver` asking for
    the operator tenant's rows. A row written at a scope that read never looks
    at would resolve to its default instead — the setting would exist, be
    visible in the admin list, and have no effect.
    """

    from app.services.settings_spec import resolve_value

    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    settings.ensure_by_key(
        db_session,
        key="sync_interval_minutes",
        value_type=SettingValueType.integer,
        value_text="47",
    )

    assert resolve_value(db_session, SettingDomain.gis, "sync_interval_minutes") == 47


def test_an_explicit_platform_row_keeps_its_missing_tenant(db_session):
    """Defaulting reads the scope, so asking for a platform row gets one.

    A column `default=` could not do this: it fires whenever `tenant_id` is
    `None` at flush and cannot see `scope_kind`, so it filled the operator
    tenant into an explicitly platform row and produced exactly the shape
    `ck_domain_settings_scope_alignment` forbids — "I asked for a platform row"
    surfacing as an opaque constraint violation.

    Pinned because the fix is a listener reading the other column, and a later
    simplification back to a column default would look tidier and reintroduce
    it.
    """

    row = DomainSetting(
        domain=SettingDomain.gis,
        key="scope_probe_orm_platform",
        value_type=SettingValueType.string,
        value_text="x",
        scope_kind=PLATFORM_SCOPE,
    )
    db_session.add(row)
    db_session.flush()

    assert row.scope_kind == PLATFORM_SCOPE
    assert row.tenant_id is None


def test_a_tenant_scoped_row_without_a_tenant_is_refused(db_session):
    """The invariant the CHECK carries, and why it is not just a default.

    A `tenant` row with a NULL tenant is invisible to the resolver, which
    filters on BOTH columns — a setting that exists and can never be read.

    Raw SQL, because the ORM cannot express this shape:
    `_default_tenant_to_the_operator` fills the tenant for any non-platform
    row, so the model produces a valid one. The writers that CAN produce it are
    migrations, which is exactly who this CHECK is for.
    """

    with pytest.raises((IntegrityError, StatementError)):
        db_session.execute(
            text(
                "INSERT INTO domain_settings "
                "(id, tenant_id, scope_kind, domain, key, value_type, "
                " value_text, is_secret, is_active, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :kind, :domain, :key, :value_type, "
                " :value_text, :secret, :active, :now, :now)"
            ),
            _raw_row(scope_kind=TENANT_SCOPE, key="scope_probe_headless"),
        )
    db_session.rollback()


def test_a_platform_row_carrying_a_tenant_is_refused(db_session):
    """The other direction of the same invariant: `platform` has no tenant."""

    with pytest.raises((IntegrityError, StatementError)):
        db_session.execute(
            text(
                "INSERT INTO domain_settings "
                "(id, tenant_id, scope_kind, domain, key, value_type, "
                " value_text, is_secret, is_active, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :kind, :domain, :key, :value_type, "
                " :value_text, :secret, :active, :now, :now)"
            ),
            _raw_row(
                scope_kind=PLATFORM_SCOPE,
                key="scope_probe_confused",
                tenant_id=str(OPERATOR_TENANT_ID),
            ),
        )
    db_session.rollback()


def test_one_key_may_hold_a_row_at_each_scope(db_session):
    """Uniqueness is per SCOPE, which is what migration 507 replaced.

    Pinned because the model kept declaring `UniqueConstraint(domain, key)`
    long after 507 dropped it, so the metadata-built lanes were enforcing a
    rule Postgres no longer had — and a test written against those lanes would
    have concluded, wrongly, that the old constraint was still the contract.
    """

    # Both halves through the ORM: an explicit `platform` keeps its missing
    # tenant, so the model can express the pair this index exists to allow.
    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_two_levels",
            value_type=SettingValueType.string,
            value_text="platform-level",
            scope_kind=PLATFORM_SCOPE,
        )
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_two_levels",
            value_type=SettingValueType.string,
            value_text="tenant-level",
            scope_kind=TENANT_SCOPE,
            tenant_id=OPERATOR_TENANT_ID,
        )
    )

    db_session.flush()  # must not raise
