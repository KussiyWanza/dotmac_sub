"""Authenticated browser coverage for the native service-team lifecycle."""

from __future__ import annotations

import re
from secrets import token_urlsafe
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
from playwright.sync_api import Locator, Page, expect

from app.models.gis import GeoArea
from app.models.service_team import ServiceTeamCapabilityDefinition
from app.services.service_team_composition import CAPABILITY_CONTRACTS
from tests.playwright.helpers.auth import login_for_token
from tests.staff_identity_fixtures import add_bound_staff_login

pytestmark = pytest.mark.e2e

SERVICE_TEAMS_PATH = "/admin/system/service-teams"


def _assert_rendered_post_forms_have_csrf(page: Page) -> None:
    forms = page.locator('form[method="post" i]')
    assert forms.count() > 0, "expected at least one rendered POST form"
    for index in range(forms.count()):
        form = forms.nth(index)
        token = form.locator('input[name="_csrf_token"]')
        assert token.count() == 1, (
            "rendered service-team POST form must contain exactly one CSRF token: "
            f"{form.get_attribute('action')}"
        )
        assert (token.get_attribute("value") or "").strip(), (
            "rendered service-team POST form contains an empty CSRF token: "
            f"{form.get_attribute('action')}"
        )


@pytest.fixture()
def support_page(browser, settings, api_context, e2e_db):
    """Create a canonical support principal in the disposable E2E database."""

    suffix = uuid4().hex[:10]
    username = f"e2e.service-team-support.{suffix}@example.com"
    password = token_urlsafe(24)
    add_bound_staff_login(
        e2e_db,
        role_name="support",
        email=username,
        password=password,
    )
    e2e_db.commit()
    session_token = login_for_token(api_context, username, password)

    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    context.add_cookies(
        [
            {
                "name": "session_token",
                "value": session_token,
                "url": settings.base_url,
                "httpOnly": True,
                "secure": settings.base_url.startswith("https://"),
                "sameSite": "Lax",
            }
        ]
    )
    page = context.new_page()
    yield page
    page.close()
    context.close()


@pytest.fixture()
def capability_vocabulary(e2e_db) -> None:
    """Register the governed capability vocabulary in the disposable E2E DB.

    Service-level composition writes fail closed on unregistered capabilities.
    Migration 440_composable_service_teams seeds the six definitions, but a
    database built from model metadata starts empty, so reconcile the rows to
    the exact code contract (the drift guard rejects any mismatch).
    """

    for contract in CAPABILITY_CONTRACTS.values():
        definition = e2e_db.get(ServiceTeamCapabilityDefinition, contract.key.value)
        if definition is None:
            definition = ServiceTeamCapabilityDefinition(
                key=contract.key.value,
                display_name=contract.display_name,
                contract_owner=contract.contract_owner,
                contract_version=contract.contract_version,
                description=f"E2E-registered definition for {contract.key.value}",
                is_active=True,
            )
            e2e_db.add(definition)
        else:
            definition.display_name = contract.display_name
            definition.contract_owner = contract.contract_owner
            definition.contract_version = contract.contract_version
            definition.is_active = True
    e2e_db.commit()


@pytest.fixture()
def active_geo_area(e2e_db) -> tuple[str, str]:
    """Create an active GeoArea the scope form can bind."""

    area = GeoArea(name=f"E2E Coverage Area {uuid4().hex[:10]}", is_active=True)
    e2e_db.add(area)
    e2e_db.commit()
    return str(area.id), area.name


class TestNativeServiceTeamLifecycle:
    def test_admin_can_complete_the_reversible_lifecycle(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        """Create, edit, manage membership, deactivate, and reactivate."""

        suffix = uuid4().hex[:10]
        original_name = f"E2E Service Team {suffix}"
        edited_name = f"E2E Operations Team {suffix}"

        admin_page.goto(f"{settings.base_url}{SERVICE_TEAMS_PATH}")
        expect(admin_page.get_by_role("heading", name="Service Teams")).to_be_visible()
        admin_page.get_by_role("link", name="Create team").click()
        expect(
            admin_page.get_by_role("heading", name="Create service team")
        ).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)

        admin_page.get_by_label("Name").fill(original_name)
        admin_page.get_by_role("button", name="Create team").click()
        admin_page.wait_for_url(
            re.compile(r".*/admin/system/service-teams/[0-9a-f-]{36}$")
        )
        expect(admin_page.get_by_role("heading", name=original_name)).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)
        expect(admin_page.locator('form[action$="/delete"]')).to_have_count(0)
        expect(
            admin_page.get_by_role("button", name=re.compile("delete", re.I))
        ).to_have_count(0)

        admin_page.get_by_role("link", name="Edit identity").click()
        expect(
            admin_page.get_by_role("heading", name="Edit service team")
        ).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)
        admin_page.get_by_label("Name").fill(edited_name)
        admin_page.get_by_role("button", name="Save changes").click()
        expect(admin_page.get_by_role("heading", name=edited_name)).to_be_visible()

        staff_select = admin_page.locator('select[name="system_user_id"]')
        expect(staff_select).to_be_visible()
        staff_options = staff_select.locator('option:not([value=""])')
        assert staff_options.count() > 0, (
            "expected at least one active staff principal with a reviewed Party binding"
        )
        selected_staff = staff_options.first
        system_user_id = selected_staff.get_attribute("value")
        selected_staff_label = (selected_staff.text_content() or "").strip()
        assert system_user_id
        assert selected_staff_label
        member_email = selected_staff_label.rsplit(" — ", maxsplit=1)[-1]

        staff_select.select_option(system_user_id)
        add_form = admin_page.locator(
            f'form[action="{SERVICE_TEAMS_PATH}/'
            f'{admin_page.url.rsplit("/", 1)[-1]}/members"]'
        )
        with (
            admin_page.expect_request(
                lambda request: (
                    request.method == "POST" and request.url.endswith("/members")
                )
            ) as add_request,
            admin_page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/members")
                )
            ) as add_response,
        ):
            add_form.get_by_role("button", name="Add member").click()
        assert "role" not in parse_qs(add_request.value.post_data or "")
        assert add_response.value.status == 303
        expect(admin_page.get_by_text(member_email, exact=False).first).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)

        member_row = (
            admin_page.locator("div.grid.gap-3.py-4")
            .filter(has_text=member_email)
            .first
        )
        responsibility_form = member_row.locator('form[action$="/responsibilities"]')
        responsibility_form.locator('select[name="responsibility"]').select_option(
            "queue_lead"
        )
        responsibility_form.locator('select[name="is_active"]').select_option("true")
        responsibility_form.get_by_role("button", name="Apply").click()
        expect(member_row).to_contain_text("Queue Lead")

        remove_form = member_row.locator('form[action$="/remove"]')
        remove_form.locator('input[name="reason"]').fill("E2E lifecycle verification")
        remove_form.get_by_role("button", name="Remove").click()
        expect(member_row).to_contain_text("Inactive")

        lifecycle_form = admin_page.locator('form[action$="/active"]')
        lifecycle_form.locator('input[name="reason"]').fill(
            "E2E reversible lifecycle verification"
        )
        lifecycle_form.get_by_role("button", name="Deactivate team").click()
        expect(
            admin_page.locator("header").get_by_text("Inactive", exact=True)
        ).to_be_visible()

        lifecycle_form = admin_page.locator('form[action$="/active"]')
        lifecycle_form.locator('input[name="reason"]').fill(
            "E2E reversible lifecycle verification"
        )
        lifecycle_form.get_by_role("button", name="Activate team").click()
        expect(
            admin_page.locator("header").get_by_text("Active", exact=True)
        ).to_be_visible()

    def test_support_role_is_read_only(
        self,
        support_page: Page,
        settings,
    ) -> None:
        list_response = support_page.goto(f"{settings.base_url}{SERVICE_TEAMS_PATH}")
        assert list_response is not None
        assert list_response.status == 200
        expect(
            support_page.get_by_role("heading", name="Service Teams")
        ).to_be_visible()
        expect(support_page.get_by_role("link", name="Create team")).to_have_count(0)

        create_response = support_page.goto(
            f"{settings.base_url}{SERVICE_TEAMS_PATH}/new"
        )
        assert create_response is not None
        assert create_response.status == 403


class TestServiceTeamCompositionForms:
    """Browser coverage for the composition forms on the team detail page."""

    @staticmethod
    def _create_team(admin_page: Page, settings) -> str:
        team_name = f"E2E Composition Team {uuid4().hex[:10]}"
        admin_page.goto(f"{settings.base_url}{SERVICE_TEAMS_PATH}/new")
        expect(
            admin_page.get_by_role("heading", name="Create service team")
        ).to_be_visible()
        admin_page.get_by_label("Name").fill(team_name)
        admin_page.get_by_role("button", name="Create team").click()
        admin_page.wait_for_url(
            re.compile(r".*/admin/system/service-teams/[0-9a-f-]{36}$")
        )
        expect(admin_page.get_by_role("heading", name=team_name)).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)
        return team_name

    @staticmethod
    def _section(admin_page: Page, heading: str) -> Locator:
        return admin_page.locator("section").filter(
            has=admin_page.get_by_role("heading", name=heading, exact=True)
        )

    def _assign_capability(self, admin_page: Page, capability: str) -> None:
        section = self._section(admin_page, "Capabilities")
        form = section.locator('form[action$="/capabilities"]')
        form.locator('select[name="capability"]').select_option(capability)
        form.locator('select[name="is_active"]').select_option("true")
        form.get_by_role("button", name="Apply").click()

    def test_admin_can_assign_and_retire_a_capability(
        self,
        admin_page: Page,
        settings,
        capability_vocabulary,
    ) -> None:
        self._create_team(admin_page, settings)

        section = self._section(admin_page, "Capabilities")
        assigned = section.locator("div.mt-3")
        expect(assigned.get_by_text("No capability assigned.")).to_be_visible()

        self._assign_capability(admin_page, "customer_support")
        expect(assigned.get_by_text("Customer Support", exact=True)).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)

        form = section.locator('form[action$="/capabilities"]')
        form.locator('select[name="capability"]').select_option("customer_support")
        form.locator('select[name="is_active"]').select_option("false")
        form.get_by_role("button", name="Apply").click()
        expect(assigned.get_by_text("No capability assigned.")).to_be_visible()
        expect(assigned.get_by_text("Customer Support", exact=True)).to_have_count(0)

    def test_admin_can_bind_geo_area_and_global_scopes(
        self,
        admin_page: Page,
        settings,
        active_geo_area,
    ) -> None:
        geo_area_id, geo_area_name = active_geo_area
        self._create_team(admin_page, settings)

        section = self._section(admin_page, "Geographic scope")
        bindings = section.locator("div.mt-3")
        expect(bindings.get_by_text("No scope binding.")).to_be_visible()

        form = section.locator('form[action$="/scopes"]')
        form.locator('select[name="scope_type"]').select_option("geo_area")
        form.locator('select[name="geo_area_id"]').select_option(geo_area_id)
        form.locator('select[name="is_active"]').select_option("true")
        form.get_by_role("button", name="Apply").click()
        expect(bindings.get_by_text(geo_area_name, exact=True)).to_be_visible()

        form = section.locator('form[action$="/scopes"]')
        form.locator('select[name="scope_type"]').select_option("global")
        form.locator('select[name="geo_area_id"]').select_option("")
        form.locator('select[name="is_active"]').select_option("true")
        form.get_by_role("button", name="Apply").click()
        expect(bindings.get_by_text("Global", exact=True)).to_be_visible()
        expect(bindings.get_by_text(geo_area_name, exact=True)).to_be_visible()

    def test_admin_can_record_an_external_reference(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        self._create_team(admin_page, settings)
        suffix = uuid4().hex[:10]
        account_scope = f"e2e-workspace-{suffix}"
        external_id = f"e2e-team-{suffix}"

        section = self._section(admin_page, "External references")
        observed = section.locator("div.mt-3")
        expect(observed.get_by_text("No external reference observed.")).to_be_visible()

        form = section.locator('form[action$="/external-references"]')
        form.locator('input[name="provider"]').fill("Slack")
        form.locator('input[name="account_scope"]').fill(account_scope)
        form.locator('input[name="external_id"]').fill(external_id)
        form.locator('input[name="provenance"]').fill("e2e-observation")
        form.locator('select[name="is_active"]').select_option("true")
        form.get_by_role("button", name="Apply").click()

        reference_row = observed.locator("> div").filter(has_text=external_id)
        expect(reference_row).to_be_visible()
        # The composition owner casefolds the provider before persisting it.
        expect(reference_row.get_by_text("slack", exact=True)).to_be_visible()
        expect(reference_row).to_contain_text(account_scope)
        expect(reference_row).to_contain_text("e2e-observation")

    def test_admin_can_register_an_outage_routing_policy(
        self,
        admin_page: Page,
        settings,
        capability_vocabulary,
    ) -> None:
        self._create_team(admin_page, settings)

        # The routing contract fails closed without the required capability.
        self._assign_capability(admin_page, "outage_response")
        expect(
            self._section(admin_page, "Capabilities")
            .locator("div.mt-3")
            .get_by_text("Outage Response", exact=True)
        ).to_be_visible()

        section = self._section(admin_page, "Routing policies")
        policies = section.locator("div.mt-3")
        expect(policies.get_by_text("No routing policy.")).to_be_visible()

        # The registration form is assign-only: is_active is a hidden
        # "true" input and retirement is a per-policy Retire button.
        form = section.locator("form").filter(
            has=admin_page.get_by_role("button", name="Assign route")
        )
        expect(form.locator('input[name="is_active"]')).to_be_hidden()
        form.locator('select[name="route"]').select_option(
            "network.outage|incident.primary"
        )
        form.locator('input[name="priority"]').fill("100")
        form.locator('select[name="scope_binding_id"]').select_option("")
        form.get_by_role("button", name="Assign route").click()

        policy_row = policies.locator("> div").filter(has_text="incident.primary")
        expect(policy_row).to_be_visible()
        expect(policy_row).to_contain_text("network.outage")
        expect(policy_row).to_contain_text("priority 100")
        expect(policy_row.get_by_role("button", name="Retire")).to_be_visible()
