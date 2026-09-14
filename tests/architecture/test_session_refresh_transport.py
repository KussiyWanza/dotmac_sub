from pathlib import Path


def test_session_refresh_asset_is_cache_busted_in_all_portal_layouts():
    for layout in ("admin.html", "customer.html", "reseller.html"):
        source = (Path("templates/layouts") / layout).read_text(encoding="utf-8")
        assert "/static/js/session-refresh.js?v=" in source


def test_admin_session_refresh_is_post_only_at_the_edge():
    for config in (
        Path("nginx/selfcare.dotmac.io.conf"),
        Path("deploy/nginx/selfcare.dotmac.io"),
    ):
        source = config.read_text(encoding="utf-8")
        section = source.split("location = /auth/session/refresh {", 1)[1][:800]
        assert "$request_method != POST" in section
        assert "return 405;" in section


def test_reseller_background_refresh_is_post_only():
    source = Path("app/web/reseller/auth.py").read_text(encoding="utf-8")
    refresh_route = source.split("def reseller_refresh", 1)[0].rsplit("@router.", 1)[1]
    assert refresh_route.startswith('post("/refresh")')
