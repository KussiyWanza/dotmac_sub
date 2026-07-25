from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxMediaAsset, InboxMessage


def _text(value: object, *, max_length: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_length] if max_length is not None else text


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _asset_type(raw: dict[str, Any]) -> str:
    for key in ("type", "asset_type", "media_type", "kind"):
        value = _text(raw.get(key), max_length=40)
        if value:
            return value
    mime_type = _text(raw.get("mime_type") or raw.get("mime"), max_length=160)
    if mime_type and "/" in mime_type:
        return mime_type.split("/", 1)[0][:40]
    return "attachment"


def _provider_media_id(raw: dict[str, Any]) -> str | None:
    for key in ("provider_media_id", "media_id", "attachment_id", "id"):
        value = _text(raw.get(key), max_length=255)
        if value:
            return value
    return None


def _file_name(raw: dict[str, Any]) -> str | None:
    return _text(raw.get("file_name") or raw.get("filename"), max_length=255)


def _source_url(raw: dict[str, Any]) -> str | None:
    return _text(raw.get("url") or raw.get("source_url") or raw.get("link"))


def _existing_asset(
    db: Session,
    *,
    message: InboxMessage,
    raw: dict[str, Any],
) -> InboxMediaAsset | None:
    provider = _text(raw.get("provider"), max_length=80)
    provider_media_id = _provider_media_id(raw)
    if provider_media_id:
        existing = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .filter(InboxMediaAsset.provider_media_id == provider_media_id)
            .first()
        )
        if existing is not None:
            return existing
    file_name = _file_name(raw)
    source_url = _source_url(raw)
    if not provider and not provider_media_id and not file_name and not source_url:
        return None
    return (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.message_id == message.id)
        .filter(InboxMediaAsset.provider == provider)
        .filter(InboxMediaAsset.provider_media_id == provider_media_id)
        .filter(InboxMediaAsset.file_name == file_name)
        .filter(InboxMediaAsset.source_url == source_url)
        .first()
    )


def promote_message_attachments(
    db: Session,
    *,
    message: InboxMessage,
    provider: str | None = None,
) -> list[InboxMediaAsset]:
    metadata = message.metadata_ or {}
    raw_items = metadata.get("attachments")
    if not isinstance(raw_items, list):
        return []

    assets: list[InboxMediaAsset] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        raw = dict(raw_item)
        if provider and not raw.get("provider"):
            raw["provider"] = provider
        existing = _existing_asset(db, message=message, raw=raw)
        if existing is not None:
            assets.append(existing)
            continue
        asset = InboxMediaAsset(
            conversation_id=message.conversation_id,
            message_id=message.id,
            channel_type=message.channel_type,
            direction=message.direction,
            provider=_text(raw.get("provider"), max_length=80),
            provider_media_id=_provider_media_id(raw),
            asset_type=_asset_type(raw),
            file_name=_file_name(raw),
            mime_type=_text(raw.get("mime_type") or raw.get("mime"), max_length=160),
            file_size=_int(raw.get("file_size") or raw.get("size")),
            caption=_text(raw.get("caption")),
            source_url=_source_url(raw),
            storage_url=_text(raw.get("storage_url")),
            checksum_sha256=_text(raw.get("checksum_sha256"), max_length=64),
            download_status=_text(raw.get("download_status"), max_length=40)
            or ("stored" if raw.get("storage_url") else "metadata_only"),
            metadata_=raw,
        )
        db.add(asset)
        assets.append(asset)
    db.flush()
    return assets


def promote_unmaterialized_assets(
    db: Session,
    *,
    limit: int = 200,
) -> int:
    rows = (
        db.query(InboxMessage)
        .order_by(InboxMessage.created_at.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    created_or_existing = 0
    for message in rows:
        metadata = message.metadata_ or {}
        if not isinstance(metadata.get("attachments"), list):
            continue
        before_count = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .count()
        )
        assets = promote_message_attachments(db, message=message)
        after_count = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .count()
        )
        if assets and after_count >= before_count:
            created_or_existing += len(assets)
    return created_or_existing


def assets_for_messages(
    db: Session,
    message_ids: list[UUID],
) -> dict[UUID, list[InboxMediaAsset]]:
    if not message_ids:
        return {}
    rows = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.message_id.in_(message_ids))
        .order_by(InboxMediaAsset.created_at.asc())
        .all()
    )
    grouped: dict[UUID, list[InboxMediaAsset]] = {}
    for row in rows:
        if row.message_id is not None:
            grouped.setdefault(row.message_id, []).append(row)
    return grouped


ALLOWED_OUTBOUND_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/zip",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
MAX_OUTBOUND_ATTACHMENT_BYTES = 10 * 1024 * 1024


class MediaUploadError(ValueError):
    """Rejected operator upload, safe for an admin adapter to render."""


def _outbound_asset_type(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    return "document"


def stage_outbound_attachment(
    db: Session,
    *,
    conversation,
    file_name: str,
    content_type: str | None,
    data: bytes,
    uploaded_by: str | None = None,
) -> InboxMediaAsset:
    """Store one operator-supplied file and record it against the conversation.

    Inbound media arrives already hosted by a provider and is promoted by
    ``promote_message_attachments``. An operator upload has no provider, so the
    bytes are staged through the shared file-storage participant and the asset
    is created here with ``message_id`` still unset — it is bound when the reply
    that carries it is actually sent, so an abandoned composer leaves no
    attachment claiming to belong to a message.
    """
    from app.services.file_storage import file_uploads

    clean_name = _text(file_name, max_length=255) or "attachment"
    mime_type = (
        (content_type or "application/octet-stream").split(";")[0].strip().lower()
    )

    if not data:
        raise MediaUploadError(f"{clean_name} is empty.")
    if len(data) > MAX_OUTBOUND_ATTACHMENT_BYTES:
        limit_mb = MAX_OUTBOUND_ATTACHMENT_BYTES // 1048576
        raise MediaUploadError(f"{clean_name} is larger than {limit_mb} MB.")
    if mime_type not in ALLOWED_OUTBOUND_MIME_TYPES:
        raise MediaUploadError(f"{clean_name} is not an allowed file type.")

    record = file_uploads.stage_upload(
        db=db,
        domain="attachments",
        entity_type="inbox_conversation",
        entity_id=str(conversation.id),
        original_filename=clean_name,
        content_type=mime_type,
        data=data,
        uploaded_by=uploaded_by,
    )

    asset = InboxMediaAsset(
        conversation_id=conversation.id,
        message_id=None,
        channel_type=conversation.channel_type,
        direction="outbound",
        provider=None,
        provider_media_id=None,
        asset_type=_outbound_asset_type(mime_type),
        file_name=record.original_filename,
        mime_type=mime_type,
        file_size=int(record.file_size),
        storage_url=record.storage_key_or_relative_path,
        download_status="stored",
        metadata_={
            "source": "operator_upload",
            "stored_file_id": str(record.id),
        },
    )
    db.add(asset)
    db.flush()
    return asset


def bind_assets_to_message(
    db: Session, *, message: InboxMessage, asset_ids: list[str] | tuple[str, ...]
) -> list[InboxMediaAsset]:
    """Attach previously staged uploads to the message that carries them.

    Only unbound assets on the same conversation are eligible, so an id from
    another thread — or one already sent — cannot be re-attached.
    """
    from app.services.common import coerce_uuid

    wanted = [value for value in (coerce_uuid(item) for item in asset_ids) if value]
    if not wanted:
        return []

    assets = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.id.in_(wanted))
        .filter(InboxMediaAsset.conversation_id == message.conversation_id)
        .filter(InboxMediaAsset.message_id.is_(None))
        .all()
    )
    for asset in assets:
        asset.message_id = message.id
    db.flush()
    return assets


def pending_outbound_assets(db: Session, conversation_id) -> list[InboxMediaAsset]:
    """Uploads staged for this conversation that no message carries yet."""
    from app.services.common import coerce_uuid

    conversation_uuid = coerce_uuid(conversation_id)
    if conversation_uuid is None:
        return []
    return (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.conversation_id == conversation_uuid)
        .filter(InboxMediaAsset.message_id.is_(None))
        .filter(InboxMediaAsset.direction == "outbound")
        .order_by(InboxMediaAsset.created_at.asc())
        .all()
    )
