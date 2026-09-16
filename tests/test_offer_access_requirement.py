"""service_intent.offer_access_requirement — admission, immutability, and the
reviewed classification command.

Transaction-boundary discipline: every helper that reads or writes through
this session leaves it transaction-free before returning (an explicit
``db_session.commit()``/``.rollback()``), because ``execute_owner_command``
requires a transaction-free session at entry and rejects an active caller
transaction (``app/services/owner_commands.py``). This mirrors the existing
pattern in ``tests/test_offer_reseller_availability.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select

from app.db import finish_read_transaction
from app.models.auth import ApiKey
from app.models.catalog import (
    AccessRequirement,
    AccessType,
    OfferAccessRequirementClassification,
    OfferVersion,
    PriceBasis,
    ServiceType,
)
from app.models.idempotency import IdempotencyKey
from app.models.rbac import (
    Permission,
    Role,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    OfferVersionUpdate,
)
from app.services import catalog as catalog_service
from app.services.catalog import offer_access_requirement
from app.services.catalog.offer_access_requirement import (
    ADMISSION_SCOPE,
    CLASSIFY_PERMISSION,
    AdmitOfferVersionCommand,
    ApiKeyPrincipal,
    ClassifyOfferAccessRequirementCommand,
    OfferAccessRequirementError,
    PreviewClassifyOfferAccessRequirementQuery,
    StaffPrincipal,
    SubscriberPrincipal,
    SystemAdmission,
    admit_offer_version,
    classify_offer_version_access_requirement,
    list_unclassified_offer_versions,
    preview_classify_offer_version_access_requirement,
    principal_label,
)
from app.services.owner_commands import CommandContext, OwnerCommandError


@pytest.fixture(autouse=True)
def _owner_command_session(db_session):
    """Keep fixture identities usable after explicit owner-command commits."""

    original_expiry = db_session.expire_on_commit
    db_session.expire_on_commit = False
    try:
        yield
    finally:
        db_session.expire_on_commit = original_expiry


def _make_offer(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100",
            code=f"FIBER-{uuid4().hex[:8]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    # Offers.create refreshes after its own commit, leaving a read transaction.
    # Commit that transaction without expiring this setup offer: the tests
    # pass its ID into a registered command, and a lazy refresh of an expired
    # ID would reopen a caller transaction before that command can start.
    original_expiry = db_session.expire_on_commit
    try:
        db_session.expire_on_commit = False
        db_session.commit()
    finally:
        db_session.expire_on_commit = original_expiry
    return offer


def _make_version(db_session, offer, *, access_requirement, version_number=1):
    # Keep the FK a primitive and settle any unrelated read transaction
    # before entering the registered command.
    identity = inspect(offer).identity
    assert identity is not None and len(identity) == 1
    offer_id = identity[0]
    finish_read_transaction(db_session)
    version = catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer_id,
            version_number=version_number,
            name=f"Fiber 100 v{version_number}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=access_requirement,
        ),
        principal=SystemAdmission(reason="test fixture"),
    )
    db_session.commit()
    return version


def _admin_system_user(db_session) -> SystemUser:
    """A real, active staff principal with the ``admin`` role.

    ``admin`` satisfies every permission automatically
    (``app/services/auth_dependencies.py::has_permission``) — this is the
    repo's own wildcard/admin convention, not something this change invents.
    """

    user = SystemUser(
        first_name="Test",
        last_name="Admin",
        email=f"admin-{uuid4().hex[:8]}@example.com",
    )
    role = Role(name="admin", is_active=True)
    db_session.add_all([user, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    return user


def _unprivileged_system_user(db_session) -> SystemUser:
    """A real, active staff principal with NO classify permission or role."""

    user = SystemUser(
        first_name="Test",
        last_name="Unprivileged",
        email=f"unprivileged-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


def _staff_with_direct_permissions(db_session, *permission_keys: str) -> SystemUser:
    """A real, active staff principal holding exactly ``permission_keys`` as
    DIRECT ``SystemUserPermission`` grants — no ``admin`` role, no wildcard,
    no role-mediated grant at all. Lets a test hold a caller to EXACTLY the
    compatibility-leg permissions (e.g. ``catalog:write`` +
    ``catalog:billing_write``, deliberately withholding
    ``catalog:offer_version:admission``) rather than the ``admin`` role's
    blanket bypass, which would satisfy the compound rule for a reason
    unrelated to the specific OR-leg under test."""

    user = SystemUser(
        first_name="Test",
        last_name="DirectGrant",
        email=f"direct-grant-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.flush()
    for key in permission_keys:
        permission = db_session.scalar(select(Permission).where(Permission.key == key))
        if permission is None:
            permission = Permission(key=key, description=f"test grant: {key}")
            db_session.add(permission)
            db_session.flush()
        db_session.add(
            SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
        )
    db_session.commit()
    return user


def _make_subscriber(db_session) -> Subscriber:
    from app.services.subscriber import _default_reseller_id

    subscriber = Subscriber(
        first_name="Test",
        last_name="Subscriber",
        email=f"subscriber-{uuid4().hex[:8]}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.commit()
    return subscriber


def _admin_subscriber(db_session) -> Subscriber:
    """A real, active subscriber mapped to the ``admin`` role — the seeded
    subscriber-admin path Decision 2 preserves
    (``scripts/seed/seed_rbac.py``, ``app/services/subscriber_assignments.py``,
    ``app/services/auth_flow.py``'s login role resolution)."""

    subscriber = _make_subscriber(db_session)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SubscriberRole(subscriber_id=subscriber.id, role_id=role.id))
    db_session.commit()
    return subscriber


def _unprivileged_subscriber(db_session) -> Subscriber:
    """A real, active subscriber with NO role granting the compound
    admission permission."""

    return _make_subscriber(db_session)


def _admission_api_key(db_session, *, scopes: list[str]) -> ApiKey:
    api_key = ApiKey(
        key_hash=f"test-hash-{uuid4().hex}",
        scopes=scopes,
        is_active=True,
    )
    db_session.add(api_key)
    db_session.commit()
    return api_key


def _context(**overrides):
    command_id = overrides.pop("command_id", uuid4())
    defaults = dict(
        command_id=command_id,
        correlation_id=command_id,
        actor="placeholder",
        scope=CLASSIFY_PERMISSION,
        reason="reviewed with network ops",
        idempotency_key=f"classify-{uuid4()}",
    )
    defaults.update(overrides)
    return CommandContext(**defaults)


def _preview(db_session, version, proposed, review_reference="JIRA-42"):
    preview = preview_classify_offer_version_access_requirement(
        db_session,
        PreviewClassifyOfferAccessRequirementQuery(
            offer_version_id=version.id,
            proposed_access_requirement=proposed,
            review_reference=review_reference,
        ),
    )
    # This is a read-only boundary. Commit it without expiring setup entities,
    # so later access to their IDs cannot implicitly reopen a caller tx.
    db_session.commit()
    return preview


def test_offer_adapter_returns_transaction_free_with_production_expiry(db_session):
    """The adapter does not refresh after the owner command boundary.

    With SQLAlchemy's production default, the immediate return is
    transaction-free; accessing an expired ORM attribute deliberately opens a
    read transaction, and the next owner command refuses that ambient state.
    """

    offer = _make_offer(db_session)
    offer_id = offer.id
    db_session.expire_on_commit = True
    db_session.commit()

    result = catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer_id,
            version_number=99,
            name="Production expiry canary",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=AccessRequirement.unclassified,
        ),
        principal=SystemAdmission(reason="production expiry canary"),
    )
    assert db_session.in_transaction() is False
    _ = result.id
    assert db_session.in_transaction() is True
    with pytest.raises(OwnerCommandError, match="transaction-free"):
        catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer_id,
                version_number=100,
                name="Ambient transaction refusal",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="ambient transaction refusal"),
        )
    db_session.rollback()


def test_offer_update_adapter_returns_transaction_free_with_production_expiry(
    db_session,
):
    """Updating must not refresh the committed row into a new read transaction."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    identity = inspect(version).identity
    assert identity is not None and len(identity) == 1
    version_id = identity[0]
    assert db_session.in_transaction() is False

    db_session.expire_on_commit = True
    updated = catalog_service.offer_versions.update(
        db_session,
        str(version_id),
        OfferVersionUpdate(name="Updated with production expiry"),
        principal=SystemAdmission(reason="production update expiry canary"),
    )
    assert db_session.in_transaction() is False
    assert inspect(updated).identity == (version_id,)
    assert updated.name == "Updated with production expiry"
    assert db_session.in_transaction() is True
    db_session.rollback()


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_offer_version_create_requires_explicit_access_requirement(db_session):
    offer = _make_offer(db_session)
    with pytest.raises(ValidationError):
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=1,
            name="Fiber 100 v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )


def test_offer_version_create_accepts_unclassified_explicitly(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    assert version.access_requirement is AccessRequirement.unclassified


def test_offer_version_create_accepts_a_real_classification_directly(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    assert version.access_requirement is AccessRequirement.network_access


def test_offer_version_update_schema_has_no_access_requirement_field():
    assert "access_requirement" not in OfferVersionUpdate.model_fields


def test_offer_version_update_never_mutates_access_requirement(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    updated = catalog_service.offer_versions.update(
        db_session,
        str(version.id),
        OfferVersionUpdate(name="Renamed"),
        principal=SystemAdmission(reason="test fixture"),
    )
    db_session.commit()
    assert updated.access_requirement is AccessRequirement.unclassified
    assert updated.name == "Renamed"


# --------------------------------------------------------------------------
# Worklist
# --------------------------------------------------------------------------


def test_worklist_reports_only_unclassified_rows(db_session):
    offer = _make_offer(db_session)
    unclassified = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    _make_version(
        db_session,
        offer,
        access_requirement=AccessRequirement.network_access,
        version_number=2,
    )
    worklist = list_unclassified_offer_versions(db_session, limit=50, offset=0)
    db_session.rollback()
    ids = {row.offer_version_id for row in worklist.rows}
    assert unclassified.id in ids
    assert worklist.total_count == len(worklist.rows)


def test_worklist_rejects_an_out_of_range_page(db_session):
    with pytest.raises(OfferAccessRequirementError):
        list_unclassified_offer_versions(db_session, limit=0, offset=0)
    with pytest.raises(OfferAccessRequirementError):
        list_unclassified_offer_versions(db_session, limit=10, offset=-1)


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def test_preview_refuses_a_target_of_unclassified(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.unclassified)
    assert excinfo.value.code.endswith("invalid_target_classification")


def test_preview_refuses_a_missing_offer_version(db_session):
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        preview_classify_offer_version_access_requirement(
            db_session,
            PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=uuid4(),
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-1",
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("offer_version_not_found")


def test_preview_refuses_an_already_classified_version_with_no_recorded_row(
    db_session,
):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.no_network_access)
    assert excinfo.value.code.endswith("already_classified")


# --------------------------------------------------------------------------
# Reviewed classification command
# --------------------------------------------------------------------------


def test_classify_denies_an_unprivileged_authenticated_principal(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _unprivileged_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_classify_denies_an_inactive_system_user(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    user.is_active = False
    db_session.commit()
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_classify_applies_the_reviewed_transition_and_records_the_real_principal(
    db_session,
):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    outcome = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    assert outcome.replayed is False
    assert outcome.new_access_requirement is AccessRequirement.network_access

    row = db_session.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    assert row is not None
    assert row.new_access_requirement is AccessRequirement.network_access
    # The recorded identity is the AUTHENTICATED principal, never a
    # caller-supplied free-text actor string.
    assert row.classified_by == principal_label(user.id)
    db_session.rollback()

    refreshed = db_session.get(OfferVersion, version.id)
    assert refreshed.access_requirement is AccessRequirement.network_access
    db_session.rollback()


def test_classify_refuses_a_stale_preview(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("stale_preview")


def test_classify_refuses_real_to_real_with_no_recorded_row(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    user = _admin_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("already_classified")


def test_classify_exact_replay_is_reachable_through_preview_then_apply(db_session):
    """The real regression this proves: a genuine retry — SAME idempotency
    key, same material inputs — reaches the replay branch instead of being
    blocked at the preview gate, because preview itself recognizes an
    already-recorded matching transition."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    fixed_key = f"fixed-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert first_preview.already_applied is False
    first_context = _context(idempotency_key=fixed_key)
    first_query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    first = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=first_context,
            query=first_query,
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    assert first.replayed is False
    db_session.rollback()

    # Simulate the client losing the response and retrying: preview again
    # (this is exactly what the CLI does before every apply) with the SAME
    # material inputs.
    retry_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert retry_preview.already_applied is True

    retry_context = _context(idempotency_key=fixed_key)
    second = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=retry_context,
            query=first_query,
            expected_preview_fingerprint=retry_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()
    assert second.replayed is True
    assert second.new_access_requirement is AccessRequirement.network_access

    rows = db_session.scalars(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    ).all()
    db_session.rollback()
    assert len(rows) == 1


def test_classify_refuses_a_key_reused_for_a_different_offer_version(db_session):
    offer = _make_offer(db_session)
    first_version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    second_version = _make_version(
        db_session,
        offer,
        access_requirement=AccessRequirement.unclassified,
        version_number=2,
    )
    user = _admin_system_user(db_session)
    shared_key = f"shared-key-{uuid4()}"

    first_preview = _preview(
        db_session, first_version, AccessRequirement.network_access
    )
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=shared_key),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=first_version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    second_preview = _preview(
        db_session, second_version, AccessRequirement.network_access
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=shared_key),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=second_version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=second_preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")


def test_classify_refuses_a_key_reused_for_a_different_target(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    shared_key = f"shared-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=shared_key),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    # The row is now real-to-real from this same key's perspective; the
    # already_classified branch is reached first here because the version is
    # no longer unclassified with a DIFFERENT proposed target — proving the
    # key is not what silently overrides target immutability.
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=shared_key),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code in (
        "service_intent.offer_access_requirement.idempotency_conflict",
        "service_intent.offer_access_requirement.already_classified",
    )


def test_classify_refuses_a_different_key_after_a_real_transition(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=f"key-one-{uuid4()}"),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    retry_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert retry_preview.already_applied is True
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=f"key-two-{uuid4()}"),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=retry_preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("already_classified")


def test_classify_reverifies_permission_after_the_row_lock_not_only_at_entry(
    db_session, monkeypatch
):
    """Regression for the permission-revocation race: before this fix,
    ``_verify_classify_permission`` ran exactly ONCE, at the top of
    ``_classify``, before the offer version was locked. This asserts it now
    runs a SECOND time — after the lock, immediately before the write. This
    NARROWS but does NOT ELIMINATE the revoke-during-apply race: no RBAC row
    (system_users, roles, role_permissions, permissions) is locked, so a
    grant revoked in the instant between this second check and the commit is
    still not caught (see the SOT manifest's own honest disclosure,
    ``service_intent_control_plane.py``'s ``locking=`` contract). Fails
    before the fix (call count 1, not 2)."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    calls: list[object] = []
    real_verify = offer_access_requirement._verify_classify_permission

    def _counting_verify(db, system_user_id):
        calls.append(system_user_id)
        return real_verify(db, system_user_id)

    monkeypatch.setattr(
        offer_access_requirement, "_verify_classify_permission", _counting_verify
    )

    outcome = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()
    assert outcome.replayed is False
    assert len(calls) == 2
    assert calls == [user.id, user.id]


def test_classify_refuses_a_replay_with_a_different_review_reference(db_session):
    """Regression for the replay/review_reference gap: previewing and then
    applying with the SAME idempotency key but a DIFFERENT review_reference
    than the one actually recorded must not silently substitute the stored
    reference — it must be refused. Fails before the fix (the preview
    silently returned the stored reference regardless of what was supplied)."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    fixed_key = f"fixed-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    first_query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=fixed_key),
            query=first_query,
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    # Preview again with a DIFFERENT review_reference for the same already-
    # applied transition.
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.network_access, "JIRA-99")
    db_session.rollback()
    assert excinfo.value.code.endswith("review_reference_mismatch")


def test_classify_refuses_an_oversized_idempotency_key(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key="x" * 121),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_key_too_long")


def test_preview_refuses_an_oversized_review_reference(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.network_access, "x" * 201)
    db_session.rollback()
    assert excinfo.value.code.endswith("review_reference_too_long")


# --------------------------------------------------------------------------
# Admission authorization and duplicate (offer_id, version_number) refusal
# --------------------------------------------------------------------------


def _admit_command(offer, version_number, *, principal=None):
    command_id = uuid4()
    resolved_principal = principal or SystemAdmission(reason="test admission")
    return AdmitOfferVersionCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="system:test",
            scope=ADMISSION_SCOPE,
            reason="test admission",
        ),
        payload=OfferVersionCreate(
            offer_id=offer.id,
            version_number=version_number,
            name=f"Fiber 100 v{version_number}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=AccessRequirement.unclassified,
        ),
        principal=resolved_principal,
    )


def test_admit_requires_an_explicit_principal():
    """Regression for the actor-spoofing gap's real fix: authorization moved
    to the route layer entirely, and the command now requires an explicit,
    typed principal — there is no default that lets a caller omit it."""

    with pytest.raises(TypeError):
        AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
            ),
            payload=OfferVersionCreate(
                offer_id=uuid4(),
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
        )


def test_admit_with_system_admission_is_not_rbac_gated(db_session):
    """Existing internal/system-initiated convention is preserved:
    ``SystemAdmission`` — and only ``SystemAdmission`` — is exempt from
    ``_verify_admission_authorization``'s RBAC re-check (see its own
    docstring). This is NOT "the command makes no authorization decision at
    all": round 11 added a real, in-transaction RBAC re-check
    (``_verify_admission_authorization``) for every OTHER principal type —
    see ``test_admit_denies_an_unprivileged_staff_principal`` and its
    siblings below. Matches every pre-existing caller of
    ``offer_versions.create``/``admit_offer_version`` with no actor, e.g.
    ``tests/conftest.py``'s shared ``catalog_offer`` fixture."""

    offer = _make_offer(db_session)
    result = admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()
    assert result.replayed is False
    assert result.offer_version.access_requirement is AccessRequirement.unclassified


def test_admit_denies_an_unprivileged_staff_principal(db_session):
    """Decision 1 (round 11): the command now re-verifies the claimed
    principal against RBAC itself, inside its own transaction. An
    unprivileged system_user — no catalog:write, no catalog:billing_write
    or catalog:offer_version:admission — is refused here even though this
    call bypasses the route entirely, proving the check is real command-level
    enforcement, not merely the route's."""

    offer = _make_offer(db_session)
    user = _unprivileged_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_accepts_a_privileged_claimed_staff_principal(db_session):
    offer = _make_offer(db_session)
    user = _admin_system_user(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_accepts_the_catalog_write_plus_billing_write_compatibility_leg(
    db_session,
):
    """Owner-command-level proof of the BASELINE caller population: a staff
    principal holding exactly ``catalog:write`` + ``catalog:billing_write``
    — no ``catalog:offer_version:admission``, no ``admin`` role, no
    wildcard — is accepted directly by ``admit_offer_version``. Before this
    test, only the ROUTE (``tests/test_offer_version_admission_route_
    permissions.py``) exercised this leg; the owner command's own
    compound-rule re-check had no test proving it accepts the shape every
    pre-existing ``catalog:billing_write`` caller already held prior to
    ``catalog:offer_version:admission`` ever being introduced.

    Break condition: this fails if ``_admission_permission_granted``'s OR
    ever drops (or narrows) the ``catalog:billing_write`` branch — e.g. a
    future edit that requires ``catalog:offer_version:admission``
    unconditionally would leave every OTHER focused test in this file green
    (they all use ``admin``, the narrower admission scope, or refusal cases)
    while silently locking out the entire pre-existing
    ``catalog:billing_write`` caller population this compound rule was
    designed to keep working."""

    offer = _make_offer(db_session)
    user = _staff_with_direct_permissions(
        db_session,
        offer_access_requirement.WRITE_PERMISSION,
        offer_access_requirement.BILLING_WRITE_PERMISSION,
    )

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_denies_a_nonexistent_api_key(db_session):
    """A principal naming an api_key_id with no backing row at all is
    refused — distinct from (and previously conflated with) an existing key
    that simply lacks the required scope; see
    ``test_admit_denies_an_api_key_with_no_matching_scope`` below for that
    case."""

    offer = _make_offer(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=uuid4())),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_denies_an_api_key_with_no_matching_scope(db_session):
    """An EXISTING, active API key whose scopes do not satisfy the compound
    rule is refused by the command's own re-check for that reason —
    ``catalog:write`` alone is not enough without either
    ``catalog:billing_write`` or ``catalog:offer_version:admission``.

    Previously this test used a nonexistent random ``api_key_id`` (now
    covered separately by ``test_admit_denies_a_nonexistent_api_key`` above)
    and so never actually exercised "a real key with an insufficient scope"
    at all — a denial was observed, but for the wrong reason."""

    offer = _make_offer(db_session)
    api_key = _admission_api_key(db_session, scopes=["catalog:write"])

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=api_key.id)),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_accepts_an_api_key_with_the_compound_scope(db_session):
    """An active API key whose scopes satisfy catalog:write AND
    catalog:offer_version:admission is accepted."""

    offer = _make_offer(db_session)
    api_key = _admission_api_key(db_session, scopes=["catalog:write", ADMISSION_SCOPE])

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=api_key.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_accepts_a_subscriber_principal_mapped_to_the_admin_role(db_session):
    """Decision 2 (round 11): a subscriber mapped to the ``admin`` role via
    the seeded subscriber-admin path is a preserved, supported caller — not
    silently refused."""

    offer = _make_offer(db_session)
    subscriber = _admin_subscriber(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(
            offer, 1, principal=SubscriberPrincipal(subscriber_id=subscriber.id)
        ),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_denies_an_unprivileged_subscriber_principal(db_session):
    """A subscriber with no role granting the compound admission permission
    is refused by the command's own re-check, matching the other principal
    types' negative tests."""

    offer = _make_offer(db_session)
    subscriber = _unprivileged_subscriber(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(
                offer, 1, principal=SubscriberPrincipal(subscriber_id=subscriber.id)
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


#: test_authorization_owner_refuses_identically_through_route_and_command used
#: to live here. Round 13 finding 4: it called
#: app.api.catalog._require_offer_version_admission directly and inspected
#: admission_router.routes/.dependencies (router membership) instead of
#: issuing a real request through the mounted app -- the same
#: source-grep/direct-call shape that let round 13's credential_kind
#: lockout ship unnoticed through three rounds of "parity" tests. It now
#: lives in tests/test_offer_version_admission_asgi.py under the SAME
#: name, driving both a real mounted HTTP request and a direct
#: admit_offer_version call through one injected sentinel.


def test_admit_accepts_a_machine_credential_with_no_satisfying_scope(db_session):
    """Michael's ruling: machine principals stay on a shadowed, inventoried
    migration path with NO immediate refusal. A ``MachineCredentialPrincipal``
    holding scopes that satisfy NOTHING in the compound rule still succeeds —
    admission proceeds under the compatibility path exactly as it did before
    this module had any command-level check at all (``origin/main``
    authorized this same shape). The decision is only logged (shadow/
    would-refuse), never enforced.

    Break condition: this fails the moment ``MachineCredentialPrincipal`` is
    ever enforced against — i.e. the exact uncensused, silent access
    retirement Michael's ruling explicitly forbids landing without an
    inventory, a scope migration, and a reviewed enforcement switch first."""

    offer = _make_offer(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(
            offer,
            1,
            principal=offer_access_requirement.MachineCredentialPrincipal(
                credential_id=uuid4(), scopes=()
            ),
        ),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_machine_credential_shadow_ignores_a_coincidentally_matching_subscriber_grant(
    db_session, monkeypatch
):
    """Round 14 finding 3: the shadow evaluation must read ONLY the
    captured scope snapshot, never a live authority table. Plants a real
    ``Subscriber`` row whose id EQUALS the machine credential's id and
    grants THAT subscriber the full compound permission directly — if the
    shadow check ever went back to the DB-backed ``has_permission`` (whose
    non-system_user branch queries ``SubscriberRole``/
    ``SubscriberPermission`` keyed by ``principal_id``), it would read this
    coincidental grant and report the credential "would be authorized"
    even though the credential's OWN captured scopes (empty) authorize
    nothing — a diagnostic that can lie about the very thing it exists to
    report.

    Break condition: fails if the shadow check is ever changed back to
    query any RBAC table instead of ``claims.scopes`` alone — it would log
    "would be authorized" instead of "WOULD REFUSE" for this exact
    credential."""

    credential_id = uuid4()
    subscriber = Subscriber(
        id=credential_id,
        first_name="Coincidence",
        last_name="Collision",
        email=f"collide-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    for key in (
        offer_access_requirement.WRITE_PERMISSION,
        offer_access_requirement.ADMISSION_SCOPE,
    ):
        permission = db_session.scalar(select(Permission).where(Permission.key == key))
        if permission is None:
            permission = Permission(key=key, description=f"test grant: {key}")
            db_session.add(permission)
            db_session.flush()
        db_session.add(
            SubscriberPermission(
                subscriber_id=subscriber.id, permission_id=permission.id
            )
        )
    db_session.commit()

    logged_messages: list[str] = []
    monkeypatch.setattr(
        offer_access_requirement.logger,
        "warning",
        lambda msg, *args, **kwargs: logged_messages.append(msg % args),
    )
    monkeypatch.setattr(
        offer_access_requirement.logger,
        "info",
        lambda msg, *args, **kwargs: logged_messages.append(msg % args),
    )

    offer = _make_offer(db_session)
    result = admit_offer_version(
        db_session,
        _admit_command(
            offer,
            1,
            principal=offer_access_requirement.MachineCredentialPrincipal(
                credential_id=credential_id, scopes=()
            ),
        ),
    )
    db_session.rollback()

    # Admission still proceeds regardless (shadow never refuses)...
    assert result.offer_version.offer_id == offer.id
    # ...but the LOGGED diagnostic must say it would REFUSE, matching the
    # credential's own (empty) scopes, not the coincidental subscriber
    # grant a live DB query would have found.
    assert any("WOULD REFUSE" in message for message in logged_messages), (
        f"shadow check logged {logged_messages!r}, expected a WOULD REFUSE "
        "entry reflecting the credential's own empty scopes"
    )
    assert not any("would be authorized" in message for message in logged_messages)


def test_machine_credential_shadow_never_raises_even_if_its_evaluation_would(
    db_session, monkeypatch
):
    """Round 14 finding 3: an error inside the shadow evaluation itself
    must never escape as an exception (and therefore never as a 500 in
    front of admission) — it is a diagnostic aid, not a gate.

    Break condition: fails if the shadow check stops catching an exception
    raised by its own evaluation logic, or if such an exception is allowed
    to propagate out of ``admit_offer_version``."""

    def _broken_expand_permission_keys(permission_key):
        raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(
        "app.services.auth_dependencies._expand_permission_keys",
        _broken_expand_permission_keys,
    )

    offer = _make_offer(db_session)
    result = admit_offer_version(
        db_session,
        _admit_command(
            offer,
            1,
            principal=offer_access_requirement.MachineCredentialPrincipal(
                credential_id=uuid4(), scopes=()
            ),
        ),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_machine_credential_is_the_only_shadow_mode_admission_principal(db_session):
    """Exact, non-growing compatibility path proof.

    Constructs a deliberately unauthorized instance of EVERY member of the
    closed ``AdmissionPrincipal`` union and asserts exactly two bypass real
    enforcement: ``SystemAdmission`` (has no RBAC identity to check at all)
    and ``MachineCredentialPrincipal`` (shadow/would-refuse migration,
    Michael's ruling). Every other member — ``StaffPrincipal``,
    ``ApiKeyPrincipal``, ``SubscriberPrincipal`` — is refused.

    Break condition: this fails if the compatibility path silently widens to
    exempt a THIRD principal type (e.g. someone adding ``ApiKeyPrincipal`` to
    the same isinstance branch as a shortcut), and it fails if
    ``MachineCredentialPrincipal`` itself starts being enforced against
    (collapsing the shadow migration back into an immediate, uncensused
    refusal) — either direction of drift trips it."""

    offer = _make_offer(db_session)
    bypassed: set[type] = set()
    refused: set[type] = set()

    unauthorized_principals = (
        offer_access_requirement.StaffPrincipal(system_user_id=uuid4()),
        offer_access_requirement.ApiKeyPrincipal(api_key_id=uuid4()),
        offer_access_requirement.SubscriberPrincipal(subscriber_id=uuid4()),
        offer_access_requirement.MachineCredentialPrincipal(
            credential_id=uuid4(), scopes=()
        ),
        offer_access_requirement.SystemAdmission(reason="non-growth probe"),
    )
    for index, principal in enumerate(unauthorized_principals):
        db_session.close()
        try:
            admit_offer_version(
                db_session,
                _admit_command(offer, 100 + index, principal=principal),
            )
        except OfferAccessRequirementError as exc:
            assert exc.code.endswith("permission_denied")
            refused.add(type(principal))
        else:
            bypassed.add(type(principal))
        finally:
            db_session.commit()

    assert bypassed == {
        offer_access_requirement.SystemAdmission,
        offer_access_requirement.MachineCredentialPrincipal,
    }
    assert refused == {
        offer_access_requirement.StaffPrincipal,
        offer_access_requirement.ApiKeyPrincipal,
        offer_access_requirement.SubscriberPrincipal,
    }


def test_admit_stages_a_machine_admission_with_api_key_actor_type_not_system(
    db_session,
):
    """Machine admission retains credential attribution in the typed audit
    actor rather than passing a free-text class through a fallback parser.

    Break condition: the principal must yield an api_key actor with its exact
    credential id, not an anonymous system actor or a descriptive label.
    """

    from app.models.audit import AuditActorType

    principal = offer_access_requirement.MachineCredentialPrincipal(
        credential_id=uuid4(), scopes=(offer_access_requirement.ADMISSION_SCOPE,)
    )
    actor = offer_access_requirement._admission_actor_evidence(principal)
    assert actor.actor_id == str(principal.credential_id)
    assert actor.actor_type is AuditActorType.api_key


def test_billing_governance_passes_a_typed_actor_to_the_audit_owner(
    db_session, monkeypatch, caplog
):
    """The new Offer path must not turn its typed actor back into scalars.

    Break condition: the staged audit receives the exact typed actor and no
    legacy id/type fields; a scalar-only bridge cannot satisfy this check.
    """
    from app.services import catalog_billing_governance as governance

    principal = offer_access_requirement.MachineCredentialPrincipal(
        credential_id=uuid4(), scopes=(offer_access_requirement.ADMISSION_SCOPE,)
    )
    actor = offer_access_requirement._admission_actor_evidence(principal)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        governance, "stage_audit_event", lambda _db, **kw: calls.append(kw)
    )
    monkeypatch.setattr(governance, "record_metric", lambda **_kw: None)
    monkeypatch.setattr(governance, "record_finding", lambda *_args, **_kw: None)

    governance.stage_billing_catalog_change(
        db_session,
        action="version_created",
        entity_type="offer_version",
        entity_id=uuid4(),
        actor=actor,
    )

    assert len(calls) == 1
    assert calls[0]["actor"] is actor
    assert "actor_id" not in calls[0]
    assert "actor_type" not in calls[0]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == governance.__name__
        and record.getMessage().startswith("catalog_billing_change ")
    ]
    assert len(warnings) == 1
    assert (
        f"actor_type={actor.actor_type.value} actor_id={actor.actor_id}" in warnings[0]
    )


def test_billing_governance_refuses_a_typed_and_scalar_actor_mix(db_session):
    from app.services import catalog_billing_governance as governance
    from app.services.audit_adapter import AuditActor

    with pytest.raises(ValueError, match="typed audit actor cannot be combined"):
        governance.stage_billing_catalog_change(
            db_session,
            action="version_created",
            entity_type="offer_version",
            entity_id=uuid4(),
            actor=AuditActor.user(str(uuid4())),
            actor_id="legacy-id",
        )


def test_admit_refuses_a_duplicate_offer_id_and_version_number(db_session):
    """Regression: before this fix, retrying an admission with the SAME
    (offer_id, version_number) silently created a second row — there was no
    uniqueness check anywhere in ``_admit`` and no database constraint
    either. This must now be a typed conflict, and no second row is
    created."""

    offer = _make_offer(db_session)
    admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()
    assert excinfo.value.code.endswith("duplicate_version_number")

    rows = db_session.scalars(
        select(OfferVersion).where(
            OfferVersion.offer_id == offer.id,
            OfferVersion.version_number == 1,
        )
    ).all()
    db_session.rollback()
    assert len(rows) == 1


def test_admit_replay_with_matching_key_and_payload_returns_the_original_row(
    db_session,
):
    """End-to-end admission idempotency: an exact-key, exact-payload retry
    returns the ORIGINAL row (``replayed=True``) instead of a
    ``duplicate_version_number`` conflict."""

    offer = _make_offer(db_session)
    key = f"admit-{uuid4()}"
    command_id = uuid4()

    def _command():
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        )

    first = admit_offer_version(db_session, _command())
    db_session.rollback()
    assert first.replayed is False

    second = admit_offer_version(db_session, _command())
    db_session.rollback()
    assert second.replayed is True
    assert second.offer_version.id == first.offer_version.id


def test_admit_replay_with_matching_key_but_different_payload_is_a_typed_conflict(
    db_session,
):
    """End-to-end admission idempotency: the SAME key reused for a
    DIFFERENT admission payload is a typed ``idempotency_conflict``, never a
    silent substitution or a raw database error."""

    offer = _make_offer(db_session)
    key = f"admit-{uuid4()}"

    admit_offer_version(
        db_session,
        AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        ),
    )
    db_session.rollback()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            AdmitOfferVersionCommand(
                context=CommandContext(
                    command_id=uuid4(),
                    correlation_id=uuid4(),
                    actor="system:test",
                    scope=ADMISSION_SCOPE,
                    reason="test admission",
                    idempotency_key=key,
                ),
                payload=OfferVersionCreate(
                    offer_id=offer.id,
                    version_number=2,
                    name="Fiber 100 v2 (different)",
                    service_type=ServiceType.residential,
                    access_type=AccessType.fiber,
                    price_basis=PriceBasis.flat,
                    access_requirement=AccessRequirement.unclassified,
                ),
                principal=SystemAdmission(reason="test admission"),
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")


def test_admission_fingerprint_distinguishes_omitted_from_explicit_default(
    db_session,
):
    """Regression for round 7's fix: an omitted optional field (e.g.
    ``billing_cycle``, left for ``_admit``'s settings-resolved default to
    decide) must fingerprint DIFFERENTLY from an explicit value that happens
    to equal that same schema default — ``model_dump()`` alone makes them
    indistinguishable, since Pydantic fills every omitted field with its
    schema default before ``_admission_fingerprint`` ever sees it."""

    offer = _make_offer(db_session)
    base_kwargs = dict(
        offer_id=offer.id,
        version_number=1,
        name="Fiber 100 v1",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        access_requirement=AccessRequirement.unclassified,
    )
    omitted = OfferVersionCreate(**base_kwargs)
    explicit_default = OfferVersionCreate(
        **base_kwargs, billing_cycle=omitted.billing_cycle
    )
    assert "billing_cycle" not in omitted.model_fields_set
    assert "billing_cycle" in explicit_default.model_fields_set

    omitted_fingerprint = offer_access_requirement._admission_fingerprint(omitted)
    explicit_fingerprint = offer_access_requirement._admission_fingerprint(
        explicit_default
    )
    assert omitted_fingerprint != explicit_fingerprint


def test_admit_stages_the_admitted_event_only_for_a_fresh_admission(
    db_session, monkeypatch
):
    """Regression for the typed-outcome/event fix: a genuinely NEW admission
    stages ``catalog.offer_version_admitted`` exactly once; an idempotent
    replay of the SAME command does not re-emit it."""

    from app.services.events import types as event_types

    emitted: list[object] = []
    real_emit_event = offer_access_requirement.emit_event

    def _recording_emit_event(db, event_type, payload, **kwargs):
        emitted.append(event_type)
        return real_emit_event(db, event_type, payload, **kwargs)

    monkeypatch.setattr(offer_access_requirement, "emit_event", _recording_emit_event)

    offer = _make_offer(db_session)
    key = f"admit-event-{uuid4()}"

    def _command():
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        )

    admit_offer_version(db_session, _command())
    db_session.rollback()
    admit_offer_version(db_session, _command())
    db_session.rollback()

    assert emitted == [event_types.EventType.catalog_offer_version_admitted]


def test_admit_command_rejects_a_principal_outside_the_closed_union():
    """Regression for the runtime-validation gap: the closed union
    (``StaffPrincipal | ApiKeyPrincipal | SystemAdmission``) is only a
    static type hint — passing ``principal=None`` or any arbitrary object
    must raise here, not just when the argument is omitted entirely."""

    def _build(principal):
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
            ),
            payload=OfferVersionCreate(
                offer_id=uuid4(),
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=principal,
        )

    with pytest.raises(TypeError):
        _build(None)
    with pytest.raises(TypeError):
        _build("system_user:not-a-real-principal")


def _admit_evidence_command(offer, *, key, version_number=1, name=None):
    command_id = uuid4()
    return AdmitOfferVersionCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="system:test",
            scope=ADMISSION_SCOPE,
            reason="test admission",
            idempotency_key=key,
        ),
        payload=OfferVersionCreate(
            offer_id=offer.id,
            version_number=version_number,
            name=name or f"Fiber 100 v{version_number}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=AccessRequirement.unclassified,
        ),
        principal=SystemAdmission(reason="test admission"),
    )


def _stored_reservation(db_session, key):
    row = db_session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope
            == offer_access_requirement._ADMISSION_IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    )
    db_session.commit()
    return row


def test_admit_replay_binds_evidence_through_ref_id_and_leaves_account_id_unused(
    db_session,
):
    """Break condition: this proves the repair itself, not just its outward
    behaviour — if ``_admit`` reverted to writing
    ``IdempotencyKey(account_id=version.id, ref_id=fingerprint)`` (the
    original defect), the ``account_id is None`` assertion below would fail
    even though the replay would still — coincidentally — return the right
    row today. This is the accept-direction control for the whole group:
    without it, a version that refuses every replay (e.g. always raising
    ``idempotency_conflict``) would still pass the malformed/conflict/missing
    tests below."""

    offer = _make_offer(db_session)
    key = f"admit-evidence-{uuid4()}"

    first = admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()
    assert first.replayed is False

    stored = _stored_reservation(db_session, key)
    assert stored is not None
    assert stored.account_id is None
    assert stored.ref_id == f"{first.offer_version.id}|" + stored.ref_id.split("|")[1]

    second = admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()
    assert second.replayed is True
    assert second.offer_version.id == first.offer_version.id


@pytest.mark.parametrize(
    "malformed_ref_id",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("not-an-evidence-string", id="no-separator"),
        pytest.param(
            "550E8400-E29B-41D4-A716-446655440000|" + "a" * 64,
            id="uppercase-uuid",
        ),
        pytest.param("not-a-uuid|" + "a" * 64, id="bad-uuid"),
        pytest.param(
            "550e8400-e29b-41d4-a716-446655440000|" + "a" * 63,
            id="short-digest",
        ),
    ],
)
def test_admit_refuses_a_replay_with_malformed_result_evidence(
    db_session, malformed_ref_id
):
    """Break condition: removing the ``try/except ValueError`` wrapped
    around ``_decode_admission_result_evidence(reservation.ref_id)`` in
    ``_admit`` — a malformed ``ref_id`` would then raise a raw, uncaught
    ``ValueError`` out of ``_admit`` instead of the typed
    ``idempotency_conflict`` this test requires via
    ``pytest.raises(OfferAccessRequirementError)``."""

    offer = _make_offer(db_session)
    key = f"admit-malformed-{uuid4()}"

    admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()

    stored = _stored_reservation(db_session, key)
    stored.ref_id = malformed_ref_id
    db_session.commit()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")
    assert "malformed" in excinfo.value.message.lower()


def test_admit_refuses_a_replay_with_a_different_fingerprint_for_stored_evidence(
    db_session,
):
    """Break condition: removing the
    ``evidence_fingerprint != fingerprint`` comparison (or replacing it with
    a check against ``reservation.account_id``/some other unrelated field)
    would let a second, DIFFERENT admission payload silently replay the
    first admission's result instead of refusing — this is the refusal
    control that ``test_admit_replay_with_matching_key_and_payload_returns_
    the_original_row``'s accept-direction case guards against."""

    offer = _make_offer(db_session)
    key = f"admit-conflict-{uuid4()}"

    first = admit_offer_version(
        db_session, _admit_evidence_command(offer, key=key, name="Fiber 100 v1")
    )
    db_session.rollback()
    assert first.replayed is False

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_evidence_command(
                offer, key=key, version_number=2, name="Fiber 100 v2 (different)"
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")


def test_admit_refuses_a_replay_whose_evidenced_offer_version_row_is_gone(
    db_session,
):
    """Break condition: removing the ``if replayed_version is None: raise
    ...`` check immediately after ``db.get(OfferVersion,
    evidence_version_id)`` — without it, ``_admit`` would return
    ``AdmitOfferVersionResult(offer_version=None, replayed=True)`` instead of
    a typed, non-retryable refusal, handing the caller ``None`` where a real
    row is expected."""

    offer = _make_offer(db_session)
    key = f"admit-missing-{uuid4()}"

    first = admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()

    stale_version = db_session.scalar(
        select(OfferVersion).where(OfferVersion.id == first.offer_version.id)
    )
    assert stale_version is not None
    db_session.delete(stale_version)
    db_session.commit()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(db_session, _admit_evidence_command(offer, key=key))
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")
    assert excinfo.value.retryable is False
