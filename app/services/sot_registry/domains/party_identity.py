"""Canonical SOT declarations for the party_identity domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="party_identity",
    services=(
        SOTService(
            name="party.registry",
            module="app.services.party",
            owns=(
                "native person and organization party identity",
                "party data classification and quarantine",
                "party merge policy and canonical redirect",
                "external identity-reference provenance",
                "concurrent party role lifecycle",
                "reseller versus partner role contract",
                "partner agreement type vocabulary",
                "directional person and organization relationships",
                "relationship type and effective-date contract",
                "person-to-organization membership lifecycle",
                "bounded organization membership access scope",
                "canonical party contact-point lifecycle",
                "contact-point verification and consent evidence",
                "provider-scoped immutable social contact identity",
                "subscriber-account canonical party binding",
                "organization role-profile canonical party binding",
                "native Vendor and FieldVendor paired party binding",
                "SystemUser principal to Person Party binding",
                "ResellerUser Person and reseller membership binding",
                "organization membership canonical context binding",
                "FieldVendorUser explicit vendor membership binding",
                "SubscriberContact canonical Person Party binding",
                "reviewed SubscriberContact relationship projection",
                "reviewed SubscriberContact source-field contact-point projection",
            ),
            depends_on=("auth.subscriber_assignments", "auth.permission_gate"),
            notes=(
                "One native owner keeps identity, roles, descriptive "
                "relationships, memberships, and contact evidence coherent. "
                "A reseller is a specific commercial channel role; a partner "
                "is an explicitly typed collaboration agreement with no "
                "implicit permission. CRM identifiers are import provenance "
                "only. Migrations 339 through 344 are additive foundations; "
                "the subscriber binding is nullable and existing domain "
                "reads cut over only in later verified slices."
            ),
        ),
        SOTService(
            name="party.identity_audit",
            module="app.services.party_identity_audit",
            owns=(
                "read-only subscriber identity cleanup classification",
                "duplicate candidate evidence grouping",
                "subscriber cleanup worklist contract",
            ),
            depends_on=(
                "party.registry",
                "sales.service",
                "sales.orders",
                "access.subscription_lifecycle",
                "operations.provisioning_workflow",
                "financial.invoices",
                "financial.payments",
                "support.ticket_lifecycle",
            ),
            notes=(
                "Observes native Sub facts and produces private UUID-only "
                "artifacts. It never writes source state, calls CRM, or "
                "authorizes an automatic merge."
            ),
        ),
        SOTService(
            name="party.identity_adjudication",
            module="app.services.party_identity_adjudication",
            owns=(
                "reviewed subscriber identity decision contract",
                "medium and high duplicate adjudication closure",
                "Party backfill dry-run plan digest",
                "PII-free Party backfill plan artifact contract",
            ),
            depends_on=("party.identity_audit", "party.registry"),
            notes=(
                "Validates explicit decisions against current audit and row "
                "digests, then produces a non-executable plan. It has no DB "
                "writer or apply mode and never authorizes automatic merge."
            ),
        ),
        SOTService(
            name="party.identity_backfill_executor",
            module="app.services.party_identity_backfill",
            owns=(
                "approved Subscriber Party backfill execution gate",
                "Party identity backfill execution receipt",
                "Party identity backfill idempotent replay verification",
            ),
            depends_on=(
                "party.identity_audit",
                "party.identity_adjudication",
                "party.registry",
            ),
            notes=(
                "Consumes one exact, expiring, separately approved plan in a "
                "SERIALIZABLE transaction and calls party.registry for "
                "predetermined Party creation and Subscriber binding. It "
                "records a PII-free receipt, never commits inside the owner, "
                "and cannot merge, repoint, assign roles, copy contacts, or "
                "change lifecycle, billing, access, or authorization state."
            ),
        ),
        SOTService(
            name="party.organization_profile_audit",
            module="app.services.party_organization_audit",
            owns=(
                "read-only organization role-profile convergence audit",
                "Vendor and FieldVendor bridge debt classification",
                "organization profile Party-role coverage report",
            ),
            depends_on=("party.registry",),
            notes=(
                "Reports aggregate schema, binding, role-coverage, and "
                "Vendor/FieldVendor bridge counts without identity values. "
                "It never binds a profile, assigns a role, repairs a twin, "
                "calls CRM, or changes a legacy read path."
            ),
        ),
        SOTService(
            name="party.principal_context_audit",
            module="app.services.party_principal_audit",
            owns=(
                "read-only Person principal convergence audit",
                "reseller and organization membership context audit",
                "FieldVendorUser vendor context debt classification",
            ),
            depends_on=(
                "party.registry",
                "auth.subscriber_assignments",
                "auth.permission_gate",
            ),
            notes=(
                "Reports aggregate schema, principal-binding, membership-"
                "context, and field-vendor-user counts without identity values. "
                "It never binds a principal, creates or activates a membership, "
                "changes a credential or permission, calls CRM, or changes a "
                "login/read path."
            ),
        ),
        SOTService(
            name="party.contact_inbox_audit",
            module="app.services.party_contact_audit",
            owns=(
                "read-only SubscriberContact Person convergence audit",
                "legacy contact relationship and contact-point projection audit",
                "Party contact-point verification and consent coverage report",
                "Team Inbox canonical contact-point projection debt report",
            ),
            depends_on=(
                "party.registry",
                "communications.team_inbox_contact_resolution",
            ),
            notes=(
                "Reports only aggregate schema, identity, contact-point, and "
                "Inbox routing-projection counts. It never emits identity "
                "values, creates or binds a Party/relationship/contact point, "
                "changes an Inbox route, copies verification or consent, or "
                "changes authentication or authorization."
            ),
        ),
    ),
    entrypoints=(
        "scripts.migration.audit_subscriber_identity",
        "scripts.migration.plan_subscriber_party_backfill",
        "scripts.migration.execute_subscriber_party_backfill",
        "scripts.migration.audit_party_organization_profiles",
        "scripts.migration.audit_party_principal_contexts",
        "scripts.migration.audit_party_contact_inbox",
        "future party backfills",
        "future subscriber/reseller/vendor cutovers",
        "future Team Inbox contact resolution",
        "future authentication principal cutovers",
    ),
    rule="One real-world person or organization has one canonical Party and "
    "may hold several independent roles. Domain records and security "
    "principals link to Party; they do not create parallel identity. "
    "No adapter treats the additive foundation as a completed cutover.",
)
