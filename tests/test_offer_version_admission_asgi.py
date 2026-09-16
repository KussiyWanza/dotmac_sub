"""End-to-end proof of offer-version admission's REAL ASGI request graph.

Round 13 finding 4 retired the source-grep/direct-dependency-call shape this
file's predecessor (``tests/test_offer_version_admission_route_permissions.py``,
deleted by this same change) used: calling ``_require_offer_version_admission``
or the router gate directly, or inspecting source text/router membership,
proved nothing about the REAL ASGI dependency graph a live request actually
traverses — and that shape has now failed to catch a real bug (round 13's
``credential_kind`` lockout) despite three rounds of making it more elaborate.

Every test here mounts the ACTUAL ``app.api.catalog`` routers (``router`` and
``admission_router``) onto a real ``FastAPI()`` app, in the SAME shape
``app/main.py``'s router table uses (``/api/v1`` prefix, bare-authentication
``"user"`` dependency mode), and drives them with ``fastapi.testclient
.TestClient`` issuing REAL HTTP requests. The only override is
``require_user_auth`` — FastAPI's own supported dependency-override
mechanism, substituting the deepest LEAF authentication dependency with a
fixture-controlled principal, exactly as ``tests/test_dispatch_api.py`` and
several other suites in this repo already do for HTTP-level tests. Every
OTHER dependency in the chain (``require_method_permission``,
``_require_offer_version_admission``, ``authorize_offer_version_admission``,
and the in-transaction re-check inside ``OfferVersions.update``) executes
for real, against the real test database, through the real ASGI stack.

Evidence-precision note (Michael's ruling on evidence claims): the
observation each test below actually makes is stated in its own docstring —
"the mounted app" (a real ``TestClient`` request was issued and its response
observed), "a resolved dependency list" (the live route's own
``dependant.dependencies`` was inspected on an app that was actually
constructed and included), or both. Neither is a source-level import or a
direct call to an inner function.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import catalog as api_catalog
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.catalog import AccessRequirement, AccessType, PriceBasis, ServiceType
from app.models.erp_staff_access import ErpStaffLeaveRestriction
from app.models.rbac import Permission, SystemUserPermission
from app.models.system_user import SystemUser
from app.schemas.catalog import CatalogOfferCreate, OfferVersionCreate
from app.services import catalog as catalog_service
from app.services import erp_staff_access
from app.services.auth_dependencies import require_user_auth
from app.services.catalog import offer_access_requirement
from app.services.catalog.offer_access_requirement import (
    AdmitOfferVersionCommand,
    OfferAccessRequirementError,
    StaffPrincipal,
    admit_offer_version,
)
from app.services.erp_staff_access import StaffLeaveRestrictionStatus
from app.services.owner_commands import CommandContext


@pytest.fixture(autouse=True)
def _owner_command_session(db_session):
    """Keep fixture identities usable after explicit owner-command commits."""

    original_expiry = db_session.expire_on_commit
    db_session.expire_on_commit = False
    try:
        yield
    finally:
        db_session.expire_on_commit = original_expiry


def _mounted_app(db_session) -> FastAPI:
    """The REAL router objects, mounted exactly as ``app/main.py``'s router
    table mounts them: same prefix, same bare-authentication dependency
    mode. ``get_db`` is overridden to the test session; ``require_user_auth``
    is overridden per-test to the controllable principal under test — the
    ONE override every HTTP-level test in this repo's suite already uses for
    this exact purpose (see ``tests/test_dispatch_api.py``)."""

    app = FastAPI()
    app.include_router(
        api_catalog.router, prefix="/api/v1", dependencies=[Depends(require_user_auth)]
    )
    app.include_router(
        api_catalog.admission_router,
        prefix="/api/v1",
        dependencies=[Depends(require_user_auth)],
    )
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="ASGI",
        last_name="Test",
        email=f"asgi-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


def _grant_direct_permission(db_session, user: SystemUser, key: str) -> None:
    """A DIRECT ``SystemUserPermission`` grant — not the ``admin`` role —
    so it can be individually revoked mid-test without touching any other
    permission the same principal might hold."""

    permission = db_session.query(Permission).filter(Permission.key == key).first()
    if permission is None:
        permission = Permission(key=key, is_active=True)
        db_session.add(permission)
        db_session.commit()
        db_session.refresh(permission)
    db_session.add(
        SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
    )
    db_session.commit()


def _revoke_direct_permission(db_session, user: SystemUser, key: str) -> None:
    permission = db_session.query(Permission).filter(Permission.key == key).first()
    grant = (
        db_session.query(SystemUserPermission)
        .filter(
            SystemUserPermission.system_user_id == user.id,
            SystemUserPermission.permission_id == permission.id,
        )
        .first()
    )
    db_session.delete(grant)
    db_session.commit()


def _auth_for(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _machine_auth(
    *, credential_id: uuid.UUID | None = None, scopes: list[str] | None = None
) -> dict:
    """The auth shape ``auth_dependencies._machine_principal`` actually
    produces: ``principal_type == "api_key"`` with the ``credential_kind
    == "machine"`` stamp. Round 14 finding 2: every OTHER request in this
    file goes through ``_auth_for`` above, which is always a staff
    principal and never sets ``credential_kind`` — so none of them could
    ever exercise, or notice the loss of, the machine-credential shadow
    path this file exists to protect. This is the one auth shape that
    can."""

    return {
        "principal_id": str(credential_id or uuid.uuid4()),
        "principal_type": "api_key",
        "credential_kind": "machine",
        "roles": [],
        "scopes": list(scopes or ()),
    }


def _offer(db_session):
    return catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="ASGI Offer",
            code=f"ASGI-{uuid.uuid4().hex[:8]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )


def _apply_active_leave_restriction(db_session, user: SystemUser) -> tuple[str, str]:
    """A REAL ``ErpStaffLeaveRestriction`` row — not a mock of
    ``staff_write_restricted`` — so the request below genuinely exercises
    the whole leave-restriction decision, not an injected substitute for
    it."""

    now = datetime.now(UTC)
    source_system = "test"
    restriction_id = f"asgi-{uuid.uuid4().hex[:8]}"
    db_session.add(
        ErpStaffLeaveRestriction(
            source_system=source_system,
            restriction_id=restriction_id,
            erp_employee_id=f"emp-{uuid.uuid4().hex[:8]}",
            system_user_id=user.id,
            effective_from=now - timedelta(days=1),
            effective_until=None,
            status=StaffLeaveRestrictionStatus.active.value,
            version=1,
            source_updated_at=now,
            last_event_id=f"evt-{uuid.uuid4().hex[:8]}",
        )
    )
    db_session.commit()
    return restriction_id, source_system


def test_unauthorized_admission_is_refused_by_the_mounted_admission_route(
    db_session,
):
    """OBSERVED: the mounted app, via a real, issued HTTP request. A staff
    principal holding NO permission at all is refused by the actual mounted
    ``POST /api/v1/offer-versions`` route — the entire real dependency
    chain (router-level auth, ``_require_offer_version_admission``,
    ``authorize_offer_version_admission``) runs for this one request; no
    dependency is called directly, no source text is inspected."""

    user = _system_user(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)
    offer = _offer(db_session)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 403


def test_admission_route_actually_carries_the_authorization_dependency(db_session):
    """OBSERVED: a resolved dependency list, read off the route object the
    ``FastAPI()`` app actually constructed after ``include_router`` — not a
    grep over ``app/api/catalog.py``'s source text. Walks the route whose
    path is ``/offer-versions`` (POST) in the REAL mounted app's own
    ``app.routes`` and asserts ``_require_offer_version_admission`` is one
    of its resolved dependant callables.

    Break condition: fails if that dependency is ever removed from
    ``create_offer_version``'s signature (the only way FastAPI's dependant
    tree would stop naming it) — see the companion sensitivity test below
    for the behavioral half of this same property."""

    app = _mounted_app(db_session)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/v1/offer-versions"
        and "POST" in getattr(route, "methods", set())
    )
    resolved_dependency_callables = {
        dependency.call for dependency in route.dependant.dependencies
    }
    assert api_catalog._require_offer_version_admission in resolved_dependency_callables


def test_removing_the_admission_dependency_lets_an_unchecked_principal_through(
    db_session,
):
    """The planted removal, proven behaviorally. OBSERVED: the mounted
    app, via real, issued HTTP requests to two apps — the real one
    (refuses) and a second app built by registering the IDENTICAL handler
    logic with the admission dependency replaced by bare authentication.

    NARROWED CLAIM (round 14 finding 6): the earlier version of this test
    had the unguarded clone resolve ``SystemAdmission`` — the documented,
    allowlist-confined "no real principal" escape hatch — as its principal.
    That 201 proved dependency removal PLUS an authorization BYPASS (a
    principal type that skips even the command's own recheck), not literal
    dependency removal in isolation; a docstring claiming it proved the
    latter asserted more than the fixture established. This version proves
    two DIFFERENT, narrower things, each honestly scoped to what its own
    fixture does:

    1. Removing the dependency AND resolving via ``SystemAdmission``
       bypasses BOTH layers — this is exactly why ``SystemAdmission``
       construction is independently confined to an AST-checked allowlist
       elsewhere (``tests/architecture/test_offer_access_requirement_
       boundary.py``'s ``test_system_admission_construction_is_confined_
       to_the_declared_allowlist``); this route deliberately is not on
       that allowlist, which is the point.
    2. Removing ONLY the route dependency, while still resolving a REAL,
       typed, unprivileged ``StaffPrincipal`` (the shape an actual caller
       reaching an unguarded route would have), does NOT bypass
       authorization — ``admit_offer_version``'s own in-transaction
       ``verify_admission_authorization`` recheck still refuses. This is
       defense in depth actually holding, not a vulnerability; the test
       asserts 403 here, not 201.
    """

    user = _system_user(db_session)
    unprivileged_user = _system_user(db_session)
    offer = _offer(db_session)
    user_id = user.id
    offer_id = offer.id
    unprivileged_user_id = unprivileged_user.id

    # The real, guarded app: refuses.
    real_app = _mounted_app(db_session)
    real_app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    real_client = TestClient(real_app)
    guarded_response = real_client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert guarded_response.status_code == 403
    db_session.commit()

    # The planted removal: a handler with the SAME service call the real
    # route makes, registered directly with no admission dependency at
    # all — only bare authentication, which the override below satisfies.
    def _make_unguarded_app(principal_factory):
        unguarded_app = FastAPI()

        def _unguarded_create_offer_version(
            payload: OfferVersionCreate,
            db=Depends(get_db),
            auth: dict = Depends(require_user_auth),
        ):
            try:
                return catalog_service.offer_versions.create(
                    db, payload, principal=principal_factory()
                )
            except offer_access_requirement.OfferAccessRequirementError as exc:
                raise HTTPException(status_code=403, detail=exc.message) from exc

        unguarded_app.add_api_route(
            "/api/v1/offer-versions",
            _unguarded_create_offer_version,
            methods=["POST"],
            status_code=201,
        )
        unguarded_app.dependency_overrides[get_db] = lambda: db_session
        unguarded_app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
        return unguarded_app

    # (1) Dependency removal + SystemAdmission: bypasses both layers.
    system_admission_app = _make_unguarded_app(
        lambda: offer_access_requirement.SystemAdmission(
            reason="round 14 finding 6 sensitivity plant — no admission "
            "dependency guards this handler, and SystemAdmission carries "
            "no RBAC identity for the command's own recheck to refuse"
        )
    )
    system_admission_response = TestClient(system_admission_app).post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 2,
            "name": "v2",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert system_admission_response.status_code == 201, (
        "removing the dependency AND resolving via SystemAdmission bypasses "
        "both authorization layers — this is why SystemAdmission "
        "construction is independently confined to an allowlist elsewhere"
    )
    db_session.close()

    # (2) Dependency removal alone, with a real, unprivileged StaffPrincipal:
    # the command's own recheck still refuses. Defense in depth holds.
    staff_app = _make_unguarded_app(
        lambda: offer_access_requirement.StaffPrincipal(
            system_user_id=unprivileged_user_id
        )
    )
    staff_response = TestClient(staff_app).post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer_id),
            "version_number": 3,
            "name": "v3",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert staff_response.status_code == 403, (
        "removing ONLY the route dependency, with a real unprivileged "
        "principal, must still be refused by the command's own "
        "in-transaction recheck — if this ever returns 201, the command "
        "stopped enforcing independently of the route, and the route "
        "dependency alone was silently carrying all of admission's "
        "authorization"
    )


def test_changed_authorization_verdict_after_validation_refuses_patch(
    db_session, monkeypatch
):
    """Prove the mounted PATCH rechecks authorization after validation.

    The route precheck sees the real grant. At the read-only validation seam
    inside the owner command, this test changes the authorization owner's
    verdict and counts only calls made afterward. A 403 proves that a later
    recheck ran and its denial reached HTTP; a recheck moved before validation
    would miss the changed verdict.

    This is a unit seam test, not a real concurrent database revocation or a
    proof of cross-transaction visibility. That requires two PostgreSQL
    sessions and separate migration/integration evidence.
    """

    from app.services import catalog_billing_governance

    user = _system_user(db_session)
    _grant_direct_permission(
        db_session, user, offer_access_requirement.WRITE_PERMISSION
    )
    _grant_direct_permission(db_session, user, offer_access_requirement.ADMISSION_SCOPE)

    offer = _offer(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)

    create_response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert create_response.status_code == 201
    version_id = create_response.json()["id"]

    real_assert_update_safe = (
        catalog_billing_governance.assert_offer_version_update_safe
    )

    post_validation_rechecks = 0
    verdict_changed = False
    real_authorize = offer_access_requirement.authorize_offer_version_admission

    def _authorize_with_changed_verdict(db, claims, *, request_id=None):
        if verdict_changed:
            nonlocal post_validation_rechecks
            post_validation_rechecks += 1
            raise offer_access_requirement.OfferAccessRequirementError(
                code="service_intent.offer_access_requirement.permission_denied",
                message="permission revoked at validation seam",
                details={},
                retryable=False,
            )
        return real_authorize(db, claims, request_id=request_id)

    monkeypatch.setattr(
        offer_access_requirement,
        "authorize_offer_version_admission",
        _authorize_with_changed_verdict,
    )

    def _assert_update_safe_that_revokes_mid_update(db, version, changes):
        # Runs FROM INSIDE OfferVersions.update, after it has already
        # started executing (past its own entry and the earlier
        # immutability checks) and immediately BEFORE
        # verify_admission_authorization's recheck — the precise window
        # round 13 finding 3 closes. The owner transaction is already active,
        # so a same-session commit would violate its transaction guard; inject
        # the changed verdict at this validation seam instead.
        nonlocal verdict_changed
        result = real_assert_update_safe(db, version, changes)
        verdict_changed = True
        return result

    monkeypatch.setattr(
        catalog_billing_governance,
        "assert_offer_version_update_safe",
        _assert_update_safe_that_revokes_mid_update,
    )

    patch_response = client.patch(
        f"/api/v1/offer-versions/{version_id}",
        json={"name": "renamed after revocation"},
    )
    assert patch_response.status_code == 403, (
        "a changed authorization verdict injected AFTER its read-only "
        "validation seam but before verify_admission_authorization's recheck "
        "must refuse the write — a recheck moved to the TOP of update() (before "
        "this seam) would have missed the changed verdict and returned 200 "
        "instead"
    )
    assert post_validation_rechecks > 0


def test_machine_admission_succeeds_via_shadow_mode_through_the_mounted_route(
    db_session,
):
    """Round 14 finding 2: the ONLY test in this file (before this one)
    that could ever exhibit the round-13 regression this file exists to
    catch. Every other request here goes through ``_auth_for`` — always a
    staff principal, never a ``credential_kind`` — so deleting the
    ``credential_kind=auth.get("credential_kind")`` propagation at
    ``app/api/catalog.py``'s ``_require_offer_version_admission`` left
    every plant in this file, the resolver test, the authentication-stamp
    AST test, and the direct machine-command tests in
    ``tests/test_offer_access_requirement.py`` green, while a real kernel
    machine credential was silently enforced as an ordinary API key and
    refused with a 403 BEFORE ``_admission_principal`` ever built a
    ``MachineCredentialPrincipal`` at all.

    A machine credential holding scopes that satisfy NOTHING in the
    compound rule still gets a real 201 through the real mounted route —
    shadow mode never refuses (Michael's ruling; see
    ``MachineCredentialPrincipal``'s own docstring).

    Break condition: fails (403 instead of 201) if ``_require_offer_
    version_admission`` stops threading ``credential_kind`` from the auth
    dict into ``AdmissionAuthorizationClaims``, if
    ``authorize_offer_version_admission`` stops branching on
    ``claims.credential_kind == "machine"`` before enforcing the compound
    rule, or if ``_admission_principal`` stops recognizing
    ``credential_kind == "machine"`` at all."""

    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _machine_auth(scopes=[])
    client = TestClient(app)
    offer = _offer(db_session)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 201, (
        "a machine credential with no satisfying scopes must still be "
        "admitted under the shadow/compatibility path — a 403 here means "
        "credential_kind never reached the authorization decision, "
        "reintroducing the exact lockout this branch already committed "
        "a fix for once"
    )


def test_a_leave_restricted_staff_admission_is_refused_and_leaves_a_durable_audit_row(
    db_session,
):
    """Round 14 finding 4: the parity test that used to prove the denial
    behavior (staged audit event survives the rollback its own refusal
    causes) was retired to a pointer comment along with the rest of that
    file's source-grep shape, and nothing replaced its BEHAVIORAL half.

    OBSERVED: the mounted app, via a real, issued HTTP POST request, and a
    real database query for the durable audit row afterward. A staff
    principal holding every permission the compound rule needs, but under
    a REAL, planted ``ErpStaffLeaveRestriction`` row (not a mocked
    ``staff_write_restricted``), is refused — and the
    ``auth.erp_staff_leave_write_denied`` audit event this refusal
    produces is queried back from the database AFTER the request
    completes, proving it survived whatever transaction the refusal itself
    rolled back.

    Break condition: fails if the leave-restriction check is ever removed
    from ``authorize_offer_version_admission``, if
    ``record_leave_denial_evidence`` stops being called on this refusal
    path, or if the evidence it writes stops being durable."""

    user = _system_user(db_session)
    _grant_direct_permission(
        db_session, user, offer_access_requirement.WRITE_PERMISSION
    )
    _grant_direct_permission(db_session, user, offer_access_requirement.ADMISSION_SCOPE)
    restriction_id, source_system = _apply_active_leave_restriction(db_session, user)

    offer = _offer(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 403

    audit_rows = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.action == "auth.erp_staff_leave_write_denied",
            AuditEvent.entity_id == str(user.id),
        )
        .all()
    )
    assert len(audit_rows) >= 1, (
        "a leave-restriction denial through the real mounted route must "
        "leave a durable audit record behind, not just a refused response"
    )
    assert any(
        (row.metadata_ or {}).get("restriction_id") == restriction_id
        and (row.metadata_ or {}).get("source_system") == source_system
        and (row.metadata_ or {}).get("permission_key")
        == offer_access_requirement.ADMISSION_SCOPE
        for row in audit_rows
    )


def test_an_audit_write_failure_never_replaces_the_permission_denied_response(
    db_session, monkeypatch
):
    """Round 14 finding 4's other half: a failure recording denial
    evidence must never mask the real refusal underneath it. OBSERVED: the
    mounted app, via a real, issued HTTP POST request, with
    ``erp_staff_access.audit_denied_write_identity`` forced to raise.

    Break condition: fails (a 500, or any status other than 403) if
    ``record_leave_denial_evidence``'s own failure handling stops
    swallowing an evidence-recording failure and returning the original
    ``permission_denied`` refusal to the caller unchanged."""

    user = _system_user(db_session)
    _grant_direct_permission(
        db_session, user, offer_access_requirement.WRITE_PERMISSION
    )
    _grant_direct_permission(db_session, user, offer_access_requirement.ADMISSION_SCOPE)
    _apply_active_leave_restriction(db_session, user)

    def _broken_audit_denied_write_identity(db, **kwargs):
        raise RuntimeError("simulated audit-write failure")

    monkeypatch.setattr(
        erp_staff_access,
        "audit_denied_write_identity",
        _broken_audit_denied_write_identity,
    )

    offer = _offer(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 403, (
        "an audit-write failure must never surface as anything other than "
        "the original permission_denied refusal — not a 500, and not a "
        "silently-succeeded 201"
    )


def test_authorization_owner_refuses_identically_through_route_and_command(
    db_session, monkeypatch
):
    """Round 15 finding 7: the centerpiece property of this whole lane —
    that the route and the direct command delegate to the SAME
    authorization owner rather than each computing an independent
    approximation — had no actual test proving it. Only comments bearing
    this test's name existed. A regression where the direct command
    swapped ``authorize_offer_version_admission`` for a bare
    compound-permission-only check would have left the mounted
    leave-denial test and the direct unprivileged-principal tests green
    (they all reach 403 via the ordinary permission leg, which both a real
    owner and a fake one would refuse identically) while a LEAVE-RESTRICTED
    direct caller silently became authorized.

    This test injects a refusal ONLY ``authorize_offer_version_admission``'s
    leave-restriction branch can produce — a monkeypatched
    ``erp_staff_access.staff_write_restricted`` sentinel, matched to one
    specific principal who otherwise holds every permission the compound
    rule needs — and drives it through BOTH delegators for that identical
    principal:

    OBSERVED: the mounted app, via a real, issued HTTP POST request (the
    route delegator); and a direct, unmounted ``admit_offer_version`` call
    with a hand-built ``AdmitOfferVersionCommand`` (the command delegator,
    bypassing the route and the ASGI stack entirely).

    Break condition: fails if the route dependency or the direct command
    ever stops delegating to ``authorize_offer_version_admission`` — a
    compound-permission-only substitute at either call site would grant
    this exact principal, since they hold every ordinary permission the
    rule needs; only the shared leave-restriction branch refuses them."""

    user = _system_user(db_session)
    _grant_direct_permission(
        db_session, user, offer_access_requirement.WRITE_PERMISSION
    )
    _grant_direct_permission(db_session, user, offer_access_requirement.ADMISSION_SCOPE)

    sentinel_restriction = SimpleNamespace(
        restriction_id="centerpiece-sentinel", source_system="test"
    )

    def _fake_staff_write_restricted(db, auth, *, method, at=None):
        if auth.get("principal_type") == "system_user" and auth.get(
            "principal_id"
        ) == str(user_id):
            return sentinel_restriction
        return None

    monkeypatch.setattr(
        erp_staff_access, "staff_write_restricted", _fake_staff_write_restricted
    )

    offer = _offer(db_session)
    user_id = user.id
    offer_id = offer.id

    # --- Route delegator: real, mounted, issued HTTP request. ---
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)
    route_response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert route_response.status_code == 403
    db_session.close()

    # --- Command delegator: direct admit_offer_version call, no route,
    # no ASGI stack, same monkeypatched sentinel, same principal. ---
    command_id = uuid.uuid4()
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            AdmitOfferVersionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=f"system_user:{user_id}",
                    scope=offer_access_requirement.ADMISSION_SCOPE,
                    reason="round 15 finding 7 centerpiece parity test",
                ),
                payload=OfferVersionCreate(
                    offer_id=offer_id,
                    version_number=2,
                    name="v2",
                    service_type=ServiceType.residential,
                    access_type=AccessType.fiber,
                    price_basis=PriceBasis.flat,
                    access_requirement=AccessRequirement.unclassified,
                ),
                principal=StaffPrincipal(system_user_id=user_id),
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_machine_admission_never_touches_subscriber_permission_tables(
    db_session, monkeypatch
):
    """Round 15 finding 1: the machine defect had moved one layer OUT
    rather than closed. ``_shadow_check_machine_credential_admission`` was
    fixed (round 14) to read only captured scopes and never raise — but
    ``_require_offer_version_admission`` still called ``load_permission_
    keys`` UNCONDITIONALLY before constructing claims, and for a machine
    principal (``principal_type == "api_key"``), ``effective_permission_
    keys`` takes the non-system-user branch and queries ``SubscriberRole``/
    ``SubscriberPermission`` using the machine credential's UUID. If that
    table were locked or unavailable while catalog tables stayed healthy,
    a machine POST would block or 500 BEFORE the shadow check was ever
    reached — the mounted machine path still neither read only captured
    scopes nor never refused, and the earlier mounted test (a healthy
    database) could not expose it.

    OBSERVED: the mounted app, via a real, issued HTTP POST request, with
    ``load_permission_keys`` forced to raise if called at all.

    Break condition: fails (an error instead of 201) if
    ``_require_offer_version_admission`` ever goes back to calling
    ``load_permission_keys`` unconditionally, before checking
    ``credential_kind``."""

    def _load_permission_keys_that_must_not_be_called(auth, db):
        raise AssertionError(
            "load_permission_keys must not be called for a machine "
            "credential — it queries SubscriberRole/SubscriberPermission "
            "using the machine credential's UUID, a table this path has "
            "no business touching"
        )

    monkeypatch.setattr(
        api_catalog,
        "load_permission_keys",
        _load_permission_keys_that_must_not_be_called,
    )

    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _machine_auth(scopes=[])
    client = TestClient(app)
    offer = _offer(db_session)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 201
