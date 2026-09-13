from pathlib import Path

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
        selectors = (*guide.route_prefixes, *guide.route_templates)
        assert selectors
        assert all(route.startswith("/admin") for route in selectors)
        assert all(
            route.startswith("/admin") for route in guide.excluded_route_prefixes
        )


def test_subscription_lifecycle_guide_includes_plan_changes() -> None:
    guide = guidance_for_path("/admin/catalog/subscriptions/123")
    assert guide is not None
    assert guide.id == "subscription-lifecycle"
    assert "subscription-lifecycle" in {
        article.id for article in search_guidance(query="plan")
    }
    assert "Subscriptions" in guidance_categories()


def test_getting_started_is_the_first_help_category() -> None:
    categories = guidance_categories()

    assert categories[:2] == ("Getting started", "Billing")


def test_specific_workflow_routes_override_or_reject_broad_sections() -> None:
    expected = {
        "/admin/dashboard": "admin-workspace",
        "/admin/customers": "find-customer",
        "/admin/customers/wizard": "create-customer",
        "/admin/customers/person/customer-id": "customer-detail",
        "/admin/catalog/subscriptions/new": "new-subscription",
        "/admin/catalog/subscriptions/subscription-id": "subscription-lifecycle",
        "/admin/catalog/subscriptions/subscription-id/access/move": "service-access",
        "/admin/network": "network-access",
        "/admin/dispatch/work-orders/work-order-id": "work-order-expenses",
        "/admin/projects/project-id/edit": "project-authoring",
        "/admin/projects/tasks/task-id": "project-task-subtasks",
        "/admin/projects/templates/template-id/tasks/editor": "project-template-plans",
        "/admin/sales/sales-order/order-id": "sales-orders",
        "/admin/billing": "billing-overview",
        "/admin/billing/payments/reconciliation": "payment-reconciliation",
        "/admin/support/tickets/ticket-id": "support-tickets",
        "/admin/inbox/manager-ai": "team-inbox",
        "/admin/network/olts": "olt-operational-health",
    }
    for path, guide_id in expected.items():
        guide = guidance_for_path(path)
        assert guide is not None
        assert guide.id == guide_id

    for unrelated_path in (
        "/admin/support/automation",
        "/admin/support/assignment-rules",
    ):
        assert guidance_for_path(unrelated_path) is None


def test_customer_detail_guidance_explains_service_extension_states() -> None:
    guide = next(item for item in WORKFLOW_GUIDANCE if item.id == "customer-detail")

    content = " ".join((*guide.steps, *guide.notes)).lower()
    for state in ("pending", "applied", "canceled", "reversed"):
        assert state in content
    assert "billing-date impact" in content


def test_customer_detail_guidance_explains_stale_payment_intent_cancellation() -> None:
    guide = guidance_for_path("/admin/customers/person/customer-id/payment-intents")

    assert guide is not None
    assert guide.id == "customer-detail"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "cancel stale intent" in content
    assert "exact submitted proof" in content
    assert "no payment was received" in content
    assert "rejects its linked proof and cancels the intent together" in content
    assert "allowing the customer to start a new payment" in content


def test_project_guidance_explains_customer_typeahead_selection() -> None:
    guide = guidance_for_path("/admin/projects/new")

    assert guide is not None
    assert guide.id == "project-authoring"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "account id" in content
    assert "choose the matching result" in content
    assert "clear the customer field" in content
    assert "selected customer account" in content


def test_project_template_and_subtask_guidance_explains_revision_scope() -> None:
    template_guide = guidance_for_path(
        "/admin/projects/templates/template-id/tasks/editor"
    )
    task_guide = guidance_for_path("/admin/projects/tasks/task-id")

    assert template_guide is not None
    assert template_guide.id == "project-template-plans"
    template_content = " ".join((*template_guide.steps, *template_guide.notes)).lower()
    assert "new revision" in template_content
    assert "does not alter projects that already use the template" in template_content
    assert "all of its active subtasks are done" in template_content

    assert task_guide is not None
    assert task_guide.id == "project-task-subtasks"
    task_content = " ".join((*task_guide.steps, *task_guide.notes)).lower()
    assert "only to one project" in task_guide.purpose.lower()
    assert "preserves ad-hoc work" in task_content
    assert "read-only history" in task_content


def test_sales_quote_guidance_explains_direct_customer_subject() -> None:
    guide = guidance_for_path("/admin/sales/quotes/new")

    assert guide is not None
    assert guide.id == "sales-quotes"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "exactly one lead or customer" in content
    assert "does not create a lead" in content
    assert "does not" in content and "party binding" in content
    assert "reuses the existing active subscriber" in content
    assert "approve for payment" in content
    assert "material quote changes require a new review" in content


def test_sales_order_guidance_separates_funding_from_service_setup() -> None:
    guide = guidance_for_path("/admin/sales/sales-order/order-id")

    assert guide is not None
    assert guide.id == "sales-orders"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "record payment" in content
    assert "customer account credit" in content
    assert "does not create a subscription" in content
    assert "ip assignment" in content
    assert "create the subscription" in content
    assert "initial invoice only when billing should begin" in content


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


def test_team_inbox_guidance_explains_lazy_customer_link_search() -> None:
    guide = guidance_for_path("/admin/inbox/conversation-id")

    assert guide is not None
    assert guide.id == "team-inbox"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "click existing customer to load likely matches" in content
    assert "type at least two characters" in content
    assert "search all active customers" in content
    assert "choose the exact result" in content
    assert "link customer" in content


def test_smtp_sender_guidance_explains_keyring_and_mailbox_route_mapping() -> None:
    guide = guidance_for_path("/admin/system/email")

    assert guide is not None
    assert guide.id == "smtp-senders"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "settings-encryption keyring" in content
    assert "secret/settings/crypto#settings_encryption_keyring" in content
    assert "reply sender" in content
    assert "mailbox route" in content
    assert "recreate api and celery" in content


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


def test_admin_guidance_uses_one_accessible_centered_modal() -> None:
    layout = Path("templates/layouts/admin.html").read_text(encoding="utf-8")
    control = Path("templates/components/ui/workflow_help.html").read_text(
        encoding="utf-8"
    )
    placement = Path("static/js/admin-workflow-help.js").read_text(encoding="utf-8")
    billing = Path("templates/admin/billing/index.html").read_text(encoding="utf-8")

    assert "{% block workflow_guidance %}" in layout
    assert "data-admin-workflow-help-staging" in layout
    assert "admin-workflow-help.js" in layout
    assert "data-admin-workflow-help-control" in control
    assert 'document.querySelectorAll("main h1")' in placement
    assert '[role="dialog"], [hidden], [x-cloak]' in placement
    assert "data-admin-workflow-title-group" in placement
    assert "htmx:afterSwap" in placement
    assert 'aria-label="How this page works: {{ workflow_guide.title }}"' in control
    assert 'aria-haspopup="dialog"' in control
    assert 'aria-modal="true"' in control
    assert 'x-trap.inert.noscroll="workflowHelpOpen"' in control
    assert "items-center justify-center" in control
    assert "{{ workflow_guide.purpose }}" in control
    assert "{% for step in workflow_guide.steps %}" in control
    assert "billingHelpOpen" not in billing


def test_customer_list_uses_shared_workflow_help_placement() -> None:
    customer_list = Path("templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert "workflow_guidance" not in customer_list
    assert "workflow_help_control" not in customer_list


def test_olt_guidance_explains_canonical_status_and_evidence_freshness() -> None:
    guide = guidance_for_path("/admin/network/olts/olt-id")

    assert guide is not None
    assert guide.id == "olt-operational-health"
    content = " ".join((*guide.steps, *guide.notes)).lower()
    assert "working or not working" in content
    assert "administrative active or inactive" in content
    assert "fresh successful native olt poll" in content
    assert "linked monitoring record" in content
    assert "active" in content and "current" in content
