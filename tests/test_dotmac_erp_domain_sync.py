from uuid import uuid4

from app.models.erp_domain_sync import ErpDomainSyncCursor
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


def test_v2_outcome_accepts_the_real_erp_error_field() -> None:
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
                    "crm_id": str(source_id),
                    "error": "project source mapping not found",
                }
            ],
        }
    )

    assert outcome.errors[0].source_id == source_id


def _seed(db):
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
    assert result.errors == ()
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
                    source_id=uuid4(),
                    error="invalid",
                ),
            ),
        )
    )

    result = _run(db_session, client)

    assert result.errors
    assert db_session.query(ErpDomainSyncCursor).count() == 0
