from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field import attachments as attachments_module
from app.services.field.attachments import field_attachments
from app.services.field.jobs import field_jobs
from app.services.field.notes import field_notes


@dataclass
class _Stream:
    chunks: Iterator[bytes]
    content_type: str
    content_length: int


class _FakeUploads:
    def __init__(self):
        self.contents: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload(self, **kwargs):
        record = StoredFile(
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"attachments/{uuid4().hex}",
            file_size=len(kwargs["data"]),
            content_type=kwargs["content_type"],
            storage_provider="s3",
            uploaded_by=kwargs["uploaded_by"],
            owner_subscriber_id=kwargs["owner_subscriber_id"],
        )
        kwargs["db"].add(record)
        kwargs["db"].commit()
        kwargs["db"].refresh(record)
        self.contents[str(record.id)] = kwargs["data"]
        return record

    def stream_file(self, record):
        data = self.contents[str(record.id)]
        return _Stream(iter([data]), record.content_type, len(data))

    def soft_delete(self, *, db, file, hard_delete_object=True):
        file.is_deleted = True
        self.deleted.append(str(file.id))
        db.commit()
        return file


@pytest.fixture()
def fake_uploads(monkeypatch):
    fake = _FakeUploads()
    monkeypatch.setattr(attachments_module, "file_uploads", fake)
    return fake


def _user(db_session, name: str = "Attach") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(
    db_session, user: SystemUser, crm_person_id: str = "crm-attach-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Attach",
        last_name="Customer",
        email=f"attach-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-attach"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field job"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-attach-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_upload_attachment_list_content_delete_and_job_detail(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-attach-detail"
    )
    db_session.commit()

    attachment = field_attachments.create(
        db_session,
        _auth(user),
        kind="photo",
        file_name="drop.jpg",
        mime_type="image/jpeg",
        content=b"image-bytes",
        crm_work_order_id="wo-attach-detail",
        latitude=9.071,
        longitude=7.451,
    )

    assert attachment["file_name"] == "drop.jpg"
    assert attachment["work_order_id"] == "wo-attach-detail"
    assert attachment["download_path"].endswith("/content")
    stored = db_session.query(FieldAttachment).one()
    assert stored.work_order_mirror_id == work_order.id

    listed = field_attachments.list(
        db_session, _auth(user), crm_work_order_id="wo-attach-detail"
    )
    assert [item["id"] for item in listed] == [attachment["id"]]

    got, stream = field_attachments.get_content(
        db_session, _auth(user), str(attachment["id"])
    )
    assert got.id == attachment["id"]
    assert b"".join(stream.chunks) == b"image-bytes"

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-attach-detail")
    assert len(detail.attachments) == 1
    assert detail.attachments[0].file_name == "drop.jpg"

    field_attachments.delete(db_session, _auth(user), str(attachment["id"]))
    assert fake_uploads.deleted == [str(stored.stored_file_id)]
    assert (
        field_attachments.list(
            db_session, _auth(user), crm_work_order_id="wo-attach-detail"
        )
        == []
    )


def test_attachment_client_ref_dedupes_retry(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-attach-dedupe")
    client_ref = uuid4()
    db_session.commit()

    first = field_attachments.create(
        db_session,
        _auth(user),
        kind="document",
        file_name="proof.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.4",
        crm_work_order_id="wo-attach-dedupe",
        client_ref=client_ref,
    )
    second = field_attachments.create(
        db_session,
        _auth(user),
        kind="document",
        file_name="proof.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.4",
        crm_work_order_id="wo-attach-dedupe",
        client_ref=client_ref,
    )

    assert first["id"] == second["id"]
    assert db_session.query(FieldAttachment).count() == 1


def test_note_can_link_same_job_attachment(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-note-photo")
    db_session.commit()
    attachment = field_attachments.create(
        db_session,
        _auth(user),
        kind="photo",
        file_name="drop.jpg",
        mime_type="image/jpeg",
        content=b"image-bytes",
        crm_work_order_id="wo-note-photo",
    )

    note = field_notes.create(
        db_session,
        _auth(user),
        "wo-note-photo",
        body="See photo",
        attachment_ids=[str(attachment["id"])],
    )

    assert note["attachments"][0]["id"] == attachment["id"]
    stored = db_session.get(FieldAttachment, attachment["id"])
    assert stored.note_id == note["id"]


def test_attachment_hidden_job_404(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-attach-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-attach-hidden",
        assigned_to_crm_person_id="other-attach-tech",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_attachments.create(
            db_session,
            _auth(user),
            kind="photo",
            file_name="drop.jpg",
            mime_type="image/jpeg",
            content=b"image-bytes",
            crm_work_order_id="wo-attach-hidden",
        )

    assert exc.value.status_code == 404


def test_attachment_api(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-attach-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    upload = client.post(
        "/api/v1/field/attachments",
        data={"kind": "photo", "work_order_id": "wo-attach-api"},
        files={"file": ("drop.jpg", b"image-bytes", "image/jpeg")},
    )

    assert upload.status_code == 201
    assert upload.json()["work_order_id"] == "wo-attach-api"
    attachment_id = upload.json()["id"]
    assert (
        upload.json()["download_path"]
        == f"/api/v1/field/attachments/{attachment_id}/content"
    )

    content = client.get(f"/api/v1/field/attachments/{attachment_id}/content")
    assert content.status_code == 200
    assert content.content == b"image-bytes"

    deleted = client.delete(f"/api/v1/field/attachments/{attachment_id}")
    assert deleted.status_code == 204
