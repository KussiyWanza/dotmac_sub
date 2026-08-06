from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.models.plan_family_catalogue import PlanFamilyCatalogue
from app.models.stored_file import StoredFile
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation
from app.schemas.plan_family_catalogue import PublishPlanFamilyCatalogueCommand
from app.services.catalog import plan_family_catalogues
from app.services.file_storage import FileValidationError, file_uploads
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake


def _staff(db_session) -> UUID:
    user = SystemUser(
        first_name="Catalogue",
        last_name="Publisher",
        email=f"catalogue-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    db_session.commit()
    return user_id


def _command(user_id: UUID, *, payload: bytes) -> PublishPlanFamilyCatalogueCommand:
    return PublishPlanFamilyCatalogueCommand(
        context=CommandContext.system(
            actor=f"system_user:{user_id}",
            scope="catalog:write",
            reason="pytest catalogue publication",
            idempotency_key=f"catalogue:{uuid4()}",
        ),
        plan_family="home_flex",
        display_name="Home Flex Plan Catalogue",
        description="Approved residential plans",
        original_filename="home-flex.pdf",
        content_type="application/pdf",
        file_bytes=payload,
        actor_system_user_id=user_id,
    )


def _stub_side_effects(monkeypatch) -> None:
    def stage_upload(**kwargs):
        data = kwargs["data"]
        record = StoredFile(
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"catalogues/{hashlib.sha256(data).hexdigest()}.pdf",
            file_size=len(data),
            content_type="application/pdf",
            checksum=hashlib.sha256(data).hexdigest(),
            storage_provider="s3",
        )
        kwargs["db"].add(record)
        kwargs["db"].flush()
        return record

    monkeypatch.setattr(
        plan_family_catalogues.file_uploads, "stage_upload", stage_upload
    )
    monkeypatch.setattr(
        plan_family_catalogues, "stage_audit_event", lambda *_a, **_k: None
    )
    monkeypatch.setattr(plan_family_catalogues, "emit_event", lambda *_a, **_k: None)


def test_publish_replays_same_pdf_and_supersedes_changed_pdf(db_session, monkeypatch):
    user = _staff(db_session)
    _stub_side_effects(monkeypatch)

    first = plan_family_catalogues.publish_catalogue(
        db_session, _command(user, payload=b"%PDF-1.4 first")
    )
    replay = plan_family_catalogues.publish_catalogue(
        db_session, _command(user, payload=b"%PDF-1.4 first")
    )
    second = plan_family_catalogues.publish_catalogue(
        db_session, _command(user, payload=b"%PDF-1.4 changed")
    )

    assert replay.catalogue_id == first.catalogue_id
    assert replay.replayed is True
    assert second.version == 2
    assert (
        db_session.get(PlanFamilyCatalogue, first.catalogue_id).status == "superseded"
    )
    assert (
        db_session.get(PlanFamilyCatalogue, second.catalogue_id).status == "published"
    )
    options = {
        item.plan_family: item
        for item in plan_family_catalogues.list_catalogue_options(db_session)
    }
    assert options["home_flex"].catalogue_id == second.catalogue_id
    assert options["home_flex"].is_shareable is True


def test_public_resolution_keeps_superseded_links_but_denies_withdrawn(
    db_session, monkeypatch
):
    user = _staff(db_session)
    _stub_side_effects(monkeypatch)
    first = plan_family_catalogues.publish_catalogue(
        db_session, _command(user, payload=b"%PDF-1.4 first")
    )
    plan_family_catalogues.publish_catalogue(
        db_session, _command(user, payload=b"%PDF-1.4 replacement")
    )

    assert plan_family_catalogues.resolve_public_catalogue(
        db_session, first.catalogue_id
    )
    row = db_session.get(PlanFamilyCatalogue, first.catalogue_id)
    row.status = "withdrawn"
    db_session.commit()
    assert (
        plan_family_catalogues.resolve_public_catalogue(db_session, first.catalogue_id)
        is None
    )


def test_catalogue_upload_domain_is_pdf_only():
    config = file_uploads.get_domain_config("catalogues")
    assert file_uploads.validate(
        config=config,
        filename="plans.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 test",
    ) == ("plans.pdf", "application/pdf")
    try:
        file_uploads.validate(
            config=config,
            filename="plans.txt",
            content_type="text/plain",
            data=b"not a PDF",
        )
    except FileValidationError:
        pass
    else:
        raise AssertionError("a non-PDF catalogue was accepted")


def test_lead_form_is_hidden_for_customer_contacts_and_resolved_threads(
    db_session, monkeypatch
):
    conversation = InboxConversation(
        channel_type="email",
        status="open",
        contact_address="contact@example.com",
        is_active=True,
        metadata_={"contact_resolution": {"status": "unmatched"}},
    )
    db_session.add(conversation)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.customer_identity_resolution.resolve_customer_identity",
        lambda *_a, **_k: SimpleNamespace(matched=True, ambiguous=False),
    )

    contact = lead_intake.manual_invitation_eligibility(db_session, conversation.id)
    assert contact.eligible is False
    assert "customer contact" in contact.reason

    conversation.status = "resolved"
    db_session.commit()
    resolved = lead_intake.manual_invitation_eligibility(db_session, conversation.id)
    assert resolved.eligible is False
    assert "Reopen" in resolved.reason
