"""Tests for PPPoE credential auto-generation."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.sequence import DocumentSequence
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.schemas.catalog import SubscriptionCreate, SubscriptionUpdate
from app.services import catalog as catalog_service
from app.services.pppoe_credentials import (
    SEQUENCE_KEY,
    _generate_random_password,
    auto_generate_pppoe_credential,
)


def _set_subscriber_number(db, subscriber, number: str) -> None:
    subscriber.subscriber_number = number
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)


def _seed_pppoe_settings(
    db,
    *,
    prefix: str = "1050",
    padding: int = 5,
    start: int = 1,
    password_length: int = 12,
) -> None:
    """Insert PPPoE domain settings for tests."""
    specs = [
        ("pppoe_username_prefix", SettingValueType.string, prefix, None),
        ("pppoe_username_padding", SettingValueType.integer, str(padding), None),
        ("pppoe_username_start", SettingValueType.integer, str(start), None),
        (
            "pppoe_default_password_length",
            SettingValueType.integer,
            str(password_length),
            None,
        ),
    ]
    for key, vtype, text, json_val in specs:
        db.add(
            DomainSetting(
                domain=SettingDomain.radius,
                key=key,
                value_type=vtype,
                value_text=text,
                value_json=json_val,
                is_active=True,
            )
        )
    db.commit()


class TestAutoGeneratePppoeCredential:
    """Tests for auto_generate_pppoe_credential."""

    def test_generates_credential(self, db_session, subscriber):
        """Creates AccessCredential with correct username."""
        _seed_pppoe_settings(db_session, start=25915)
        _set_subscriber_number(db_session, subscriber, "SUB-025915")
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        assert result.username == "10025915"
        assert str(result.subscriber_id) == str(subscriber.id)
        assert result.is_active is True
        assert result.secret_hash is not None

    def test_multi_service_credentials_bind_to_exact_subscription(
        self, db_session, subscriber, catalog_offer
    ):
        from app.models.catalog import Subscription, SubscriptionStatus

        _seed_pppoe_settings(db_session, start=31000)
        _set_subscriber_number(db_session, subscriber, "SUB-031000")
        first = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
        )
        second = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
        )
        db_session.add_all([first, second])
        db_session.flush()

        first_credential = auto_generate_pppoe_credential(
            db_session,
            str(subscriber.id),
            subscription_id=str(first.id),
        )
        second_credential = auto_generate_pppoe_credential(
            db_session,
            str(subscriber.id),
            subscription_id=str(second.id),
        )

        assert first_credential is not None
        assert second_credential is not None
        assert first_credential.username != second_credential.username
        assert first_credential.subscription_id == first.id
        assert second_credential.subscription_id == second.id

    def test_skips_when_credential_exists(self, db_session, subscriber):
        """When subscriber already has active credential, skips."""
        _seed_pppoe_settings(db_session, start=1000)
        _set_subscriber_number(db_session, subscriber, "SUB-001000")

        # Create existing credential
        existing = AccessCredential(
            subscriber_id=subscriber.id,
            username="existing_user",
            secret_hash="plain:test123",
            is_active=True,
        )
        db_session.add(existing)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is None

    def test_generates_when_only_inactive_credential_exists(
        self, db_session, subscriber
    ):
        """When subscriber only has inactive credentials, generates new one."""
        _seed_pppoe_settings(db_session, start=5000)
        _set_subscriber_number(db_session, subscriber, "SUB-005000")

        inactive = AccessCredential(
            subscriber_id=subscriber.id,
            username="old_user",
            secret_hash="plain:old",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is not None
        assert result.username == "10005000"

    def test_usernames_derive_from_subscriber_numbers(self, db_session):
        """Two calls derive usernames from each subscriber's canonical id."""
        from app.models.subscriber import Subscriber

        _seed_pppoe_settings(db_session, start=100)

        sub1 = Subscriber(
            first_name="A",
            last_name="One",
            email=f"a-{uuid.uuid4().hex[:8]}@test.com",
            subscriber_number="SUB-000100",
        )
        sub2 = Subscriber(
            first_name="B",
            last_name="Two",
            email=f"b-{uuid.uuid4().hex[:8]}@test.com",
            subscriber_number="SUB-000101",
        )
        db_session.add_all([sub1, sub2])
        db_session.commit()

        cred1 = auto_generate_pppoe_credential(db_session, str(sub1.id))
        cred2 = auto_generate_pppoe_credential(db_session, str(sub2.id))

        assert cred1 is not None
        assert cred2 is not None
        assert cred1.username == "10000100"
        assert cred2.username == "10000101"

    def test_password_encrypted(self, db_session, subscriber):
        """Generated password is stored encrypted or with plain: prefix."""
        _seed_pppoe_settings(db_session, start=1)
        _set_subscriber_number(db_session, subscriber, "SUB-000001")
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        # Without CREDENTIAL_ENCRYPTION_KEY, falls back to plain: prefix
        assert result.secret_hash.startswith(("enc:", "plain:"))

    def test_radius_profile_id_assigned(self, db_session, subscriber):
        """Passes through the radius_profile_id when provided."""
        from app.models.catalog import RadiusProfile

        _seed_pppoe_settings(db_session, start=1)
        _set_subscriber_number(db_session, subscriber, "SUB-000001")

        profile = RadiusProfile(name="Test Profile", is_active=True)
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        result = auto_generate_pppoe_credential(
            db_session,
            str(subscriber.id),
            radius_profile_id=str(profile.id),
        )

        assert result is not None
        assert str(result.radius_profile_id) == str(profile.id)

    def test_falls_back_to_sequence_when_subscriber_number_not_canonical(
        self, db_session, subscriber
    ):
        """Legacy/manual (non-canonical) subscriber numbers aren't transformed
        into a 10<id> username, but PPPoE is mandatory — so generation falls back
        to a sequential username instead of returning None (which would block
        activation)."""
        _seed_pppoe_settings(db_session, prefix="SEQ", padding=5, start=1)
        _set_subscriber_number(db_session, subscriber, "100000127")

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        # Sequential fallback (seeded prefix), not the canonical 10<canonical_id>.
        assert result.username.startswith("SEQ")

    def test_conflicting_derived_username_fails_loudly(self, db_session, subscriber):
        """Derived username collisions are reported instead of falling back."""
        _seed_pppoe_settings(db_session, start=1)
        _set_subscriber_number(db_session, subscriber, "SUB-000127")
        other = Subscriber(
            first_name="Other",
            last_name="User",
            email=f"other-{uuid.uuid4().hex[:8]}@test.com",
            subscriber_number="SUB-999999",
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            AccessCredential(
                subscriber_id=other.id,
                username="10000127",
                secret_hash="plain:old",
                is_active=False,
            )
        )
        db_session.commit()

        with pytest.raises(ValueError, match="already used"):
            auto_generate_pppoe_credential(db_session, str(subscriber.id))

    def test_active_subscription_creation_fails_when_pppoe_conflicts(
        self,
        db_session,
        subscriber,
        catalog_offer,
    ):
        """Activation must not silently continue without a usable PPPoE credential."""
        _seed_pppoe_settings(db_session, start=1)
        _set_subscriber_number(db_session, subscriber, "SUB-000127")
        other = Subscriber(
            first_name="Other",
            last_name="User",
            email=f"other-{uuid.uuid4().hex[:8]}@test.com",
            subscriber_number="SUB-999999",
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            AccessCredential(
                subscriber_id=other.id,
                username="10000127",
                secret_hash="plain:old",
                is_active=False,
            )
        )
        db_session.commit()
        subscriber_id = subscriber.id

        with pytest.raises(ValueError, match="already used"):
            catalog_service.subscriptions.create(
                db_session,
                SubscriptionCreate(
                    account_id=subscriber_id,
                    offer_id=catalog_offer.id,
                    status=SubscriptionStatus.active,
                ),
            )

        assert (
            db_session.query(Subscription)
            .filter(
                Subscription.subscriber_id == subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
            .first()
            is None
        )
        assert (
            db_session.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == subscriber_id,
                AccessCredential.is_active.is_(True),
            )
            .first()
            is None
        )

    def test_pending_to_active_reverts_when_pppoe_conflicts(
        self,
        db_session,
        subscriber,
        catalog_offer,
    ):
        """Update-time activation reverts instead of leaving active without credentials."""
        _seed_pppoe_settings(db_session, start=1)
        _set_subscriber_number(db_session, subscriber, "SUB-000127")
        subscription = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        other = Subscriber(
            first_name="Other",
            last_name="User",
            email=f"other-{uuid.uuid4().hex[:8]}@test.com",
            subscriber_number="SUB-999999",
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            AccessCredential(
                subscriber_id=other.id,
                username="10000127",
                secret_hash="plain:old",
                is_active=False,
            )
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(subscription.id),
                SubscriptionUpdate(status=SubscriptionStatus.active),
            )
        assert exc_info.value.status_code == 409
        assert "lifecycle fields are read-only" in str(exc_info.value.detail)

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.pending
        assert (
            db_session.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == subscriber.id,
                AccessCredential.is_active.is_(True),
            )
            .first()
            is None
        )

    def test_active_create_login_matches_sequence_fallback_credential(
        self,
        db_session,
        subscriber,
        catalog_offer,
    ):
        """Non-canonical subscriber numbers fall back to a sequence username for
        the credential; subscription.login must match that real credential
        username (not be left empty, which would desync login from RADIUS)."""
        _seed_pppoe_settings(db_session, prefix="SEQ", padding=5, start=1)
        _set_subscriber_number(db_session, subscriber, "100000127")

        created = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
            ),
        )

        credential = (
            db_session.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == subscriber.id,
                AccessCredential.is_active.is_(True),
            )
            .one()
        )
        assert credential.username.startswith("SEQ")
        assert created.login == credential.username


class TestGenerateRandomPassword:
    """Tests for _generate_random_password."""

    def test_correct_length(self):
        pw = _generate_random_password(16)
        assert len(pw) == 16

    def test_alphanumeric_only(self):
        pw = _generate_random_password(100)
        assert pw.isalnum()

    def test_different_each_time(self):
        passwords = {_generate_random_password(12) for _ in range(10)}
        assert len(passwords) == 10


class TestDeprecatedPppoeSequence:
    """The old PPPoE sequence remains present but is no longer consumed."""

    def test_creates_sequence(self, db_session):
        """Creates DocumentSequence with specified start value."""
        seq = DocumentSequence(key=SEQUENCE_KEY, next_value=25915)
        db_session.add(seq)
        db_session.commit()

        existing = (
            db_session.query(DocumentSequence)
            .filter(DocumentSequence.key == SEQUENCE_KEY)
            .first()
        )
        assert existing is not None
        assert existing.next_value == 25915

    def test_sequence_not_used_by_auto_gen(self, db_session, subscriber):
        """DocumentSequence no longer feeds into auto-generation."""
        _seed_pppoe_settings(db_session)
        _set_subscriber_number(db_session, subscriber, "SUB-025915")

        # Pre-seed sequence at 25915
        seq = DocumentSequence(key=SEQUENCE_KEY, next_value=25915)
        db_session.add(seq)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is not None
        assert result.username == "10025915"

        # Verify deprecated sequence was not consumed
        db_session.refresh(seq)
        assert seq.next_value == 25915
