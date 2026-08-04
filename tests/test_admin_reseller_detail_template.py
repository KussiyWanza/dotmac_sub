from __future__ import annotations

from pathlib import Path

from app.services import reseller_onboarding
from app.web.admin.resellers import templates

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates/admin/resellers/detail.html"
).read_text()


def test_link_customer_form_is_exposed_from_linked_subscribers_modal() -> None:
    linked_subscribers_heading = TEMPLATE.index("Linked Subscribers")
    link_customer_trigger = TEMPLATE.index("Link a customer")
    subscribers_table = TEMPLATE.index('<table class="w-full text-sm">')

    assert linked_subscribers_heading < link_customer_trigger < subscribers_table
    assert "showLinkCustomerModal: false" in TEMPLATE
    assert 'id="link-customer-modal"' in TEMPLATE
    assert 'role="dialog"' in TEMPLATE
    assert 'aria-modal="true"' in TEMPLATE
    assert TEMPLATE.count('action="/admin/resellers/{{ reseller.id }}/users/link"') == 1


def test_invite_user_form_is_exposed_from_reseller_details_header() -> None:
    reseller_details_heading = TEMPLATE.index("Reseller Details")
    invite_user_trigger = TEMPLATE.index("Invite user")
    reseller_details_list = TEMPLATE.index('<dl class="mt-4 space-y-3 text-sm">')

    assert reseller_details_heading < invite_user_trigger < reseller_details_list
    assert "showInviteUserModal: false" in TEMPLATE
    assert 'id="invite-reseller-user-modal"' in TEMPLATE
    assert 'aria-labelledby="invite-reseller-user-modal-title"' in TEMPLATE
    assert (
        TEMPLATE.count('action="/admin/resellers/{{ reseller.id }}/users/create"') == 1
    )


def test_first_class_reseller_invite_does_not_offer_subscriber_roles() -> None:
    template = templates.env.get_template("admin/resellers/_invite_role_field.html")

    html = template.render(
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.RESELLER_USER,
            subscriber_role_assignment_supported=False,
        )
    )

    assert 'name="role"' not in html
    assert "Subscriber roles do not apply to reseller portal users." in html


def test_legacy_reseller_invite_offers_subscriber_roles() -> None:
    template = templates.env.get_template("admin/resellers/_invite_role_field.html")

    html = template.render(
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.SUBSCRIBER,
            subscriber_role_assignment_supported=True,
        ),
        roles=[{"name": "reseller-admin"}],
    )

    assert 'name="role"' in html
    assert "reseller-admin" in html
