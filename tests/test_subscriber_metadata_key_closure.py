"""An undeclared `subscribers.metadata` key is REFUSED, not ignored.

The distinction is the whole point of these tests. Until 2026-08-22 an
unrendered admin form field named `metadata` accepted arbitrary JSON and
`web_customer_actions` wrote it to the column wholesale, so any caller could
invent any key on any subscriber. Closing that could have been done three ways,
and two of them are wrong:

- **Ignore** the unknown key. The caller believes the write succeeded, the
  value is gone, and the failure surfaces later as absent data with nothing
  attached to the moment it was lost.
- **Sanitise** it — strip, rename or coerce. Same silence, plus a value that
  differs from what was sent without anyone being told.
- **Refuse** it, naming the key. This is what the code does, and what these
  tests pin.

The tests therefore assert an exception, and separately assert that the row was
NOT modified. A guard that raises after writing is not a guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.schemas.subscriber import (
    SubscriberNotificationPreferencesUpdate,
    SubscriberUpdate,
)
from app.services import subscriber as subscriber_service
from app.services.subscriber_metadata_keys import (
    DECLARED_METADATA_KEYS,
    UndeclaredMetadataKeyError,
    reject_undeclared_keys,
    undeclared_keys,
)

#: Anchored to the repository, not the working directory: these two checks read
#: source files, and a cwd-relative path makes them pass vacuously from the
#: wrong directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------------


def test_a_declared_key_is_accepted() -> None:
    reject_undeclared_keys({"nin_verified": True})


def test_an_undeclared_key_raises_and_names_itself() -> None:
    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        reject_undeclared_keys({"invented_by_a_caller": "anything"})
    assert "invented_by_a_caller" in str(caught.value), (
        "the refusal must name the key; an unnamed refusal sends the caller "
        "looking through a whole payload"
    )
    assert caught.value.keys == frozenset({"invented_by_a_caller"})


def test_every_undeclared_key_is_reported_not_just_the_first() -> None:
    """A caller fixing one key at a time learns of the next one on the retry."""

    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        reject_undeclared_keys({"first_invented": 1, "second_invented": 2})
    assert caught.value.keys == frozenset({"first_invented", "second_invented"})


def test_a_declared_key_beside_an_undeclared_one_does_not_rescue_the_write() -> None:
    """Partial validity is not validity. The whole write is refused."""

    with pytest.raises(UndeclaredMetadataKeyError):
        reject_undeclared_keys({"nin_verified": True, "invented": 1})


def test_the_registry_names_an_owner_for_every_key() -> None:
    ownerless = sorted(
        key for key, owner in DECLARED_METADATA_KEYS.items() if not owner.strip()
    )
    assert not ownerless, (
        "these keys are declared with no owner, which is the state the registry "
        "exists to end:\n  " + "\n  ".join(ownerless)
    )


def test_a_non_dict_value_is_not_treated_as_declared() -> None:
    """`undeclared_keys` must not silently pass a malformed payload."""

    assert undeclared_keys(None) == frozenset()
    assert undeclared_keys("not a dict") == frozenset()


# --------------------------------------------------------------------------
# the owner refuses, and refuses BEFORE writing
# --------------------------------------------------------------------------


def test_the_owner_refuses_an_undeclared_key_on_update(db_session, subscriber):
    before = dict(subscriber.metadata_ or {})

    with pytest.raises(Exception) as caught:
        subscriber_service.Subscribers.update(
            db_session,
            str(subscriber.id),
            SubscriberUpdate(metadata_={"invented_by_a_caller": "anything"}),
        )
    assert "invented_by_a_caller" in str(caught.value)

    db_session.rollback()
    refreshed = db_session.get(type(subscriber), subscriber.id)
    assert dict(refreshed.metadata_ or {}) == before, (
        "the row changed despite the refusal. A guard that raises after writing "
        "is not a guard — the caller sees an error and the data is modified."
    )


def test_the_owner_accepts_a_declared_key_on_update(db_session, subscriber):
    """The refusal must not be indiscriminate, or it proves nothing."""

    subscriber_service.Subscribers.update(
        db_session,
        str(subscriber.id),
        SubscriberUpdate(metadata_={"nin_verified": True}),
    )
    refreshed = db_session.get(type(subscriber), subscriber.id)
    assert (refreshed.metadata_ or {}).get("nin_verified") is True


# --------------------------------------------------------------------------
# the wildcard surface itself is gone
# --------------------------------------------------------------------------


def test_the_admin_customer_forms_accept_no_metadata_field() -> None:
    """The field was never rendered by a template — an unused write surface.

    Removing the plumbing matters as much as the guard: a refusal at the owner
    still leaves an endpoint whose contract advertises arbitrary JSON, and the
    next person to need "somewhere to put something" would find it.
    """

    source = (REPOSITORY_ROOT / "app/web/admin/customers.py").read_text(
        encoding="utf-8"
    )
    assert "metadata: str | None = Form(None)" not in source, (
        "the admin customer form still declares a free-JSON `metadata` field"
    )
    assert "metadata_json" not in source, (
        "the admin customer route still plumbs `metadata_json` to the service"
    )


def test_no_service_still_forwards_a_free_json_metadata_payload() -> None:
    for module in (
        "app/services/web_customer_actions.py",
        "app/services/web_subscriber_actions.py",
    ):
        source = (REPOSITORY_ROOT / module).read_text(encoding="utf-8")
        assert "metadata_json" not in source, (
            f"{module} still forwards a free-JSON metadata payload; the second "
            "caller of this shape was found only after the first was removed"
        )


# Typed preference patches use the same guarded update path as profile fields.


def _notification_preferences() -> SubscriberNotificationPreferencesUpdate:
    return SubscriberNotificationPreferencesUpdate(
        billing_notifications=False,
        sms_updates=True,
        push_notifications=False,
        service_notifications=True,
        account_notifications=False,
        usage_notifications=True,
        general_notifications=False,
    )


def test_rejected_billing_update_does_not_dirty_notification_preferences(
    db_session: Session, subscriber: Subscriber
) -> None:
    before = {"billing_notifications": True, "sms_updates": False}
    subscriber.metadata_ = dict(before)
    db_session.commit()
    payload = SubscriberUpdate(
        notification_preferences=_notification_preferences(),
        billing_enabled=not subscriber.billing_enabled,
    )

    with pytest.raises(HTTPException) as caught:
        subscriber_service.Subscribers.update(
            db_session, subscriber_id=str(subscriber.id), payload=payload
        )

    assert caught.value.status_code == 409
    # A rollback would mask an early ORM mutation. Check before rolling back
    # and prove a later flush cannot persist a rejected preference change.
    assert subscriber.metadata_ == before
    assert not db_session.is_modified(subscriber, include_collections=True)
    db_session.flush()
    db_session.refresh(subscriber)
    assert subscriber.metadata_ == before


@pytest.mark.parametrize(
    ("field", "value"), [("status", "blocked"), ("is_active", False)]
)
def test_lifecycle_fields_are_rejected_before_notification_updates(
    db_session: Session, subscriber: Subscriber, field: str, value: str | bool
) -> None:
    before = {"billing_notifications": True, "sms_updates": False}
    subscriber.metadata_ = dict(before)
    db_session.commit()

    # SubscriberUpdate deliberately excludes lifecycle fields. Exercise the
    # production schema rather than constructing an impossible service input.
    with pytest.raises(ValidationError) as caught:
        SubscriberUpdate.model_validate(
            {
                field: value,
                "notification_preferences": _notification_preferences().model_dump(),
            }
        )

    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        ((field,), "extra_forbidden")
    ]
    assert subscriber.metadata_ == before
    assert not db_session.is_modified(subscriber, include_collections=True)
    db_session.flush()
    db_session.refresh(subscriber)
    assert subscriber.metadata_ == before


@pytest.mark.parametrize(
    "metadata", [None, {}, {"nin_verified": True, "sms_updates": False}]
)
def test_explicit_metadata_and_preferences_are_applied_together(
    db_session: Session, subscriber: Subscriber, metadata: dict[str, bool] | None
) -> None:
    subscriber.metadata_ = {"portal_read_notification_keys": ["old-notice"]}
    db_session.commit()
    preferences = _notification_preferences()
    updated = subscriber_service.Subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(
            metadata_=metadata, notification_preferences=preferences
        ),
    )
    assert updated.metadata_ == {**(metadata or {}), **preferences.model_dump()}


def test_preference_patch_cannot_bypass_undeclared_metadata_guard(
    db_session: Session, subscriber: Subscriber
) -> None:
    before = dict(subscriber.metadata_ or {})
    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        subscriber_service.Subscribers.update(
            db_session,
            subscriber_id=str(subscriber.id),
            payload=SubscriberUpdate(
                metadata_={"invented_by_a_caller": True},
                notification_preferences=_notification_preferences(),
            ),
        )
    assert caught.value.keys == frozenset({"invented_by_a_caller"})
    assert dict(subscriber.metadata_ or {}) == before
    assert not db_session.is_modified(subscriber, include_collections=True)


def test_preference_patch_keeps_a_closed_field_set() -> None:
    with pytest.raises(ValidationError):
        SubscriberNotificationPreferencesUpdate.model_validate(
            {
                **_notification_preferences().model_dump(),
                "invented_by_a_caller": True,
            }
        )


@pytest.mark.parametrize("explicit_null", [False, True])
def test_absent_or_null_preference_patch_preserves_existing_metadata(
    db_session: Session, subscriber: Subscriber, explicit_null: bool
) -> None:
    before = {
        "frozen_import_provenance": {"source": "historical"},
        "sms_updates": False,
    }
    subscriber.metadata_ = dict(before)
    db_session.commit()
    payload = (
        SubscriberUpdate(first_name="Updated", notification_preferences=None)
        if explicit_null
        else SubscriberUpdate(first_name="Updated")
    )
    updated = subscriber_service.Subscribers.update(
        db_session, subscriber_id=str(subscriber.id), payload=payload
    )
    assert updated.first_name == "Updated"
    assert updated.metadata_ == before
