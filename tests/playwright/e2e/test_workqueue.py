"""Authenticated browser coverage for the native agent workqueue."""

from __future__ import annotations

from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser

pytestmark = pytest.mark.e2e

WORKQUEUE_PATH = "/admin/workqueue"


def _mark_admin_tour_seen(page: Page) -> None:
    """Keep the global first-login tour from covering this page-specific flow."""

    page.add_init_script(
        "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
    )


def _assert_rendered_post_forms_have_csrf(page: Page) -> None:
    forms = page.locator('form[method="post" i]')
    assert forms.count() > 0, "expected rendered workqueue action forms"
    for index in range(forms.count()):
        form = forms.nth(index)
        token = form.locator('input[name="_csrf_token"]')
        assert token.count() == 1, (
            "rendered workqueue POST form must contain exactly one CSRF token: "
            f"{form.get_attribute('action')}"
        )
        assert (token.get_attribute("value") or "").strip()
        request_id = form.locator('input[name="request_id"]')
        assert request_id.count() == 1
        assert (request_id.get_attribute("value") or "").strip()


@pytest.fixture()
def workqueue_ticket(e2e_db, settings):
    username = settings.admin_username
    assert username
    system_user = e2e_db.query(SystemUser).filter(SystemUser.email == username).one()
    assert system_user.person_party_id is not None
    suffix = uuid4().hex[:10]
    team = ServiceTeam(name=f"E2E Workqueue {suffix}")
    e2e_db.add(team)
    e2e_db.flush()
    e2e_db.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=system_user.person_party_id,
        )
    )
    ticket = Ticket(
        title=f"E2E queue ticket {suffix}",
        status=TicketStatus.open.value,
        priority="urgent",
        service_team_id=team.id,
    )
    e2e_db.add(ticket)
    e2e_db.commit()
    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "system_user_id": system_user.id,
    }


class TestNativeAgentWorkqueue:
    def test_admin_can_claim_and_complete_through_native_owner(
        self,
        admin_page: Page,
        settings,
        e2e_db,
        workqueue_ticket,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        response = admin_page.goto(f"{settings.base_url}{WORKQUEUE_PATH}")
        assert response is not None
        assert response.status == 200
        expect(
            admin_page.get_by_role("heading", name="Agent workqueue")
        ).to_be_visible()
        expect(admin_page.get_by_role("link", name="Open Inbox")).to_be_visible()
        expect(admin_page.get_by_text("Generated", exact=False).first).to_be_visible()
        _assert_rendered_post_forms_have_csrf(admin_page)

        section = admin_page.locator("#workqueue-section-ticket")
        row = section.locator("article").filter(has_text=workqueue_ticket["title"])
        expect(row).to_have_count(1)
        row.locator("summary").click()
        row.get_by_role("button", name="Claim for me").click()

        row = section.locator("article").filter(has_text=workqueue_ticket["title"])
        expect(row).to_have_count(1)
        row.locator("summary").click()
        expect(row.get_by_role("button", name="Claim for me")).to_have_count(0)
        row.locator('input[name="confirmed"]').check()
        row.get_by_role("button", name="Complete through owner").click()
        expect(
            section.locator("article").filter(has_text=workqueue_ticket["title"])
        ).to_have_count(0)

        e2e_db.expire_all()
        ticket = e2e_db.get(Ticket, workqueue_ticket["ticket_id"])
        assert ticket is not None
        assert ticket.assigned_to_person_id == workqueue_ticket["system_user_id"]
        assert ticket.status == TicketStatus.resolved.value

    def test_narrow_view_keeps_filters_and_primary_action_usable(
        self,
        admin_page: Page,
        settings,
        workqueue_ticket,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        admin_page.set_viewport_size({"width": 390, "height": 844})
        admin_page.goto(f"{settings.base_url}{WORKQUEUE_PATH}")

        expect(
            admin_page.get_by_role("heading", name="Agent workqueue")
        ).to_be_visible()
        expect(admin_page.get_by_label("Audience")).to_be_visible()
        expect(admin_page.get_by_label("Service team")).to_be_visible()
        expect(admin_page.get_by_role("link", name="Open Inbox")).to_be_visible()
        overflow = admin_page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1
