from uuid import uuid4

from app.models.erp_domain_sync import ErpDomainSyncCursor
from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.dotmac_erp.domain_sync import sync_operational_domains


class _ERP:
    def __init__(self, response=None):
        self.response = response or {
            "contract_version": 2,
            "projects_synced": 1,
            "project_tasks_synced": 1,
            "tickets_synced": 1,
            "work_orders_synced": 1,
            "errors": [],
        }
        self.payloads = []

    def sync_operational_domains(self, payload):
        self.payloads.append(payload)
        return self.response


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


def test_domain_sync_pushes_sub_ids_and_advances_cursors(db_session):
    _seed(db_session)
    client = _ERP()

    result = sync_operational_domains(db_session, client=client)

    assert result == {
        "projects": 1,
        "project_tasks": 1,
        "tickets": 1,
        "work_orders": 1,
        "errors": [],
    }
    payload = client.payloads[0]
    assert payload["projects"][0]["source_id"]
    assert (
        payload["project_tasks"][0]["project_source_id"]
        == payload["projects"][0]["source_id"]
    )
    assert payload["tickets"][0]["source_id"]
    assert payload["work_orders"][0]["source_id"]
    assert payload["projects"][0]["metadata"]["source_system"] == "dotmac_sub"
    assert db_session.query(ErpDomainSyncCursor).count() == 4

    # No changes after the keyset watermark: next sweep is a no-op.
    assert sync_operational_domains(db_session, client=client)["projects"] == 0
    assert len(client.payloads) == 1


def test_domain_sync_does_not_advance_on_partial_erp_error(db_session):
    _seed(db_session)
    client = _ERP(
        {
            "contract_version": 2,
            "projects_synced": 0,
            "project_tasks_synced": 0,
            "tickets_synced": 0,
            "work_orders_synced": 0,
            "errors": [{"entity_type": "project", "error": "invalid"}],
        }
    )

    result = sync_operational_domains(db_session, client=client)

    assert result["errors"]
    assert db_session.query(ErpDomainSyncCursor).count() == 0


def test_project_task_cursor_does_not_advance_against_old_erp_contract(db_session):
    _seed(db_session)
    client = _ERP(
        {
            "projects_synced": 1,
            "tickets_synced": 1,
            "work_orders_synced": 1,
            "errors": [],
        }
    )

    result = sync_operational_domains(db_session, client=client)

    assert result["errors"][0]["entity_type"] == "project_task"
    assert db_session.query(ErpDomainSyncCursor).count() == 0
