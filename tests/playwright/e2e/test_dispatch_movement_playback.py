from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

TECHNICIAN_ALPHA = "11111111-1111-4111-8111-111111111111"
TECHNICIAN_BRAVO = "22222222-2222-4222-8222-222222222222"


def test_technician_click_direct_navigation_and_refresh_keep_exact_history(
    admin_page, settings
):
    requested_technicians: list[str | None] = []

    def live_feed(route):
        route.fulfill(
            json={
                "count": 2,
                "live_count": 2,
                "stale_after_seconds": 120,
                "items": [
                    {
                        "technician_id": TECHNICIAN_ALPHA,
                        "person_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "label": "Technician Alpha",
                        "status": "on_shift",
                        "latitude": 6.51,
                        "longitude": 3.31,
                        "last_location_at": "2026-08-12T09:00:00Z",
                        "is_live": True,
                    },
                    {
                        "technician_id": TECHNICIAN_BRAVO,
                        "person_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "label": "Technician Bravo",
                        "status": "on_shift",
                        "latitude": 9.01,
                        "longitude": 7.41,
                        "last_location_at": "2026-08-12T09:00:00Z",
                        "is_live": True,
                    },
                ],
            }
        )

    def movement_feed(route):
        technician_id = parse_qs(urlparse(route.request.url).query).get(
            "technician_id", [None]
        )[0]
        requested_technicians.append(technician_id)
        points = {
            TECHNICIAN_ALPHA: [
                {
                    "latitude": 6.51,
                    "longitude": 3.31,
                    "captured_at": "2026-08-12T09:00:00Z",
                    "kind": "start",
                    "status": "en_route",
                    "label": "Alpha start",
                },
                {
                    "latitude": 6.52,
                    "longitude": 3.32,
                    "captured_at": "2026-08-12T09:15:00Z",
                    "kind": "arrival",
                    "status": "arrived",
                    "label": "Alpha arrival",
                },
            ],
            TECHNICIAN_BRAVO: [
                {
                    "latitude": 9.01,
                    "longitude": 7.41,
                    "captured_at": "2026-08-12T10:00:00Z",
                    "kind": "start",
                    "status": "en_route",
                    "label": "Bravo only",
                }
            ],
        }.get(technician_id, [])
        route.fulfill(
            json={
                "leg_count": 1 if points else 0,
                "point_count": len(points),
                "points": points,
            }
        )

    admin_page.route("**/admin/dispatch/live-map/feed?*", live_feed)
    admin_page.route(
        "**/admin/network/map/plant-data",
        lambda route: route.fulfill(
            json={
                "type": "FeatureCollection",
                "features": [],
                "counts": {},
                "unmatched_olts": 0,
            }
        ),
    )
    admin_page.route("**/admin/dispatch/movement-playback/feed?*", movement_feed)

    admin_page.goto(f"{settings.base_url}/admin/dispatch/live-map")
    admin_page.get_by_text("Technician Alpha", exact=True).click()
    admin_page.get_by_role("link", name="Movement playback").click()

    expect(admin_page).to_have_url(
        f"{settings.base_url}/admin/dispatch/movement-playback?technician_id={TECHNICIAN_ALPHA}"
    )
    expect(admin_page.locator("#stat-points")).to_have_text("2")
    assert requested_technicians[-1] == TECHNICIAN_ALPHA

    admin_page.goto(
        f"{settings.base_url}/admin/dispatch/movement-playback"
        f"?technician_id={TECHNICIAN_BRAVO}"
    )
    expect(admin_page.locator("#stat-points")).to_have_text("1")
    assert requested_technicians[-1] == TECHNICIAN_BRAVO

    before_refresh = requested_technicians.count(TECHNICIAN_BRAVO)
    admin_page.reload()
    expect(admin_page.locator("#stat-points")).to_have_text("1")
    assert requested_technicians.count(TECHNICIAN_BRAVO) == before_refresh + 1
    assert None not in requested_technicians
