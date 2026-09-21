"""Canonical SOT declarations for the application_sessions domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="application_sessions",
    services=(
        SOTService(
            name="app_sessions.store",
            module="app.services.session_store",
            owns=(
                "Redis-backed session storage",
                "session principal indexes",
                "session revocation epochs",
            ),
        ),
        SOTService(
            name="app_sessions.customer_portal",
            module="app.services.customer_portal_session",
            owns=(
                "customer portal session creation",
                "customer portal session refresh/revoke",
                "impersonation/read-only portal session policy",
            ),
            depends_on=("app_sessions.store", "customer.identity_scope"),
        ),
        SOTService(
            name="app_sessions.auth",
            module="app.services.session_manager",
            owns=(
                "database auth-session listing",
                "database auth-session revocation",
            ),
            depends_on=("app_sessions.store",),
        ),
        SOTService(
            name="app_sessions.refresh",
            module="app.services.auth_session_refresh",
            owns=("concurrency-safe database authentication session renewal",),
            depends_on=(
                "app_sessions.auth",
                "events.dispatcher",
                "events.store",
                "party.staff_session_projection",
                "control.settings_spec",
                "secrets.reference_store",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "concurrency-safe database authentication session renewal"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "database authentication session state",
                            "presented refresh credential and client binding",
                            "canonical staff session principal projection",
                            "access-token signing policy",
                            "held JWT signing secret",
                        ),
                        canonical_writer="app_sessions.refresh",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="database authentication session state",
                        owner="app_sessions.refresh",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "sessions token hashes, rotation timestamp, status, "
                            "expiry and recorded client binding"
                        ),
                    ),
                    AuthorityInput(
                        name="presented refresh credential and client binding",
                        owner="external:auth_client",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "opaque refresh credential plus normalized device, "
                            "user-agent and effective client address observations"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff session principal projection",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Party-bound staff session principal resolved before "
                            "a staff token rotates"
                        ),
                    ),
                    AuthorityInput(
                        name="access-token signing policy",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "effective JWT algorithm and 15-minute default access "
                            "lifetime resolved while the renewal transaction is open"
                        ),
                    ),
                    AuthorityInput(
                        name="held JWT signing secret",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "process-held jwt_secret material loaded at boot; token "
                            "values and signing material are never persisted in events"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "renew_authentication_session enters execute_owner_command "
                        "once and commits rotation, revocation and event evidence "
                        "atomically"
                    ),
                    locking=(
                        "SELECT FOR UPDATE locks the active session matching the "
                        "current or immediately previous refresh-token hash"
                    ),
                    idempotency=(
                        "The current token rotates once; the immediately previous "
                        "token replays for the same client for five seconds without "
                        "rotating again"
                    ),
                    retries=(
                        "Concurrent callers wait on the session row. Database "
                        "deadlock or serialization failures remain retryable by the "
                        "adapter; security refusals are not retried"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "app_sessions.refresh.invalid_token",
                        "app_sessions.refresh.invalid_principal",
                        "app_sessions.refresh.signing_unavailable",
                        "app_sessions.refresh.staff_projection_refused",
                        *owner_command_boundary_error_codes("app_sessions.refresh"),
                    ),
                    mapping_owner="authentication HTTP and web adapters",
                    retryable_codes=(),
                    fail_closed_on=(
                        "unknown refresh credential",
                        "expired session",
                        "previous-token reuse outside the five-second overlap",
                        "previous-token reuse from a different client binding",
                        "ambiguous or unavailable staff principal projection",
                        "missing process-held JWT signing material",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "authentication_session.rotated",
                        "authentication_session.refresh_refused",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility="Version 1 is additive and contains no token material.",
                    replay=(
                        "Events are immutable security evidence; current session "
                        "state is rebuilt from the sessions record, not event replay"
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="app.services.auth_flow.AuthFlow.refresh",
                    new_owner="app_sessions.refresh",
                    verification=(
                        "Unit overlap/refusal tests, PostgreSQL concurrent rotation "
                        "test and browser coordinator tests"
                    ),
                    cutover_gate=(
                        "Every refresh adapter delegates to the typed owner and no "
                        "adapter rotates or revokes the session row"
                    ),
                    fallback_retirement=(
                        "The legacy AuthFlow refresh mutation is removed; AuthFlow "
                        "remains only a transport adapter"
                    ),
                ),
                steward="identity and platform operations",
                design_refs=(
                    "docs/designs/AUTH_SESSION_REFRESH_CONCURRENCY.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_auth_session_refresh.py",
                    "tests/integration/test_auth_session_refresh_concurrency.py",
                    "tests/js/session_refresh.test.js",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.customer.auth",
        "app.web.customer.routes",
        "app.api.auth",
        "app.web.admin.auth",
    ),
    rule="Routes authenticate and authorize; session services own storage, "
    "refresh, listing, revocation, and impersonation session policy.",
)
