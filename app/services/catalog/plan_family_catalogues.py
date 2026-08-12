"""Owner for approved, versioned plan-family catalogue PDFs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType
from app.models.plan_family_catalogue import PlanFamilyCatalogue
from app.schemas.plan_family_catalogue import (
    PlanFamilyCatalogueOption,
    PublicPlanFamilyCatalogue,
    PublishPlanFamilyCatalogueCommand,
    PublishPlanFamilyCatalogueOutcome,
    ResolveShareablePlanFamilyCatalogueQuery,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.file_storage import FileValidationError, file_uploads
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.web_catalog_offers import plan_family_values

OWNER = "service_intent.plan_family_catalogues"
CONCERN = "approved plan-family catalogue publication"
_PUBLISH = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="publish_plan_family_catalogue",
)


class PlanFamilyCatalogueError(DomainError):
    """Safe domain failure mapped by web adapters."""


def _error(code: str, message: str, **details: object) -> PlanFamilyCatalogueError:
    return PlanFamilyCatalogueError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


def _normalize_family(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _label(value: str) -> str:
    return value.replace("_", " ").title()


def _published_rows(db: Session) -> dict[str, PlanFamilyCatalogue]:
    rows = db.scalars(
        select(PlanFamilyCatalogue)
        .options(joinedload(PlanFamilyCatalogue.stored_file))
        .where(PlanFamilyCatalogue.status == "published")
        .order_by(
            PlanFamilyCatalogue.plan_family.asc(),
            PlanFamilyCatalogue.version.desc(),
        )
    ).all()
    return {row.plan_family: row for row in rows}


def list_catalogue_options(db: Session) -> tuple[PlanFamilyCatalogueOption, ...]:
    """Return every configured family, including those still missing a PDF."""

    published = _published_rows(db)
    options: list[PlanFamilyCatalogueOption] = []
    for family in plan_family_values(db):
        normalized = _normalize_family(family)
        row = published.get(normalized)
        stored = row.stored_file if row is not None else None
        shareable = bool(
            row is not None
            and stored is not None
            and not stored.is_deleted
            and stored.content_type == "application/pdf"
        )
        options.append(
            PlanFamilyCatalogueOption(
                plan_family=normalized,
                label=_label(normalized),
                catalogue_id=row.id if row is not None else None,
                display_name=row.display_name if row is not None else None,
                version=row.version if row is not None else None,
                filename=stored.original_filename if stored is not None else None,
                file_size=stored.file_size if stored is not None else None,
                is_shareable=shareable,
            )
        )
    return tuple(options)


def resolve_public_catalogue(
    db: Session, catalogue_id: UUID
) -> PublicPlanFamilyCatalogue | None:
    """Resolve a public brochure version; drafts/withdrawals fail closed."""

    row = db.scalar(
        select(PlanFamilyCatalogue)
        .options(joinedload(PlanFamilyCatalogue.stored_file))
        .where(
            PlanFamilyCatalogue.id == catalogue_id,
            PlanFamilyCatalogue.status.in_(("published", "superseded")),
        )
    )
    if row is None or row.stored_file is None or row.stored_file.is_deleted:
        return None
    if row.stored_file.content_type != "application/pdf":
        return None
    return PublicPlanFamilyCatalogue(
        catalogue_id=row.id,
        plan_family=row.plan_family,
        display_name=row.display_name,
        version=row.version,
        filename=row.stored_file.original_filename,
        content_type=row.stored_file.content_type,
        file_size=row.stored_file.file_size,
        stored_file_id=row.stored_file.id,
    )


def resolve_shareable_catalogue(
    db: Session, query: ResolveShareablePlanFamilyCatalogueQuery
) -> PublicPlanFamilyCatalogue:
    normalized = _normalize_family(query.plan_family)
    row = db.scalar(
        select(PlanFamilyCatalogue)
        .options(joinedload(PlanFamilyCatalogue.stored_file))
        .where(
            PlanFamilyCatalogue.plan_family == normalized,
            PlanFamilyCatalogue.status == "published",
        )
        .order_by(PlanFamilyCatalogue.version.desc())
    )
    if row is None:
        raise _error(
            "catalogue_unavailable",
            f"No published {_label(normalized)} catalogue is available.",
            plan_family=normalized,
        )
    resolved = resolve_public_catalogue(db, row.id)
    if resolved is None:
        raise _error(
            "catalogue_unavailable",
            f"The {_label(normalized)} catalogue file is unavailable.",
            plan_family=normalized,
        )
    return resolved


def publish_catalogue(
    db: Session, command: PublishPlanFamilyCatalogueCommand
) -> PublishPlanFamilyCatalogueOutcome:
    return execute_owner_command(
        db,
        definition=_PUBLISH,
        context=command.context,
        operation=lambda: _publish_catalogue(db, command),
    )


def _publish_catalogue(
    db: Session, command: PublishPlanFamilyCatalogueCommand
) -> PublishPlanFamilyCatalogueOutcome:
    family = _normalize_family(command.plan_family)
    display_name = command.display_name.strip()
    if not display_name:
        raise _error("display_name_required", "Catalogue name is required.")
    if len(display_name) > 160:
        raise _error("display_name_too_long", "Catalogue name is too long.")
    if not command.file_bytes:
        raise _error("file_required", "Choose a PDF catalogue to upload.")

    # Do external object-storage I/O before the first database operation.  The
    # command transaction is active, but SQLAlchemy has not checked out a
    # connection yet; this keeps a slow upload from leaving PostgreSQL idle in
    # a transaction until its timeout terminates the request.
    catalogue_id = uuid4()
    try:
        prepared_file = file_uploads.prepare_upload(
            domain="catalogues",
            entity_type="plan_family_catalogue",
            entity_id=str(catalogue_id),
            original_filename=command.original_filename,
            content_type=command.content_type,
            data=command.file_bytes,
            uploaded_by=None,
            owner_subscriber_id=None,
        )
    except FileValidationError as exc:
        raise _error("invalid_file", str(exc)) from exc

    configured = {_normalize_family(item) for item in plan_family_values(db)}
    if family not in configured:
        raise _error(
            "invalid_plan_family",
            "Choose a configured catalogue plan family.",
            plan_family=family,
        )

    rows = db.scalars(
        select(PlanFamilyCatalogue)
        .options(joinedload(PlanFamilyCatalogue.stored_file))
        .where(PlanFamilyCatalogue.plan_family == family)
        .order_by(PlanFamilyCatalogue.version.desc())
        .with_for_update()
    ).all()
    checksum = hashlib.sha256(command.file_bytes).hexdigest()
    current = next((row for row in rows if row.status == "published"), None)
    if (
        current is not None
        and current.stored_file is not None
        and current.stored_file.checksum == checksum
    ):
        return PublishPlanFamilyCatalogueOutcome(
            catalogue_id=current.id,
            plan_family=current.plan_family,
            display_name=current.display_name,
            version=current.version,
            stored_file_id=current.stored_file_id,
            replayed=True,
        )

    now = datetime.now(UTC)
    for row in rows:
        if row.status == "published":
            row.status = "superseded"
            row.superseded_at = now

    version = (
        int(
            db.scalar(
                select(func.max(PlanFamilyCatalogue.version)).where(
                    PlanFamilyCatalogue.plan_family == family
                )
            )
            or 0
        )
        + 1
    )
    stored = file_uploads.stage_prepared_upload(db=db, prepared=prepared_file)
    row = PlanFamilyCatalogue(
        id=catalogue_id,
        plan_family=family,
        version=version,
        status="published",
        display_name=display_name,
        description=(command.description or "").strip() or None,
        stored_file_id=stored.id,
        created_by_system_user_id=command.actor_system_user_id,
        published_at=now,
    )
    db.add(row)
    db.flush()
    stage_audit_event(
        db,
        action="catalog.plan_family_catalogue.published",
        entity_type="plan_family_catalogue",
        entity_id=str(row.id),
        actor_type=AuditActorType.user,
        actor_id=str(command.actor_system_user_id),
        metadata={"plan_family": family, "version": version},
    )
    emit_event(
        db,
        EventType.plan_family_catalogue_published,
        {
            "catalogue_id": str(row.id),
            "plan_family": family,
            "version": version,
            "stored_file_id": str(stored.id),
        },
        actor=OWNER,
    )
    return PublishPlanFamilyCatalogueOutcome(
        catalogue_id=row.id,
        plan_family=family,
        display_name=row.display_name,
        version=version,
        stored_file_id=stored.id,
        replayed=False,
    )
