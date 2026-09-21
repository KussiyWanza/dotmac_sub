"""Repair requester identity used by field request history.

The field history endpoints deliberately show only requests owned by the
authenticated requester.  Older rows can predate the durable system-user
link, so a replaced technician profile or a later Party binding can otherwise
make an owned request disappear from both the material and expense lists.

This migration fills only identities proven by an exact foreign-key or unique
identity match.  Ambiguous rows remain unchanged and therefore fail closed.

Revision ID: 587_field_request_requester_history
Revises: 586_inbox_sla_rules
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "587_field_request_requester_history"
down_revision: str | None = "586_inbox_sla_rules"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_REQUEST_TABLES: tuple[str, ...] = (
    "field_material_requests",
    "field_expense_requests",
)

_REQUESTER_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "field_material_requests",
        "ix_field_material_requests_requested_by_person",
    ),
    (
        "field_material_requests",
        "ix_field_material_requests_requested_by_system_user",
    ),
    (
        "field_expense_requests",
        "ix_field_expense_requests_requested_by_person",
    ),
    (
        "field_expense_requests",
        "ix_field_expense_requests_requested_by_system_user",
    ),
)


def _has_index(table_name: str, index_name: str) -> bool:
    return any(
        index["name"] == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )


def _repair_requester_identity(table_name: str) -> None:
    # Old field submissions always carried a technician link.  That link is
    # the strongest available bridge to the durable SystemUser identity.
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS request
            SET requested_by_system_user_id = technician.system_user_id
            FROM technician_profiles AS technician
            WHERE request.requested_by_system_user_id IS NULL
              AND request.requested_by_technician_id = technician.id
              AND technician.system_user_id IS NOT NULL
            """
        )
    )

    # Early native rows used the SystemUser UUID as their person identifier.
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS request
            SET requested_by_system_user_id = su.id
            FROM system_users AS su
            WHERE request.requested_by_system_user_id IS NULL
              AND request.requested_by_person_id = su.id
            """
        )
    )

    # Party-backed rows can be recovered from the unique staff Party binding.
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS request
            SET requested_by_system_user_id = su.id
            FROM system_users AS su
            WHERE request.requested_by_system_user_id IS NULL
              AND su.person_party_id IS NOT NULL
              AND request.requested_by_person_id = su.person_party_id
            """
        )
    )

    # Preserve the permanent Person Party identity once a formerly legacy row
    # has an exact SystemUser binding.  Never overwrite a different person ID.
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS request
            SET requested_by_person_id = su.person_party_id
            FROM system_users AS su
            WHERE request.requested_by_system_user_id = su.id
              AND su.person_party_id IS NOT NULL
              AND request.requested_by_person_id = su.id
            """
        )
    )

    # Staff-created web rows legitimately have no technician link.  Add one
    # only when the durable SystemUser has one exact active field profile.
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS request
            SET requested_by_technician_id = technician.id
            FROM technician_profiles AS technician
            WHERE request.requested_by_technician_id IS NULL
              AND request.requested_by_system_user_id = technician.system_user_id
              AND technician.is_active IS TRUE
            """
        )
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    for table_name, index_name in _REQUESTER_INDEXES:
        column_name = (
            "requested_by_system_user_id"
            if index_name.endswith("system_user")
            else "requested_by_person_id"
        )
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, [column_name])
    for table_name in _REQUEST_TABLES:
        _repair_requester_identity(table_name)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Repaired identity evidence is valid historical data and is intentionally
    # retained.  Downgrade removes only the query-supporting indexes.
    for table_name, index_name in reversed(_REQUESTER_INDEXES):
        if _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
