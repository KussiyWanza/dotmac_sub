"""Supply `module_database_roles.v1` from Sub's own lineage.

Revision ID: 546_module_database_roles_prerequisite
Revises: 545_tenant_scope_catalog_prerequisite
Create Date: 2026-08-20

ADR-0011. The second effect every installable module declares. A module never
creates these roles itself — the kernel's spec is explicit that creating a role
needs privileges a module migration must not assume, and that a module inventing
its own roles is a second authority over cluster access. So the product supplies
them, which here means Sub.

Three roles, and the RLS posture is the point rather than the names:

- ``app_admin`` — ``LOGIN BYPASSRLS``. Offline and migration work has to see
  every tenant's rows; one that cannot turns maintenance into silent zero-row
  success. Deliberately NOT a superuser.
- ``app_user`` — ``LOGIN``, no bypass. Online tenant request traffic.
- ``platform_api`` — ``LOGIN``, no bypass. Online platform request traffic.

The verifier checks ``rolsuper`` as well as ``rolbypassrls``, because **a
superuser bypasses RLS regardless of the flag** — so a superuser ``app_user``
would satisfy a naive check while defeating tenant isolation for every composed
module at once. Creating them here with exactly kernel `0001`'s attributes is
what keeps that from being an accident.

**Passwords are not set here**, matching the kernel. Operators set them out of
band and wire each role to its own connection string.

**Privileges this migration needs.** Creating a role requires ``CREATEROLE`` (or
superuser) on the migrating connection. The DO block only issues ``CREATE ROLE``
for a role that does not already exist, so a deployment whose operator has
pre-created all three needs no elevated privilege at all and this migration is a
no-op. A deployment where they are missing AND the migrating role cannot create
them will fail here, loudly, at deploy time — which is the correct outcome: the
alternative is a module lineage later failing to grant to a role that was never
created, much further from the cause.

Additive, idempotent, and forward-only. Roles are NOT dropped on downgrade —
they are cluster-wide objects another database or a later migration may be
using, and dropping one because a single lineage stepped back would reach well
outside this migration's business.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "546_module_database_roles_prerequisite"
down_revision: str | None = "545_tenant_scope_catalog_prerequisite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kernel `0001`'s `_ensure_roles`, verbatim in attributes and idempotency.
_ENSURE_ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
        CREATE ROLE app_admin LOGIN BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_api') THEN
        CREATE ROLE platform_api LOGIN;
    END IF;
END$$;
"""


def upgrade() -> None:
    op.execute(_ENSURE_ROLES)


def downgrade() -> None:
    """Deliberately empty — see the module docstring on not dropping roles."""
