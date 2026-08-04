from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.templating import Jinja2Templates

from app.models.catalog import SubscriptionStatus
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.services import (
    web_catalog_subscription_workflows as workflow_service,
)
from app.services import web_catalog_subscriptions as subscription_web_service
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
)


def _drifted_projection(db_session, subscription):
    subscription.status = SubscriptionStatus.active
    subscription.login = "projection-ui-user"
    subscription.ipv4_address = "10.92.0.4"
    pool = IpPool(
        name=f"Projection UI {uuid4()}",
        ip_version=IPVersion.ipv4,
        cidr="10.92.0.0/29",
        is_active=True,
    )
    address = IPv4Address(
        address="10.92.0.5",
        pool=pool,
        allocation_type="static",
    )
    assignment = IPAssignment(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address=address,
        is_active=True,
        is_primary=True,
    )
    db_session.add_all([pool, address, assignment])
    db_session.commit()
    return subscription, assignment


def _projection_patches(subscription):
    return (
        patch(
            "app.services.ip_consistency_audit._external_ip_state",
            return_value=({subscription.login: "10.92.0.4"}, {subscription.login}, 0),
        ),
        patch(
            "app.services.radius_projection_planner.plan_login_radius_projections",
            return_value={
                subscription.login: SimpleNamespace(
                    subscription_id=str(subscription.id),
                    plan=SimpleNamespace(mode="active", write_radreply=True),
                )
            },
        ),
    )


def test_projection_drift_builds_confirmed_server_owned_action(
    db_session,
    subscription,
):
    subscription, assignment = _drifted_projection(db_session, subscription)

    external_patch, planner_patch = _projection_patches(subscription)
    with external_patch, planner_patch:
        action = workflow_service._subscription_ipv4_projection_reconciliation_action(
            db_session,
            subscription=subscription,
        )

    assert action is not None
    assert action.visible is True
    assert action.allowed is True
    assert action.confirmation is not None
    assert action.action_url.endswith(f"/{subscription.id}/ipv4/reconcile")
    hidden = {item.key: item.value for item in action.hidden_values}
    assert hidden["assignment_id"] == str(assignment.id)
    assert hidden["preview_fingerprint"]
    assert hidden["idempotency_key"].startswith("admin-ipv4-projection:")
    assert "10.92.0.5" in (action.impact or "")
    assert "Billing, plan, add-ons, and service period remain unchanged" in (
        action.impact or ""
    )


def test_confirmed_projection_action_delegates_to_owner(
    db_session,
    subscription,
):
    subscription, assignment = _drifted_projection(db_session, subscription)
    external_patch, planner_patch = _projection_patches(subscription)
    with external_patch, planner_patch:
        action = workflow_service._subscription_ipv4_projection_reconciliation_action(
            db_session,
            subscription=subscription,
        )
    assert action is not None
    hidden = {item.key: item.value for item in action.hidden_values}
    command = workflow_service.SubscriptionIPv4ProjectionReconciliationCommand(
        subscription_id=subscription.id,
        assignment_id=assignment.id,
        preview_fingerprint=hidden["preview_fingerprint"],
        idempotency_key=hidden["idempotency_key"],
        actor_id="network-admin",
    )
    external_patch, planner_patch = _projection_patches(subscription)
    db_session.commit()

    with (
        external_patch,
        planner_patch,
        patch("app.services.ip_assignment_lifecycle.emit_event"),
    ):
        outcome = workflow_service.execute_subscription_ipv4_projection_reconciliation(
            db_session,
            command=command,
        )

    db_session.refresh(subscription)
    assert outcome.previous_address == "10.92.0.4"
    assert outcome.desired_address == "10.92.0.5"
    assert subscription.ipv4_address == "10.92.0.5"


def test_replace_same_ip_directs_projection_drift_to_reconcile_action(
    db_session,
    subscription,
):
    subscription, _assignment = _drifted_projection(db_session, subscription)
    external_patch, planner_patch = _projection_patches(subscription)

    with (
        external_patch,
        planner_patch,
        pytest.raises(
            ValueError,
            match="Use Reconcile served IPv4",
        ),
    ):
        subscription_web_service.replace_subscription_ipv4_with_owner(
            db_session,
            subscription_id=str(subscription.id),
            selector="existing-assignment",
            requested_ip="10.92.0.5",
            actor_id="network-admin",
        )

    db_session.refresh(subscription)
    assert subscription.ipv4_address == "10.92.0.4"


def test_ipv4_projection_action_partial_composes_shared_confirmation_form():
    action = ActionForm(
        key="admin.subscription_ipv4_projection_reconciliation",
        title="Reconcile served IPv4",
        description=("IPAM owns 10.92.0.5; served IPv4 and RADIUS report 10.92.0.4."),
        action_url="/admin/catalog/subscriptions/sub-1/ipv4/reconcile",
        submit_label="Reconcile served IPv4",
        fields=(),
        hidden_values=(
            ActionHiddenValue(key="assignment_id", value="assignment-1"),
            ActionHiddenValue(key="preview_fingerprint", value="fingerprint-1"),
            ActionHiddenValue(key="idempotency_key", value="operation-1"),
        ),
        impact="Rebuild RADIUS and reauthenticate only old-address sessions.",
        confirmation=ActionConfirmation(
            title="Confirm this exact IPv4 reconciliation",
            message="I reviewed the owner preview.",
        ),
    )
    env = Jinja2Templates(directory="templates").env
    html = env.get_template(
        "admin/catalog/_ipv4_projection_reconciliation.html"
    ).render(
        ipv4_projection_reconciliation_action=action,
        can_correct_subscription=True,
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="csrf-test")),
    )

    assert "IPv4 projection alignment" in html
    assert "Reconcile served IPv4" in html
    assert "Confirm this exact IPv4 reconciliation" in html
    assert 'name="preview_fingerprint" value="fingerprint-1"' in html
    assert 'name="_csrf_token" value="csrf-test"' in html
    assert "/admin/catalog/subscriptions/sub-1/ipv4/reconcile" in html
