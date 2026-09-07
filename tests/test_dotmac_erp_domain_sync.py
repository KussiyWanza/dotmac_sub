from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.erp_domain_sync import ErpDomainSyncCursor, ErpOperationalSyncState
from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.dotmac_erp.domain_sync import sync_operational_domains
from app.services.dotmac_erp.operational_contracts import (
    ErpOperationalSyncCommand,
    ErpOperationalSyncError,
    ErpOperationalSyncOutcome,
    OperationalSyncExecution,
)
from app.services.owner_commands import CommandContext


class _ERP:
    def __init__(self, response: ErpOperationalSyncOutcome | None = None):
        self.response = response or ErpOperationalSyncOutcome(
            contract_version=2,
            projects_synced=1,
            project_tasks_synced=1,
            tickets_synced=1,
            work_orders_synced=1,
        )
        self.commands: list[ErpOperationalSyncCommand] = []

    def sync_operational_domains(
        self, command: ErpOperationalSyncCommand
    ) -> ErpOperationalSyncOutcome:
        self.commands.append(command)
        return self.response

    def close(self) -> None:
        return None


def test_v2_outcome_accepts_only_the_neutral_erp_error_field() -> None:
    source_id = uuid4()

    outcome = ErpOperationalSyncOutcome.model_validate(
        {
            "contract_version": 2,
            "projects_synced": 0,
            "project_tasks_synced": 0,
            "tickets_synced": 0,
            "work_orders_synced": 0,
            "errors": [
                {
                    "entity_type": "project_task",
                    "source_reference": str(source_id),
                    "error": "project source mapping not found",
                }
            ],
        }
    )

    assert outcome.errors[0].source_reference == source_id

    with pytest.raises(ValidationError):
        ErpOperationalSyncOutcome.model_validate(
            {
                "contract_version": 2,
                "projects_synced": 0,
                "project_tasks_synced": 0,
                "tickets_synced": 0,
                "work_orders_synced": 0,
                "errors": [
                    {
                        "entity_type": "project_task",
                        "crm_id": str(source_id),
                        "error": "legacy alias must be refused",
                    }
                ],
            }
        )


def _seed(db):
    if db.get(ErpOperationalSyncState, 1) is None:
        db.add(ErpOperationalSyncState(id=1))
    subscriber = Subscriber(
        first_name="ERP",
        last_name="Context",
        email=f"erp-context-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    project = Project(name="Native fiber project", subscriber_id=subscriber.id)
    ticket = Ticket(
        subscriber_id=subscriber.id,
        number=f"T-{uuid4().hex[:8]}",
        title="Native support ticket",
    )
    db.add_all([project, ticket])
    db.flush()
    db.add(
        ProjectTask(
            project_id=project.id,
            ticket_id=ticket.id,
            title="Survey and scope",
            status="todo",
        )
    )
    work_order = WorkOrder(
        crm_work_order_id=str(uuid4()),
        subscriber_id=subscriber.id,
        title="Native field work",
        crm_project_id=str(project.id),
        crm_ticket_id=str(ticket.id),
    )
    db.add(work_order)
    db.commit()


def _run(db_session, client):
    return sync_operational_domains(
        db_session,
        command=OperationalSyncExecution(),
        context=CommandContext.system(
            actor="test-suite",
            scope="erp-operational-context",
            reason="verify operational sync",
        ),
        client=client,
    )


def test_domain_sync_pushes_sub_ids_and_advances_cursors(db_session):
    _seed(db_session)
    client = _ERP()

    result = _run(db_session, client)

    assert result.projects == 1
    assert result.project_tasks == 1
    assert result.tickets == 1
    assert result.work_orders == 1
    assert result.status == "success"
    assert result.errors == ()
    assert result.diagnostic is None
    command = client.commands[0]
    assert command.projects[0].source_id
    assert command.project_tasks[0].project_source_id == command.projects[0].source_id
    assert command.tickets[0].source_id
    assert command.work_orders[0].source_id
    assert command.projects[0].metadata is not None
    assert command.projects[0].metadata["source_system"] == "dotmac_sub"
    assert db_session.query(ErpDomainSyncCursor).count() == 4
    db_session.rollback()

    # No changes after the keyset watermark: next sweep is a no-op.
    assert _run(db_session, client).projects == 0
    assert len(client.commands) == 1


def test_domain_sync_does_not_advance_on_partial_erp_error(db_session):
    _seed(db_session)
    client = _ERP(
        ErpOperationalSyncOutcome(
            contract_version=2,
            projects_synced=0,
            project_tasks_synced=0,
            tickets_synced=0,
            work_orders_synced=0,
            errors=(
                ErpOperationalSyncError(
                    entity_type="project",
                    source_reference=uuid4(),
                    error="invalid",
                ),
            ),
        )
    )

    result = _run(db_session, client)

    assert result.status == "blocked"
    assert result.diagnostic.code == "item_rejected"
    assert db_session.query(ErpDomainSyncCursor).count() == 0


class _FailingERP(_ERP):
    def __init__(self, *, transient: bool = False):
        super().__init__()
        self.transient = transient

    def sync_operational_domains(
        self, command: ErpOperationalSyncCommand
    ) -> ErpOperationalSyncOutcome:
        from app.services.dotmac_erp.client import (
            DotMacERPError,
            DotMacERPTransientError,
        )

        self.commands.append(command)
        error_type = DotMacERPTransientError if self.transient else DotMacERPError
        raise error_type(
            "safe test failure", status_code=503 if self.transient else 403
        )


def test_permanent_failure_is_durable_and_not_retried_until_due(db_session):
    from datetime import UTC, datetime, timedelta

    _seed(db_session)
    client = _FailingERP()
    first = _run(db_session, client)
    assert first.status == "blocked"
    assert first.diagnostic.http_status == 403
    assert first.diagnostic.operation_id
    assert first.diagnostic.correlation_id
    assert first.next_attempt_at > datetime.now(UTC) + timedelta(hours=5)
    assert db_session.query(ErpDomainSyncCursor).count() == 0
    db_session.rollback()
    second = _run(db_session, client)
    assert second.skipped == "retry_not_due"
    assert len(client.commands) == 1
    assert second.diagnostic == first.diagnostic
    db_session.rollback()
    state = db_session.get(ErpOperationalSyncState, 1)
    state.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    recovered = _run(db_session, _ERP())
    assert recovered.status == "success"
    assert recovered.projects == 1
    assert db_session.get(ErpOperationalSyncState, 1).diagnostic is None


def test_transient_failure_has_bounded_backoff_and_preserves_watermarks(db_session):
    from datetime import UTC, datetime, timedelta

    _seed(db_session)
    client = _FailingERP(transient=True)
    for _ in range(7):
        outcome = _run(db_session, client)
        assert outcome.status == "retryable"
        remaining = (outcome.next_attempt_at - datetime.now(UTC)).total_seconds()
        assert 290 <= remaining <= 3600
        assert db_session.query(ErpDomainSyncCursor).count() == 0
        state = db_session.get(ErpOperationalSyncState, 1)
        state.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()


def test_configuration_revision_correction_admits_earlier_probe(
    db_session, monkeypatch
):
    from app.services.integrations import installations
    from app.services.integrations.backoffice_contracts import (
        ERP_OPERATIONAL_SYNC_CAPABILITY,
    )
    from tests.integration_platform_helpers import enable_erp_capability

    _seed(db_session)
    binding = enable_erp_capability(db_session, ERP_OPERATIONAL_SYNC_CAPABILITY)
    installation_id = binding.installation_id
    db_session.commit()
    failed_client = _FailingERP()
    monkeypatch.setattr(
        "app.services.dotmac_erp.domain_sync.capability_client",
        lambda db: failed_client,
    )
    assert _run(db_session, None).status == "blocked"
    assert _run(db_session, None).skipped == "retry_not_due"
    assert len(failed_client.commands) == 1
    # The configuration owner creates an immutable revision, never an admission-row edit.
    installations.create_config_revision(
        db_session,
        installation_id=installation_id,
        config={
            "base_url": "https://erp-corrected.invalid",
            "timeout_seconds": 5,
            "max_retries": 1,
        },
        secret_refs={
            "service_credentials": "env://ERP_TEST_TOKEN",
            "webhook_signing_secret": "env://ERP_TEST_WEBHOOK_SECRET",
        },
    )
    from app.services.integrations.runtime import ValidationResult

    installations.validate_static(db_session, installation_id=installation_id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation_id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.commit()
    success_client = _ERP()
    monkeypatch.setattr(
        "app.services.dotmac_erp.domain_sync.capability_client",
        lambda db: success_client,
    )
    outcome = _run(db_session, None)
    assert outcome.status == "success"
    assert outcome.projects == 1


@pytest.mark.parametrize(
    "installation_state,binding_state",
    [("enabled", "disabled"), ("disabled", "enabled")],
)
def test_disabled_erp_configuration_blocks_without_transport(
    db_session, monkeypatch, installation_state, binding_state
):
    from unittest.mock import MagicMock

    from app.services.integrations.backoffice_contracts import (
        ERP_OPERATIONAL_SYNC_CAPABILITY,
    )
    from tests.integration_platform_helpers import enable_erp_capability

    _seed(db_session)
    binding = enable_erp_capability(db_session, ERP_OPERATIONAL_SYNC_CAPABILITY)
    binding.state = binding_state
    binding.installation.state = installation_state
    db_session.commit()
    transport = MagicMock()
    monkeypatch.setattr(
        "app.services.dotmac_erp.domain_sync.capability_client", transport
    )
    assert _run(db_session, None).status == "blocked"
    assert _run(db_session, None).skipped == "retry_not_due"
    transport.assert_not_called()
