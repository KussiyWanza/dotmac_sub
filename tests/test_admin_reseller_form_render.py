from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from app.services import reseller_onboarding
from app.web.admin.resellers import templates

RESELLER_ID = UUID("6017d1ca-8c53-4bc2-a969-5b7f769f142a")


class _State:
    csrf_token = "test-csrf-token"
    auth: dict[str, object] = {"permission_keys": {"*"}}


class _URL:
    path = f"/admin/resellers/{RESELLER_ID}/edit"

    def __str__(self) -> str:
        return self.path


class _Request:
    state = _State()
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    url = _URL()
    session: dict[str, object] = {}
    client = None
    scope: dict[str, object] = {}

    def url_for(self, *args: object, **kwargs: object) -> str:
        return "/"


def _render(*, reseller: SimpleNamespace | None) -> str:
    return templates.env.get_template("admin/resellers/reseller_form.html").render(
        request=_Request(),
        active_page="resellers",
        active_menu="customers",
        current_user={"name": "Test Admin", "email": "admin@example.com"},
        sidebar_stats={},
        reseller=reseller,
        action_url=(
            f"/admin/resellers/{RESELLER_ID}" if reseller else "/admin/resellers"
        ),
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.RESELLER_USER,
            subscriber_role_assignment_supported=False,
        ),
        roles=[],
        policy_sets=[],
        error=None,
    )


def test_edit_reseller_renders_detail_navigation() -> None:
    reseller = SimpleNamespace(
        id=RESELLER_ID,
        name="Example Reseller",
        code="EXAMPLE",
        contact_email="partner@example.com",
        contact_phone="08000000000",
        policy_set_id=None,
        notes=None,
        is_active=True,
        restrict_to_assigned_offers=False,
    )

    html = _render(reseller=reseller)

    assert f'href="/admin/resellers/{RESELLER_ID}"' in html
    assert "View reseller" in html


def test_create_reseller_does_not_render_detail_navigation() -> None:
    html = _render(reseller=None)

    assert "View reseller" not in html
    assert 'name="user_role"' not in html
    assert "Subscriber roles do not apply to reseller portal users." in html
