from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.models.nextcloud_talk import NextcloudTalkNotificationRoom
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import nextcloud_talk_staff
from app.services.integrations import installations, nextcloud_talk_capability
from app.services.integrations.connectors import nextcloud_talk
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)
from app.services.nextcloud_talk_staff import (
    StaffTalkEventType,
    StageStaffTalkNotification,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def _envelope(*, action: str, params: dict[str, object]) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test:nextcloud-talk:1",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=nextcloud_talk.NEXTCLOUD_TALK_CAPABILITY,
        connector_key="nextcloud.talk",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.event,
        idempotency_key=f"talk-test:{uuid4()}",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={"action": action, "params": params},
    )


def _install_talk(db_session):
    installation = installations.create_draft(
        db_session,
        connector_key="nextcloud.talk",
        name=f"Talk {uuid4().hex}",
        environment="test",
        actor="test-operator",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "url": "https://messenger.example.test",
            "notifier_username": "selfcare-notifications",
            "timeout_seconds": 30,
        },
        secret_refs={"app_password": "env://NEXTCLOUD_TEST_APP_PASSWORD"},
        actor="test-operator",
    )
    binding = installations.bind_capability(
        db_session,
        installation_id=installation.id,
        capability_id=nextcloud_talk.NEXTCLOUD_TALK_CAPABILITY,
        scope={"audience": "staff"},
        policy={
            "default": True,
            "approved_egress_hosts": ["messenger.example.test"],
        },
        actor="test-operator",
    )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
        actor="test-operator",
    )
    return installation, binding


def _command(user_id, *, source_event_id=None):
    source_id = source_event_id or uuid4()
    return StageStaffTalkNotification(
        system_user_id=user_id,
        source_event_id=source_id,
        event_type=StaffTalkEventType.ticket_assignment,
        subject="Ticket assigned: TKT-1",
        body="Ticket TKT-1 assignment updated.",
        target_url="/admin/support/tickets/00000000-0000-0000-0000-000000000001",
        source_entity_type="support_ticket",
        source_entity_id=uuid4(),
    )


def test_runtime_rejects_local_and_non_https_base_urls() -> None:
    with pytest.raises(ValueError, match="https_required"):
        nextcloud_talk.normalize_base_url("http://messenger.example.test")
    with pytest.raises(ValueError, match="local_host_forbidden"):
        nextcloud_talk.normalize_base_url("https://localhost")
    with pytest.raises(ValueError, match="unsafe_address"):
        nextcloud_talk.normalize_base_url("https://127.0.0.1")


def test_runtime_uses_v4_room_and_v1_chat_endpoints(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if "/api/v4/room" in url:
                data = {"token": "room-token"}
                status_code = 201
            else:
                data = {"id": 42, "referenceId": "b" * 64}
                status_code = 100
            return httpx.Response(
                200,
                json={
                    "ocs": {
                        "meta": {"statuscode": status_code},
                        "data": data,
                    }
                },
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(nextcloud_talk, "_validate_public_host", lambda *_: None)
    monkeypatch.setattr(nextcloud_talk.httpx, "Client", FakeClient)
    runner = nextcloud_talk.NextcloudTalkRuntimeRunner()
    config = {
        "url": "https://messenger.example.test",
        "notifier_username": "notifier",
        "timeout_seconds": 30,
    }
    secret = {"app_password": "secret"}

    room = runner.execute(
        _envelope(
            action="create_direct_room",
            params={"invite": "Confidence Okaka"},
        ),
        config=config,
        secret_material=secret,
    )
    message = runner.execute(
        _envelope(
            action="post_message",
            params={
                "room_token": "room-token",
                "message": "Assigned",
                "reference_id": "b" * 64,
            },
        ),
        config=config,
        secret_material=secret,
    )

    assert room.status is OperationStatus.succeeded
    assert room.output["room_token"] == "room-token"
    assert message.status is OperationStatus.succeeded
    assert calls[0]["url"].endswith("/ocs/v2.php/apps/spreed/api/v4/room")
    assert calls[0]["json"] == {
        "roomType": 1,
        "invite": "Confidence Okaka",
    }
    assert calls[1]["url"].endswith("/ocs/v2.php/apps/spreed/api/v1/chat/room-token")
    assert calls[1]["data"] == {
        "message": "Assigned",
        "referenceId": "b" * 64,
    }


def test_feature_disabled_stages_nothing(db_session, monkeypatch) -> None:
    user, _person = add_bound_staff_user(db_session, email="off@example.test")
    monkeypatch.setattr(nextcloud_talk_staff, "resolve_value", lambda *_: False)

    result = nextcloud_talk_staff.stage_staff_talk_notification(
        db_session, _command(user.id)
    )

    assert result is None
    assert (
        db_session.query(Notification)
        .filter(Notification.channel == NotificationChannel.nextcloud_talk)
        .count()
        == 0
    )


def test_staging_is_idempotent_and_uses_pinned_binding(db_session, monkeypatch) -> None:
    user, _person = add_bound_staff_user(db_session, email="mapped@example.test")
    _installation, binding = _install_talk(db_session)
    monkeypatch.setattr(nextcloud_talk_staff, "resolve_value", lambda *_: True)
    command = _command(user.id)

    first = nextcloud_talk_staff.stage_staff_talk_notification(db_session, command)
    second = nextcloud_talk_staff.stage_staff_talk_notification(db_session, command)
    db_session.flush()

    assert first is second
    assert first is not None
    assert first.integration_capability_binding_id == binding.id
    assert first.status is NotificationStatus.queued
    assert first.metadata_["target_url"].startswith("https://selfcare.dotmac.io/")
    assert (
        db_session.query(Notification)
        .filter(Notification.channel == NotificationChannel.nextcloud_talk)
        .count()
        == 1
    )


def test_worker_creates_room_once_and_reuses_it(db_session, monkeypatch) -> None:
    user, _person = add_bound_staff_user(db_session, email="worker@example.test")
    installation, _binding = _install_talk(db_session)
    nextcloud_talk_staff.set_staff_account_mapping(
        db_session,
        system_user_id=user.id,
        integration_installation_id=installation.id,
        nextcloud_user_id=nextcloud_talk_staff.NextcloudUserId.parse("worker.agent"),
        actor="test",
    )
    monkeypatch.setattr(nextcloud_talk_staff, "resolve_value", lambda *_: True)
    nextcloud_talk_staff.stage_staff_talk_notification(db_session, _command(user.id))
    nextcloud_talk_staff.stage_staff_talk_notification(db_session, _command(user.id))
    db_session.commit()
    calls = {"room": 0, "post": 0}

    def create_room(*args, **kwargs):
        calls["room"] += 1
        return nextcloud_talk_capability.TalkOperationOutcome(
            status=OperationStatus.succeeded,
            error_code=None,
            room_token="room-token",
        )

    def post(*args, **kwargs):
        calls["post"] += 1
        return nextcloud_talk_capability.TalkOperationOutcome(
            status=OperationStatus.succeeded,
            error_code=None,
            message_id=f"message-{calls['post']}",
        )

    monkeypatch.setattr(nextcloud_talk_capability, "create_direct_room", create_room)
    monkeypatch.setattr(nextcloud_talk_capability, "post_message", post)

    result = nextcloud_talk_staff.deliver_due_staff_talk_notifications(db_session)

    assert result.claimed == 2
    assert result.delivered == 2
    assert calls == {"room": 1, "post": 2}
    assert db_session.query(NextcloudTalkNotificationRoom).count() == 1
    statuses = {
        row.status
        for row in db_session.query(Notification)
        .filter(Notification.channel == NotificationChannel.nextcloud_talk)
        .all()
    }
    assert statuses == {NotificationStatus.delivered}


def test_missing_username_mapping_fails_visibly(db_session, monkeypatch) -> None:
    user, _person = add_bound_staff_user(db_session, email="missing@example.test")
    _install_talk(db_session)
    monkeypatch.setattr(nextcloud_talk_staff, "resolve_value", lambda *_: True)
    notification = nextcloud_talk_staff.stage_staff_talk_notification(
        db_session, _command(user.id)
    )
    db_session.commit()

    result = nextcloud_talk_staff.deliver_due_staff_talk_notifications(db_session)

    assert result.failed == 1
    assert notification is not None
    db_session.refresh(notification)
    assert notification.status is NotificationStatus.failed
    assert notification.last_error == "nextcloud_username_mapping_missing"
    assert notification.retry_count == nextcloud_talk_staff.MAX_RETRIES


def test_mapping_accepts_exact_nextcloud_user_id_with_internal_spaces(
    db_session,
) -> None:
    user, _person = add_bound_staff_user(
        db_session,
        email="confidence.okaka@example.test",
    )
    installation, _binding = _install_talk(db_session)
    nextcloud_user_id = nextcloud_talk_staff.NextcloudUserId.parse(
        "  Confidence Okaka  "
    )

    mapping = nextcloud_talk_staff.set_staff_account_mapping(
        db_session,
        system_user_id=user.id,
        integration_installation_id=installation.id,
        nextcloud_user_id=nextcloud_user_id,
        actor="test",
    )

    assert nextcloud_user_id.value == "Confidence Okaka"
    assert nextcloud_user_id.normalized == "confidence okaka"
    assert mapping.nextcloud_username == "Confidence Okaka"
    assert mapping.nextcloud_username_normalized == "confidence okaka"


@pytest.mark.parametrize(
    "value",
    (
        "Confidence\tOkaka",
        "Confidence\nOkaka",
        "Confidence\x7fOkaka",
        "Confidence\u00a0Okaka",
    ),
)
def test_nextcloud_user_id_rejects_non_space_whitespace_and_controls(
    value: str,
) -> None:
    with pytest.raises(
        nextcloud_talk_staff.NextcloudTalkStaffCommandError,
        match="unsupported whitespace or control characters",
    ) as exc_info:
        nextcloud_talk_staff.NextcloudUserId.parse(value)

    assert exc_info.value.code == (
        "communications.nextcloud_talk_staff.invalid_mapping"
    )


def test_connection_test_recreates_one_stale_room(db_session, monkeypatch) -> None:
    user, _person = add_bound_staff_user(db_session, email="test-action@example.test")
    installation, _binding = _install_talk(db_session)
    nextcloud_talk_staff.set_staff_account_mapping(
        db_session,
        system_user_id=user.id,
        integration_installation_id=installation.id,
        nextcloud_user_id=nextcloud_talk_staff.NextcloudUserId.parse("test.agent"),
        actor="test",
    )
    db_session.add(
        NextcloudTalkNotificationRoom(
            system_user_id=user.id,
            integration_installation_id=installation.id,
            invite_target="test.agent",
            room_token="stale-token",
        )
    )
    db_session.commit()
    calls = {"room": 0, "post": 0}

    def create_room(*args, **kwargs):
        calls["room"] += 1
        return nextcloud_talk_capability.TalkOperationOutcome(
            status=OperationStatus.succeeded,
            error_code=None,
            room_token="fresh-token",
        )

    def post(*args, **kwargs):
        calls["post"] += 1
        if calls["post"] == 1:
            return nextcloud_talk_capability.TalkOperationOutcome(
                status=OperationStatus.rejected,
                error_code="provider_resource_not_found",
            )
        return nextcloud_talk_capability.TalkOperationOutcome(
            status=OperationStatus.succeeded,
            error_code=None,
            message_id="test-message",
        )

    monkeypatch.setattr(nextcloud_talk_capability, "create_direct_room", create_room)
    monkeypatch.setattr(nextcloud_talk_capability, "post_message", post)

    result = nextcloud_talk_staff.test_staff_talk_connection(
        db_session,
        system_user_id=user.id,
        integration_installation_id=installation.id,
    )

    assert result.succeeded is True
    assert calls == {"room": 1, "post": 2}
    room = db_session.query(NextcloudTalkNotificationRoom).one()
    assert room.room_token == "fresh-token"
    assert room.invalidated_at is None
