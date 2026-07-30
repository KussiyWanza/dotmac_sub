from pathlib import Path

from jinja2 import Environment, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_four_page_routers_are_registered() -> None:
    admin_init = _read("app/web/admin/__init__.py")
    public_init = _read("app/web/public/__init__.py")

    assert "help_center_router" in admin_init
    assert "meta_connection_router" in admin_init
    assert "surveys_router" in admin_init
    assert "surveys_router" in public_init


def test_new_admin_pages_are_discoverable() -> None:
    sidebar = _read("templates/components/navigation/admin_sidebar.html")

    assert '"/admin/surveys"' in sidebar
    assert '"/admin/crm/meta"' in sidebar
    assert '"/admin/help"' in sidebar


def test_new_templates_parse_as_jinja() -> None:
    environment = Environment(autoescape=select_autoescape())
    paths = (
        "templates/admin/surveys/index.html",
        "templates/admin/surveys/form.html",
        "templates/admin/surveys/detail.html",
        "templates/admin/help/index.html",
        "templates/admin/inbox/meta_connection.html",
        "templates/public/surveys/respond.html",
        "templates/public/surveys/thank_you.html",
        "templates/public/surveys/unavailable.html",
    )

    for path in paths:
        environment.parse(_read(path))


def test_public_survey_form_has_csrf_and_rating() -> None:
    template = _read("templates/public/surveys/respond.html")

    assert 'name="_csrf_token"' in template
    assert 'name="rating"' in template
