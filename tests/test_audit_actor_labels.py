"""Audit events display real actor names, not raw ids.

Two gaps closed: name resolution was gated to `user` actors only (so api_key
and service actions showed a raw id), and a label was snapshotted only from an
interactive HTTP request (so background/job/webhook writes recorded a bare
uuid). Both are exercised here.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.audit import AuditActorType
from app.services import audit_helpers


class _Event(SimpleNamespace):
    pass


def _event(actor_id, actor_type, metadata=None):
    return _Event(
        actor_id=str(actor_id) if actor_id is not None else None,
        actor_type=actor_type,
        metadata_=metadata or {},
    )


# ---------------------------------------------------------------------------
# Display-time resolution (covers historical events with no snapshot)
# ---------------------------------------------------------------------------


def test_user_actor_resolves_to_a_person_name():
    person_id = uuid.uuid4()
    person = SimpleNamespace(
        id=person_id, display_name="Aisha Ibrahim", first_name="", last_name=""
    )

    name = audit_helpers.resolve_actor_name(
        _event(person_id, AuditActorType.user), {str(person_id): person}
    )

    assert name == "Aisha Ibrahim"


def test_api_key_actor_resolves_to_its_label():
    """Previously any api_key action rendered its raw uuid."""
    key_id = uuid.uuid4()
    key = SimpleNamespace(id=key_id, label="crm-service-integration")

    name = audit_helpers.resolve_actor_name(
        _event(key_id, AuditActorType.api_key), {str(key_id): key}
    )

    assert name == "crm-service-integration"


def test_service_actor_shows_a_human_readable_label():
    """Named services remain attributable without exposing a raw identifier."""
    name = audit_helpers.resolve_actor_name(
        _event("system:outage-classifier", AuditActorType.service), {}
    )

    assert name == "System: outage classifier"


def test_snapshotted_name_is_used_when_the_actor_is_gone():
    """A deleted user (actor_id is not a FK) still reads correctly from metadata."""
    gone = uuid.uuid4()

    name = audit_helpers.resolve_actor_name(
        _event(gone, AuditActorType.user, {"actor_name": "Former Staff"}), {}
    )

    assert name == "Former Staff"


def test_unresolvable_user_never_exposes_the_raw_id():
    orphan = uuid.uuid4()

    name = audit_helpers.resolve_actor_name(_event(orphan, AuditActorType.user), {})

    assert name == "Former or unknown user"


def test_system_actor_with_no_id_is_labelled_system():
    assert (
        audit_helpers.resolve_actor_name(_event(None, AuditActorType.system), {})
        == "System"
    )


def test_uuid_shaped_service_identity_is_not_exposed():
    actor_id = uuid.uuid4()

    assert (
        audit_helpers._readable_service_actor(f"integration:{actor_id}")
        == "Integration service"
    )


def test_named_service_identity_remains_readable():
    assert (
        audit_helpers._readable_service_actor("system:outage-classifier")
        == "System: outage classifier"
    )


# ---------------------------------------------------------------------------
# Write-time snapshot from the db (covers request-less writes)
# ---------------------------------------------------------------------------


class _FakeDb:
    """Answers db.get(Model, pk) from canned rows keyed by (model_name, pk)."""

    def __init__(self, rows: dict):
        self.rows = rows

    def get(self, model, pk):
        return self.rows.get((model.__name__, str(pk)))


def test_db_resolver_labels_an_api_key_write():
    key_id = uuid.uuid4()
    db = _FakeDb(
        {("ApiKey", str(key_id)): SimpleNamespace(id=key_id, label="erp-sync")}
    )

    label = audit_helpers._resolve_actor_label_from_db(
        db, str(key_id), AuditActorType.api_key
    )

    assert label == "erp-sync"


def test_db_resolver_labels_a_user_write():
    uid = uuid.uuid4()
    db = _FakeDb(
        {
            (
                "SystemUser",
                str(uid),
            ): SimpleNamespace(
                id=uid, display_name=None, first_name="Chibuzor", last_name="Nnamani"
            )
        }
    )

    label = audit_helpers._resolve_actor_label_from_db(
        db, str(uid), AuditActorType.user
    )

    assert label == "Chibuzor Nnamani"


def test_db_resolver_leaves_service_actors_to_display_as_is():
    """Service ids are readable already; no db lookup, no crash on a non-uuid id."""
    assert (
        audit_helpers._resolve_actor_label_from_db(
            _FakeDb({}), "system:outage-classifier", AuditActorType.service
        )
        is None
    )


def test_db_resolver_tolerates_a_missing_row():
    assert (
        audit_helpers._resolve_actor_label_from_db(
            _FakeDb({}), str(uuid.uuid4()), AuditActorType.api_key
        )
        is None
    )
