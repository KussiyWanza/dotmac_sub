"""Browser acceptance for the reversible core-device archive lifecycle."""

from __future__ import annotations

from urllib.parse import quote_plus
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice
from app.services.db_session_adapter import db_session_adapter
from app.services.device_projection_reconcile import (
    ReconcileDeviceProjectionsCommand,
    reconcile_device_projections,
)
from app.services.owner_commands import CommandContext


def _refresh_device_projection(db: Session) -> None:
    """Run the registered projection repair exactly as production does."""
    db.expire_all()
    db_session_adapter.release_read_transaction(db)
    reconcile_device_projections(
        db,
        ReconcileDeviceProjectionsCommand(
            context=CommandContext.system(
                actor="service:playwright-core-device-archive",
                scope="network.device_projection.reconcile",
                reason="Refresh the device ledger for browser acceptance",
            )
        ),
    )


@pytest.fixture()
def archive_candidate(e2e_db: Session) -> NetworkDevice:
    suffix = uuid4().hex[:10]
    device = NetworkDevice(
        name=f"E2E archive candidate {suffix}",
        hostname=f"e2e-archive-{suffix}",
        is_active=True,
        ping_enabled=False,
        snmp_enabled=False,
    )
    e2e_db.add(device)
    e2e_db.commit()
    _refresh_device_projection(e2e_db)
    return device


def test_admin_can_archive_view_and_restore_core_device(
    admin_page: Page,
    settings,
    e2e_db: Session,
    archive_candidate: NetworkDevice,
) -> None:
    name = archive_candidate.name
    device_id = archive_candidate.id
    current_url = (
        f"{settings.base_url}/admin/network/devices?type=core&search={quote_plus(name)}"
    )
    admin_page.goto(current_url, wait_until="domcontentloaded")

    current_row = admin_page.locator("tbody tr", has_text=name)
    expect(current_row).to_be_visible()
    current_row.get_by_title("Decommission Device").click()
    expect(
        admin_page.get_by_role("heading", name="Decommission core device")
    ).to_be_visible()
    admin_page.get_by_label("Decommission reason").fill(
        "Removed from service in browser test"
    )
    admin_page.locator("#core-device-archive-modal").get_by_role(
        "button", name="Decommission device", exact=True
    ).click()

    admin_page.wait_for_url(
        f"**/admin/network/core-devices/{device_id}?message=*",
        wait_until="domcontentloaded",
    )
    expect(
        admin_page.get_by_text("This device is decommissioned and read-only.")
    ).to_be_visible()
    expect(admin_page.get_by_role("link", name="Edit")).to_have_count(0)

    _refresh_device_projection(e2e_db)
    archived_url = (
        f"{settings.base_url}/admin/network/devices?type=core&lifecycle=archived"
        f"&search={quote_plus(name)}"
    )
    admin_page.goto(archived_url, wait_until="domcontentloaded")
    archived_row = admin_page.locator("tbody tr", has_text=name)
    expect(archived_row).to_be_visible()
    expect(
        archived_row.locator("td").nth(2).get_by_text("Decommissioned", exact=True)
    ).to_be_visible()

    archived_row.get_by_title("Restore Device").click()
    confirmation = admin_page.get_by_role("dialog")
    expect(confirmation).to_be_visible()
    confirmation.get_by_role("button", name="Restore", exact=True).click()
    admin_page.wait_for_url(
        f"**/admin/network/core-devices/{device_id}?message=*",
        wait_until="domcontentloaded",
    )
    expect(admin_page.get_by_text(f"{name} restored as inactive.")).to_be_visible()
    expect(
        admin_page.get_by_text("This device is decommissioned and read-only.")
    ).to_have_count(0)

    e2e_db.expire_all()
    restored = e2e_db.get(NetworkDevice, device_id)
    assert restored is not None
    assert restored.archived_at is None
    assert restored.is_active is False


def test_support_user_cannot_open_archive_action(
    agent_page: Page,
    settings,
    archive_candidate: NetworkDevice,
) -> None:
    response = agent_page.goto(
        f"{settings.base_url}/admin/network/core-devices/"
        f"{archive_candidate.id}/archive/preview",
        wait_until="domcontentloaded",
    )
    assert response is not None
    assert response.status == 403
