from pathlib import Path

from app.models.catalog import RadiusProfile
from app.models.radius import RadiusClient, RadiusServer
from app.services import web_network_radius


def test_radius_overview_paginates_clients_and_profiles_independently(db_session):
    server = RadiusServer(
        name="Primary",
        host="192.0.2.10",
        auth_port=1812,
        acct_port=1813,
        is_active=True,
    )
    db_session.add(server)
    db_session.flush()

    db_session.add_all(
        RadiusClient(
            server_id=server.id,
            client_ip=f"192.0.2.{index:03d}",
            shared_secret_hash=f"hash-{index}",
            is_active=True,
        )
        for index in range(12)
    )
    db_session.add_all(
        RadiusProfile(name=f"Profile {index:03d}", is_active=True)
        for index in range(12)
    )
    db_session.commit()

    query = web_network_radius.RadiusOverviewQuery.from_pages(
        clients_page=2,
        profiles_page=1,
    )
    page = web_network_radius.radius_page_data(db=db_session, query=query)

    assert page.clients.page_meta.page == 2
    assert page.clients.page_meta.total_items == 12
    assert page.clients.page_meta.total_pages == 2
    assert len(page.clients.items) == 2
    assert page.profiles.page_meta.page == 1
    assert page.profiles.page_meta.total_items == 12
    assert page.profiles.page_meta.total_pages == 2
    assert len(page.profiles.items) == 10


def test_radius_overview_template_preserves_independent_pages_and_activity_order():
    template = Path("templates/admin/network/radius/index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="nas-clients"' in template
    assert 'id="authentication-profiles"' in template
    assert "clients_page_meta" in template
    assert "profiles_page_meta" in template
    assert "'clients_page', 'profiles_page'" in template
    assert "'profiles_page', 'clients_page'" in template
    assert template.index("Recent Activity") > template.index(
        "Recent Authentication Sessions"
    )
    assert template.index("Recent Activity") > template.index(
        "Recent Authentication Errors"
    )
