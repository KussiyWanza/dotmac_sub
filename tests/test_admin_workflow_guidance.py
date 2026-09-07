from app.services.admin_workflow_guidance import (
    WORKFLOW_GUIDANCE,
    guidance_categories,
    guidance_for_path,
    search_guidance,
)
from scripts.architecture.workflow_guidance_gate import validation_errors


def test_every_guide_has_plain_language_content_and_a_route() -> None:
    assert len(WORKFLOW_GUIDANCE) >= 20
    for guide in WORKFLOW_GUIDANCE:
        assert guide.id
        assert guide.title
        assert guide.purpose
        assert guide.steps
        assert guide.route_prefixes
        assert all(route.startswith("/admin") for route in guide.route_prefixes)


def test_change_plan_guide_is_searchable_and_contextual() -> None:
    guide = guidance_for_path("/admin/catalog/subscriptions/123")
    assert guide is not None
    assert guide.id == "change-plan"
    assert "change-plan" in {article.id for article in search_guidance(query="plan")}
    assert "Subscriptions" in guidance_categories()


def test_customer_detail_guidance_explains_service_extension_states() -> None:
    guide = next(item for item in WORKFLOW_GUIDANCE if item.id == "customer-detail")

    content = " ".join((*guide.steps, *guide.notes)).lower()
    for state in ("pending", "applied", "canceled", "reversed"):
        assert state in content
    assert "billing-date impact" in content


def test_project_guidance_explains_customer_typeahead_selection() -> None:
    guide = guidance_for_path("/admin/projects/new")

    assert guide is not None
    assert guide.id == "project-authoring"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "account id" in content
    assert "choose the matching result" in content
    assert "clear the customer field" in content
    assert "selected customer account" in content


def test_manager_ai_guidance_explains_question_and_answer_workflow() -> None:
    guide = guidance_for_path("/admin/inbox/manager-ai")

    assert guide is not None
    assert guide.id == "team-inbox"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "period review" in content
    assert "ask ai" in content
    assert "response under answer" in content
    assert "html-like text remains plain text" in content
    assert "verify ai advice" in content


def test_workflow_change_without_guidance_update_fails_gate() -> None:
    assert validation_errors(
        (__import__("pathlib").PurePosixPath("app/web/admin/reports.py"),)
    )
    assert not validation_errors(
        (
            __import__("pathlib").PurePosixPath("app/web/admin/reports.py"),
            __import__("pathlib").PurePosixPath(
                "app/services/admin_workflow_guidance.py"
            ),
        )
    )


def test_project_infrastructure_guide_is_linked_and_searchable() -> None:
    for path in ("/admin/projects", "/admin/projects/new", "/admin/projects/123/edit"):
        guide = guidance_for_path(path)
        assert guide is not None
        assert guide.id == "project-authoring"
    assert "project-authoring" in {
        guide.id for guide in search_guidance(query="cable rerun")
    }


def test_support_ticket_guidance_separates_editing_from_assignment() -> None:
    guide = guidance_for_path("/admin/support/tickets/123")

    assert guide is not None
    assert guide.id == "support-tickets"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "ordinary ticket editing" in content
    assert "ticket-update authority" in content
    assert "assignment details" in content


def test_support_csat_report_guidance_is_route_specific() -> None:
    guide = guidance_for_path("/admin/reports/support-csat")

    assert guide is not None
    assert guide.id == "support-csat-report"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "historical snapshots" in content
    assert "export csv" in content


def test_payment_guidance_explains_funded_prepaid_renewal() -> None:
    guide = guidance_for_path("/admin/billing/payments/123")

    assert guide is not None
    assert guide.id == "payments"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "creates and pays one invoice" in content
    assert "complete prepaid charge is unavailable" in content
    assert "billing date is not moved" in content
