from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.schemas.plan_family_catalogue import PublicPlanFamilyCatalogue
from app.web.admin.inbox import router as inbox_router
from app.web.public.catalogues import router as public_catalogue_router


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(public_catalogue_router)
    app.include_router(inbox_router)
    app.dependency_overrides[get_db] = lambda: db_session
    for route in inbox_router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_share_catalogue_queues_the_selected_versioned_public_link(db_session):
    conversation_id = uuid4()
    catalogue_id = uuid4()
    captured: dict[str, object] = {}

    def reply(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(replayed=False, kind="queued", sender="Support")

    resolved = PublicPlanFamilyCatalogue(
        catalogue_id=catalogue_id,
        plan_family="dedicated",
        display_name="Dedicated Plan Catalogue",
        version=3,
        filename="dedicated.pdf",
        content_type="application/pdf",
        file_size=1200,
        stored_file_id=uuid4(),
    )
    with (
        patch(
            "app.services.catalog.plan_family_catalogues.resolve_shareable_catalogue",
            return_value=resolved,
        ),
        patch("app.services.team_inbox_commands.reply", side_effect=reply),
        patch("app.services.web_admin.get_actor_id", return_value=str(uuid4())),
    ):
        response = _client(db_session).post(
            f"/inbox/{conversation_id}/share-catalogue",
            data={"plan_family": "dedicated"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert captured["conversation_id"] == conversation_id
    assert "Dedicated Plan Catalogue" in str(captured["body_text"])
    assert f"/catalogues/{catalogue_id}/download" in str(captured["body_text"])
