from types import SimpleNamespace

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.system_user import SystemUser, SystemUserType
from app.services import audit_helpers, web_system_audit


def test_audit_page_resolves_system_user_actor_names(db_session):
    user = SystemUser(
        first_name="Admin",
        last_name="Operator",
        email="admin-operator@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        AuditEvent(
            actor_type=AuditActorType.user,
            actor_id=str(user.id),
            action="update",
            entity_type="support_ticket",
            entity_id="ticket-1",
            is_success=True,
        )
    )
    db_session.commit()

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(
            entity_type="support_ticket",
            page=1,
        ),
    )

    assert page["events"][0]["actor_name"] == "Admin Operator"
    assert page["total"] == 1


def test_recent_activity_feed_resolves_system_user_actor_names(db_session):
    user = SystemUser(
        first_name="Audit",
        last_name="Reviewer",
        email="audit-reviewer@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id=str(user.id),
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        is_success=True,
    )
    db_session.add(event)
    db_session.commit()

    feed = audit_helpers.build_recent_activity_feed(db_session, [event], limit=5)

    assert feed[0]["message"].startswith("Audit Reviewer ")


def test_recent_activity_feed_uses_actor_name_metadata_for_unresolved_user(
    db_session,
):
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id="missing-user-id",
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        is_success=True,
        metadata_={"actor_name": "Archived Admin"},
    )
    db_session.add(event)
    db_session.commit()

    feed = audit_helpers.build_recent_activity_feed(db_session, [event], limit=5)

    assert feed[0]["message"].startswith("Archived Admin ")


def test_log_audit_event_derives_system_user_actor_name_when_id_missing(db_session):
    user = SystemUser(
        first_name="NOC",
        last_name="Admin",
        email="noc-admin@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            user=user,
            auth={"principal_type": "system_user", "principal_id": str(user.id)},
        ),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )

    audit_helpers.log_audit_event(
        db=db_session,
        request=request,
        action="update",
        entity_type="invoice",
        entity_id="inv-1",
        actor_id="",
    )

    event = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == "inv-1",
            AuditEvent.action == "update",
        )
        .one()
    )
    assert event.actor_type == AuditActorType.user
    assert event.actor_id == str(user.id)
    assert event.metadata_["actor_name"] == "NOC Admin"
    assert event.metadata_["actor_email"] == "noc-admin@example.com"

    page = web_system_audit.get_audit_page_data(
        db_session,
        web_system_audit.build_audit_list_query(page=1),
    )

    assert page["events"][0]["actor_name"] == "NOC Admin"


# --- Audit list-projection contract (ui.audit_events_list_projection) ---


def test_audit_list_definition_declares_expected_capabilities():
    definition = web_system_audit.AUDIT_EVENTS_LIST_DEFINITION
    assert definition.filterable_keys == ("actor_id", "action", "entity_type")
    assert definition.sortable_keys == ("occurred_at",)
    assert definition.default_sort == "occurred_at"
    assert definition.default_sort_dir == "desc"
    assert definition.default_per_page == 50


def test_build_audit_list_query_normalizes_and_defaults():
    query = web_system_audit.build_audit_list_query(
        actor_id=" ", action="update", entity_type=None, page=2
    )
    # Blank filters dropped; supplied ones kept.
    assert query.filter_value("actor_id") is None
    assert query.filter_value("action") == "update"
    assert query.sort_by == "occurred_at"
    assert query.sort_dir == "desc"
    assert query.page == 2
    assert query.per_page == 50


def test_build_audit_list_query_rejects_out_of_contract_params():
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(sort_by="actor_id")
    with pytest.raises(ValueError):
        web_system_audit.build_audit_list_query(per_page=20)


def test_get_audit_page_data_filters_through_the_query(db_session):
    user = SystemUser(
        first_name="Filter",
        last_name="Target",
        email="filter-target@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    for action in ("create", "update"):
        db_session.add(
            AuditEvent(
                actor_type=AuditActorType.user,
                actor_id=str(user.id),
                action=action,
                entity_type="support_ticket",
                entity_id=f"ticket-{action}",
                is_success=True,
            )
        )
    db_session.commit()

    query = web_system_audit.build_audit_list_query(action="update")
    page = web_system_audit.get_audit_page_data(db_session, query)

    assert page["total"] == 1
    assert page["events"][0]["action"] == "update"
