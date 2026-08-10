"""The operator tenant — Sub's single tenant, and the owner of that fact.

ADR-0009. Sub is a dedicated single-operator deployment, and starter ADR-0003
is explicit that this is a topology rather than a second architecture: a
single-tenant deployment provisions exactly ONE tenant and keeps `Tenant`,
tenant context and composite constraints. The ISP operator is that tenant.

This module owns three things and nothing else:

- the identity of the operator tenant,
- provisioning it, idempotently,
- returning it.

It deliberately offers no update, no delete, and no "list tenants". There is
one tenant and it is not an operator-editable resource; a second row is a
defect, not a feature, until an ADR supersedes ADR-0009.

`Tenant` is imported from `dotmac_kernel.models`, which the adoption ledger
admits for exactly two names (`Tenant`, `TenantDomain`). Importing it
constructs no engine: that module declares its own `Base` and imports nothing
from `dotmac_kernel.db`. `app/db.py` remains Sub's session and transaction
authority.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session

#: Deterministic so the migration that backfills `domain_settings` and the
#: runtime that provisions on boot agree without coordinating. Derived from
#: `uuid5(NAMESPACE_DNS, "operator.sub.dotmac")`, and pinned as a literal
#: because a migration must not depend on application code that can change.
#: `tests/test_operator_tenant.py` asserts the migration's copy still matches.
OPERATOR_TENANT_ID = UUID("8c7ae830-51fc-52ae-9818-d84b2a35e568")

#: `slug` is unique in the kernel's model and is what a future multi-ISP
#: deployment would resolve on. "operator" names the role rather than the
#: company, so it needs no rename if the deployment is rebranded.
OPERATOR_TENANT_SLUG = "operator"
OPERATOR_TENANT_NAME = "Operator"


class OperatorTenantMissingError(RuntimeError):
    """The operator tenant was read before it was provisioned.

    Not a soft failure: every tenant-scoped row Sub writes carries this
    tenant's id, so continuing without it would write rows attributed to
    nothing. Provisioning runs at startup, so this means a caller ran before
    startup completed or against a database that never migrated.
    """


def provision_operator_tenant(db: Session) -> Tenant:
    """Ensure the operator tenant exists. Idempotent, safe on every boot.

    Returns the existing row untouched when there is one — this must never
    revert a `name` an operator has changed, which is why it is an
    existence check rather than an upsert.
    """

    existing = db.get(Tenant, OPERATOR_TENANT_ID)
    if existing is not None:
        return existing

    tenant = Tenant(
        id=OPERATOR_TENANT_ID,
        slug=OPERATOR_TENANT_SLUG,
        name=OPERATOR_TENANT_NAME,
        is_active=True,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def operator_tenant(db: Session) -> Tenant:
    """The operator tenant.

    Sub resolves no tenant from the request — no host or header carries one,
    and there is only ever one — so this is a lookup, not a resolver.
    """

    tenant = db.get(Tenant, OPERATOR_TENANT_ID)
    if tenant is None:
        raise OperatorTenantMissingError(
            "the operator tenant is absent; it is provisioned at startup and "
            "by migration 509_backfill_operator_tenant_scope"
        )
    return tenant


def operator_tenant_id() -> UUID:
    """The operator tenant's id, without a database round trip.

    Callers that only need to stamp a row's `tenant_id` should use this;
    `operator_tenant` is for callers that need the row.
    """

    return OPERATOR_TENANT_ID
