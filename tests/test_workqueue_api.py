from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import workqueue as workqueue_api
from app.schemas.workqueue import WorkqueueSnoozeCreate
from app.services.workqueue import WorkqueuePrincipal
from app.services.workqueue.commands import (
    WorkqueueActionError,
    WorkqueueActionOutcome,
    WorkqueueSnoozeSnapshot,
)
from app.services.workqueue.types import ActionKind, ItemKind


def _principal(system_user_id: UUID) -> WorkqueuePrincipal:
    return WorkqueuePrincipal(
        person_id=system_user_id,
        roles=frozenset({"admin"}),
        scopes=frozenset(),
        can_view=True,
        can_act=True,
    )


def test_snooze_api_delegates_to_owner_and_preserves_idempotency(monkeypatch):
    system_user_id = uuid4()
    item_id = uuid4()
    snooze_id = uuid4()
    created_at = datetime.now(UTC)
    captured = []
    principal = _principal(system_user_id)

    monkeypatch.setattr(workqueue_api, "_principal", lambda *_args: principal)

    def execute(_db, command):
        captured.append(command)
        return WorkqueueActionOutcome(
            item_kind=ItemKind.ticket,
            item_id=item_id,
            action=ActionKind.snooze,
            result="snoozed",
            replayed=False,
            service_team_id=None,
            assigned_system_user_id=None,
            previous_assigned_system_user_id=None,
            snooze=WorkqueueSnoozeSnapshot(
                snooze_id=snooze_id,
                system_user_id=system_user_id,
                item_kind=ItemKind.ticket,
                item_id=item_id,
                snooze_until=None,
                until_next_reply=False,
                created_at=created_at,
            ),
        )

    monkeypatch.setattr(workqueue_api, "execute_action", execute)

    response = workqueue_api.snooze_item(
        WorkqueueSnoozeCreate(
            item_kind=ItemKind.ticket.value,
            item_id=item_id,
        ),
        idempotency_key="api-request-1",
        auth={
            "principal_id": str(system_user_id),
            "principal_type": "system_user",
        },
        db=object(),
    )

    assert response.id == snooze_id
    assert response.user_id == system_user_id
    assert len(captured) == 1
    command = captured[0]
    assert command.action is ActionKind.snooze
    assert command.context.idempotency_key == "api-request-1"
    assert command.principal is principal


def test_clear_snooze_api_maps_owner_scope_error(monkeypatch):
    system_user_id = uuid4()
    principal = _principal(system_user_id)
    monkeypatch.setattr(workqueue_api, "_principal", lambda *_args: principal)

    def execute(_db, _command):
        raise WorkqueueActionError(
            code="operations.agent_workqueue.item_out_of_scope",
            message="Item is outside native team scope",
        )

    monkeypatch.setattr(workqueue_api, "execute_action", execute)

    with pytest.raises(HTTPException) as error:
        workqueue_api.clear_snooze(
            ItemKind.ticket.value,
            uuid4(),
            idempotency_key="api-request-2",
            auth={
                "principal_id": str(system_user_id),
                "principal_type": "system_user",
            },
            db=object(),
        )

    assert error.value.status_code == 403
    assert error.value.detail == "Item is outside native team scope"
