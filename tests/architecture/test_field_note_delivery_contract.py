from pathlib import Path

from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_field_note_writer_has_one_typed_owner_contract() -> None:
    service = service_relationship("operations.field_notes")

    assert service.module == "app.services.field.note_commands"
    assert service.owns == (
        "native field work-order note creation",
        "authorized staff field-note related-context projection",
    )
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "owner_managed"
    assert service.contract.events is not None
    assert service.contract.events.event_types == ("field_work_order_note.created",)
    assert [concern.role.value for concern in service.contract.concerns] == [
        "command_writer",
        "resolver",
    ]


def test_field_note_adapter_cannot_restore_legacy_writer() -> None:
    route = (ROOT / "app/api/field/notes.py").read_text(encoding="utf-8")
    legacy = (ROOT / "app/services/field/notes.py").read_text(encoding="utf-8")

    assert "create_field_work_order_note(" in route
    assert "field_notes.create(" not in route
    assert "def create(" not in legacy


def test_mobile_note_delivery_keeps_stable_retry_and_visible_state() -> None:
    sync = (ROOT / "field_mobile/lib/core/offline/sync_service.dart").read_text(
        encoding="utf-8"
    )
    screen = (ROOT / "field_mobile/lib/features/jobs/job_detail_screen.dart").read_text(
        encoding="utf-8"
    )

    assert "entry.kind == 'note'" in sync
    assert "offlineNotesForJob" in sync
    assert "Note queued for sync" in screen
    assert "Note could not sync" in screen


def test_staff_pages_compose_canonical_field_notes_without_copying_comments() -> None:
    owner = (ROOT / "app/services/field/note_commands.py").read_text(encoding="utf-8")
    dispatch = (ROOT / "app/services/web_dispatch_work_orders.py").read_text(
        encoding="utf-8"
    )
    projects = (ROOT / "app/services/web_projects.py").read_text(encoding="utf-8")
    tickets = (ROOT / "app/services/web_support_tickets.py").read_text(encoding="utf-8")
    access_adapter = (ROOT / "app/web/admin/field_note_access.py").read_text(
        encoding="utf-8"
    )
    templates = [
        ROOT / "templates/admin/dispatch/work_order_detail.html",
        ROOT / "templates/admin/projects/project_task_detail.html",
        ROOT / "templates/admin/support/tickets/detail.html",
    ]

    assert "list_staff_field_work_order_notes(" in owner
    assert "WorkOrder.project_task_id ==" in owner
    assert "WorkOrder.origin_ticket_id ==" in owner
    assert "list_staff_field_work_order_notes(" in dispatch
    assert "list_staff_field_work_order_notes(" in projects
    assert "list_staff_field_work_order_notes(" in tickets
    assert "grant_scopes_for_permission" in access_adapter
    assert "StaffFieldNoteAccess" in access_adapter
    assert all(
        "field_note_list" in path.read_text(encoding="utf-8") for path in templates
    )
    assert "ProjectTaskComment" not in owner
    assert "TicketComment" not in owner
