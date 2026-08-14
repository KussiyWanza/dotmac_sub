"""Migrated-PostgreSQL proof of the staff Party identity invariant.

The unit shadow-parity tests build SQLite from current ORM metadata. They prove
the model still declares one SystemUser per non-null Person Party, but they
cannot detect an Alembic or kernel-lineage change that drops the deployed
constraint while leaving the model unchanged.

This module runs only in the PostgreSQL Gate, against the schema built by the
real migration chain. It pins both the catalog shape and its enforcement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.party import Party, PartyType
from app.models.system_user import SystemUser

CONSTRAINT_NAME = "uq_system_users_person_party_id"


def _bound_staff(*, party: Party) -> SystemUser:
    return SystemUser(
        first_name="Migrated",
        last_name="Constraint",
        email=f"party-constraint-{uuid.uuid4().hex}@example.test",
        is_active=True,
        person_party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="integration_test",
        party_binding_reason="prove migrated staff Party uniqueness",
    )


def test_migrated_catalog_keeps_one_system_user_per_person_party(
    engine: Engine,
) -> None:
    constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints(
            "system_users",
            schema="public",
        )
    }

    assert constraints.get(CONSTRAINT_NAME) == ("person_party_id",)


def test_migrated_constraint_refuses_a_second_system_user_for_one_party(
    db_session: Session,
) -> None:
    party = Party(
        party_type=PartyType.person.value,
        display_name=f"Migrated Constraint {uuid.uuid4().hex[:8]}",
    )
    db_session.add(party)
    db_session.flush()
    db_session.add(_bound_staff(party=party))
    db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(_bound_staff(party=party))
            db_session.flush()

    assert CONSTRAINT_NAME in str(exc_info.value.orig)
