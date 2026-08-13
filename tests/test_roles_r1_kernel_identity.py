"""Roles R1: the kernel identity is dual-written by its one owner, or absent.

Migration 528 adds `roles.tenant_id` and `roles.slug`. These cover the three
things that make the addition safe to ship before any cutover: the derivation
is pure and stable, the canonical owner writes both halves on the same row, and
a half-populated identity cannot be persisted at all.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.rbac import Role
from app.services import rbac_catalog
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext


def _context(scope: str = rbac_catalog.ROLE_WRITE_SCOPE) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:roles-r1-test",
        scope=scope,
        reason="verify roles R1 kernel identity",
        idempotency_key=f"roles-r1:{command_id}",
    )


def test_slug_equals_the_name_when_the_name_already_fits() -> None:
    assert rbac_catalog.derive_role_slug("field_operator") == "field_operator"
    # Normalization is the catalog's, not a second spelling rule.
    assert rbac_catalog.derive_role_slug("  Field_Operator  ") == "field_operator"


def test_slug_derivation_is_pure_and_stable_across_calls() -> None:
    name = "n" * 90
    first = rbac_catalog.derive_role_slug(name)
    assert first == rbac_catalog.derive_role_slug(name)
    assert len(first) <= rbac_catalog.ROLE_SLUG_MAX_LENGTH


def test_model_declares_the_complete_kernel_a42_role_parent_contract() -> None:
    constraint_names = {constraint.name for constraint in Role.__table__.constraints}

    assert Role.__table__.c.name.type.length == 120
    assert Role.__table__.c.slug.type.length == 63
    assert {
        "ck_roles_kernel_identity_projection",
        "uq_roles_tenant_slug",
        "uq_roles_tenant_id_id",
    } <= constraint_names
    assert Role.__table__.c.created_at.server_default is not None
    assert Role.__table__.c.updated_at.server_default is not None


def test_long_names_sharing_a_prefix_still_derive_distinct_slugs() -> None:
    shared = "regional_network_operations_supervisor_northern_region_"
    left = rbac_catalog.derive_role_slug(shared + "abuja")
    right = rbac_catalog.derive_role_slug(shared + "lagos")
    assert left != right
    assert len(left) <= rbac_catalog.ROLE_SLUG_MAX_LENGTH
    assert len(right) <= rbac_catalog.ROLE_SLUG_MAX_LENGTH


def test_create_role_writes_both_halves_of_the_kernel_identity(db_session) -> None:
    outcome = rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(context=_context(), name="noc_operator"),
    )
    db_session.commit()

    role = db_session.get(Role, outcome.id)
    assert role.slug == "noc_operator"
    assert role.tenant_id == OPERATOR_TENANT_ID
    # The legacy identity is untouched — this is a dual-write, not a migration.
    assert role.name == "noc_operator"


def test_renaming_a_role_moves_the_slug_with_the_name(db_session) -> None:
    outcome = rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(context=_context(), name="temporary_name"),
    )
    db_session.commit()

    rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(),
            role_id=outcome.id,
            name="settled_name",
        ),
    )
    db_session.commit()

    role = db_session.get(Role, outcome.id)
    assert role.name == "settled_name"
    assert role.slug == "settled_name"


def test_a_write_converges_a_role_that_predates_the_column(db_session) -> None:
    """A row written before 528 has no kernel identity; touching it fixes that.

    This is convergence-on-write, not a backfill: only rows the canonical owner
    actually writes are affected, and nothing walks the table.
    """

    legacy = Role(name="legacy_role", is_active=True)
    db_session.add(legacy)
    db_session.flush()
    legacy_id = legacy.id
    assert legacy.slug is None and legacy.tenant_id is None
    db_session.commit()

    rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(),
            role_id=legacy_id,
            description="now described",
            update_description=True,
        ),
    )
    db_session.commit()

    db_session.refresh(legacy)
    assert legacy.slug == "legacy_role"
    assert legacy.tenant_id == OPERATOR_TENANT_ID


def test_ensure_role_seeds_the_kernel_identity_too(db_session) -> None:
    role = rbac_catalog.ensure_role(
        db_session, name="seeded_role", description="from seed"
    )
    db_session.commit()

    assert role.slug == "seeded_role"
    assert role.tenant_id == OPERATOR_TENANT_ID


def test_deactivate_role_also_converges_a_legacy_kernel_identity(db_session) -> None:
    legacy = Role(name="legacy_deactivation", is_active=True)
    db_session.add(legacy)
    db_session.flush()
    legacy_id = legacy.id
    db_session.commit()

    outcome = rbac_catalog.deactivate_role(
        db_session,
        rbac_catalog.DeactivateRoleCommand(
            context=_context(rbac_catalog.ROLE_DELETE_SCOPE),
            role_id=legacy_id,
        ),
    )
    db_session.commit()

    db_session.refresh(legacy)
    assert legacy.is_active is False
    assert legacy.tenant_id == OPERATOR_TENANT_ID
    assert legacy.slug == "legacy_deactivation"
    assert outcome.tenant_id == OPERATOR_TENANT_ID
    assert outcome.slug == "legacy_deactivation"


def test_half_an_identity_cannot_be_persisted(db_session) -> None:
    """The 528 projection CHECK: both halves, or neither.

    `(tenant, NULL)` matches no slug lookup and `(NULL, slug)` matches no tenant
    lookup, so a half-written row is unaddressable rather than partly adopted.
    """

    db_session.add(Role(name="half_identity", is_active=True, slug="half_identity"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_two_roles_cannot_share_one_kernel_identity(db_session) -> None:
    db_session.add(
        Role(
            name="first_role",
            is_active=True,
            slug="contended",
            tenant_id=OPERATOR_TENANT_ID,
        )
    )
    db_session.flush()
    db_session.add(
        Role(
            name="second_role",
            is_active=True,
            slug="contended",
            tenant_id=OPERATOR_TENANT_ID,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_legacy_population_is_left_alone_by_the_nullable_key(db_session) -> None:
    """Many roles with no kernel identity coexist — NULLs do not collide."""

    for index in range(3):
        db_session.add(Role(name=f"untouched_{index}", is_active=True))
    db_session.commit()

    unadopted = db_session.execute(
        select(Role).where(Role.slug.is_(None), Role.tenant_id.is_(None))
    ).scalars()
    assert len({role.name for role in unadopted}) >= 3


class TestCollisionReport:
    """The report is the artifact that has to be reviewed before adoption."""

    def test_a_clean_population_reports_no_collisions(self) -> None:
        report = rbac_catalog.role_slug_collision_report(
            [(uuid4(), "admin"), (uuid4(), "noc_operator")]
        )
        assert report.total_roles == 2
        assert report.distinct_slugs == 2
        assert report.collisions == ()
        assert report.blocking is False

    def test_names_differing_only_by_case_and_space_are_reported(self) -> None:
        left, right = uuid4(), uuid4()
        report = rbac_catalog.role_slug_collision_report(
            [(left, "Field_Operator"), (right, " field_operator ")]
        )
        assert report.blocking is True
        assert len(report.collisions) == 1
        collision = report.collisions[0]
        assert collision.slug == "field_operator"
        assert collision.role_ids == tuple(sorted((left, right), key=str))
        assert collision.role_names == ("field_operator", "field_operator")
        assert report.as_dict()["collisions"][0]["role_ids"] == [
            str(role_id) for role_id in sorted((left, right), key=str)
        ]

    def test_truncated_names_are_named_even_without_a_collision(self) -> None:
        long_name = "a" * 70
        report = rbac_catalog.role_slug_collision_report([(uuid4(), long_name)])
        assert report.collisions == ()
        assert report.truncated_names == (long_name,)

    def test_the_report_is_byte_identical_across_runs(self) -> None:
        """Determinism is the property that makes it diffable between snapshots."""

        population = [
            (UUID("11111111-1111-4111-8111-111111111111"), "Field_Operator"),
            (UUID("22222222-2222-4222-8222-222222222222"), "field_operator"),
            (UUID("33333333-3333-4333-8333-333333333333"), "b" * 80),
            (UUID("44444444-4444-4444-8444-444444444444"), "admin"),
        ]
        first = rbac_catalog.role_slug_collision_report(population).as_dict()
        shuffled = [population[2], population[0], population[3], population[1]]
        second = rbac_catalog.role_slug_collision_report(shuffled).as_dict()
        assert first == second
