from pathlib import Path

from app.models.catalog import DunningAction
from app.schemas.collections import DunningActionLogCreate, DunningCaseCreate
from app.services import collections as collections_service
from app.services import web_billing_dunning
from app.web.admin.billing_dunning import router


def test_dunning_detail_route_is_registered():
    routes = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", []))))
        for route in router.routes
    }

    assert ("/billing/dunning/{case_id}", ("GET",)) in routes


def test_dunning_detail_data_includes_case_account_and_actions(
    db_session, subscriber_account
):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(account_id=subscriber_account.id),
    )
    action = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent",
        ),
    )

    state = web_billing_dunning.build_detail_data(
        db_session,
        case_id=str(case.id),
        can_write=True,
    )

    assert state["case"].id == case.id
    assert state["account"].id == subscriber_account.id
    assert state["actions"][0].id == action.id
    assert [form.key for form in state["case_actions"]] == [
        web_billing_dunning._ACTION_KEYS[
            web_billing_dunning.dunning_owner.DunningStaffAction.pause
        ],
        web_billing_dunning._ACTION_KEYS[
            web_billing_dunning.dunning_owner.DunningStaffAction.close
        ],
    ]


def test_dunning_templates_link_to_real_detail_route():
    listing = Path("templates/admin/billing/dunning.html").read_text()
    detail = Path("templates/admin/billing/dunning_detail.html").read_text()

    assert 'href="/admin/billing/dunning/{{ case.id }}"' in listing
    assert "Action History" in detail
    assert "{{ action_form(case_action) }}" in detail
    assert "onsubmit=" not in detail
    assert "dunning_note" in listing
    assert "dunning_warning" in listing


def test_dunning_safe_action_routes_replace_direct_mutation_routes():
    paths = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", []))))
        for route in router.routes
    }

    assert (
        "/billing/dunning/{case_id}/{action}/confirm",
        ("POST",),
    ) in paths
    assert (
        "/billing/dunning/bulk/{action}/preview",
        ("POST",),
    ) in paths
    assert (
        "/billing/dunning/bulk/{action}/confirm",
        ("POST",),
    ) in paths
    assert ("/billing/dunning/{case_id}/pause", ("POST",)) not in paths
