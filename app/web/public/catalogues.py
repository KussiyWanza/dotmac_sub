"""Unauthenticated delivery adapter for approved public plan catalogues."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.stored_file import StoredFile
from app.services.catalog import plan_family_catalogues
from app.services.file_storage import build_content_disposition, file_uploads
from app.services.object_storage import ObjectNotFoundError

router = APIRouter(prefix="/catalogues", tags=["web-public-catalogues"])


@router.get("/{catalogue_id}/download", name="public_catalogue_download")
def public_catalogue_download(
    catalogue_id: UUID, db: Session = Depends(get_db)
) -> StreamingResponse:
    resolved = plan_family_catalogues.resolve_public_catalogue(db, catalogue_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Catalogue not found")
    stored = db.get(StoredFile, resolved.stored_file_id)
    if stored is None or stored.is_deleted:
        raise HTTPException(status_code=404, detail="Catalogue not found")
    try:
        stream = file_uploads.stream_file(stored)
    except ObjectNotFoundError:
        raise HTTPException(status_code=404, detail="Catalogue not found") from None
    return StreamingResponse(
        stream.chunks,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(resolved.filename),
            "Content-Length": str(resolved.file_size),
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
