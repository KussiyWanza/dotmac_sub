from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models.stored_file import StoredFile
from app.models.team_inbox import InboxMediaAsset, InboxMessage
from app.services.file_storage import ObjectNotFoundError, StreamResult, file_uploads

REMOTE_MEDIA_PROVIDERS = frozenset(
    {
        "facebook",
        "instagram",
        "meta",
        "whatsapp",
    }
)
REMOTE_MEDIA_HOSTS = frozenset(
    {
        "graph.facebook.com",
        "lookaside.facebook.com",
        "lookaside.fbsbx.com",
    }
)
REMOTE_MEDIA_HOST_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".fbsbx.com",
)


@dataclass(frozen=True, slots=True)
class InboxMediaContent:
    asset_id: UUID
    file_name: str
    content_type: str
    stream: StreamResult


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


def media_content_url(asset_id: object) -> str:
    return f"/admin/inbox/media/{asset_id}/content"


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


@dataclass(frozen=True)
class InboxDeliveryAttachment:
    asset_id: UUID
    filename: str
    content_type: str
    content: bytes
    asset_type: str


class MediaContentError(RuntimeError):
    """Media exists, but its content cannot be read safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _stored_file_id(asset: InboxMediaAsset) -> UUID | None:
    from app.services.common import coerce_uuid

    metadata = asset.metadata_ or {}
    if not isinstance(metadata, dict):
        return None
    return coerce_uuid(metadata.get("stored_file_id"))


def _filename(asset: InboxMediaAsset) -> str:
    suffix = "jpg" if (asset.mime_type or "").lower() == "image/jpeg" else "bin"
    return asset.file_name or f"inbox-media-{asset.id}.{suffix}"


def _content_type(asset: InboxMediaAsset) -> str:
    return asset.mime_type or "application/octet-stream"


def can_stream_remote_media(asset: InboxMediaAsset) -> bool:
    source_url = str(asset.source_url or "").strip()
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    provider = str(asset.provider or "").strip().lower()
    allowed_host = host in REMOTE_MEDIA_HOSTS or any(
        host.endswith(suffix) for suffix in REMOTE_MEDIA_HOST_SUFFIXES
    )
    return (
        parsed.scheme == "https" and provider in REMOTE_MEDIA_PROVIDERS and allowed_host
    )


def _graph_version(config: Mapping[str, Any]) -> str:
    version = str(config.get("graph_version") or "v21.0").strip() or "v21.0"
    return version if version.startswith("v") else f"v{version}"


def _whatsapp_media_content(db: Session, asset: InboxMediaAsset) -> StreamResult:
    from app.services.integrations import whatsapp_capability

    media_id = str(asset.provider_media_id or "").strip()
    if not media_id:
        raise MediaContentError("Media content is not available.")
    context = whatsapp_capability.execution_context(
        db,
        capability_id=whatsapp_capability.WHATSAPP_RECEIVE_CAPABILITY,
    )
    token = str(context.secret_material.get("service_credentials") or "").strip()
    if not token:
        raise MediaContentError("WhatsApp media credentials are not configured.")
    base = f"https://graph.facebook.com/{_graph_version(context.config)}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        metadata_response = httpx.get(
            f"{base}/{media_id}",
            params={"fields": "url,mime_type,file_size"},
            headers=headers,
            timeout=10,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        media_url = str(metadata.get("url") or "").strip()
        if not media_url:
            raise MediaContentError("WhatsApp media URL is unavailable.")
        media_response = httpx.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
    except MediaContentError:
        raise
    except Exception as exc:
        raise MediaContentError("WhatsApp media could not be downloaded.") from exc
    content_type = str(
        media_response.headers.get("content-type") or asset.mime_type or ""
    ).split(";")[0]
    return StreamResult(
        chunks=iter([media_response.content]),
        content_type=content_type or _content_type(asset),
        content_length=len(media_response.content),
    )


def _remote_media_content(asset: InboxMediaAsset) -> StreamResult:
    source_url = str(asset.source_url or "").strip()
    if not can_stream_remote_media(asset):
        raise MediaContentError("Media content is not available.")
    try:
        response = httpx.get(source_url, timeout=20, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MediaContentError("Remote media content is not available.") from exc
    content_type = str(
        response.headers.get("content-type") or asset.mime_type or ""
    ).split(";")[0]
    return StreamResult(
        chunks=iter([response.content]),
        content_type=content_type or _content_type(asset),
        content_length=len(response.content),
    )


def stream_asset_content(db: Session, asset_id: str | UUID) -> InboxMediaContent:
    from app.services.common import coerce_uuid

    asset_uuid = coerce_uuid(asset_id)
    asset = db.get(InboxMediaAsset, asset_uuid) if asset_uuid else None
    if asset is None:
        raise MediaContentError("Media not found.")
    stored_file_id = _stored_file_id(asset)
    stream: StreamResult
    if stored_file_id is not None:
        stored_file = db.get(StoredFile, stored_file_id)
        if stored_file is None or stored_file.is_deleted:
            raise MediaContentError("Media content is not available.")
        try:
            stream = file_uploads.stream_file(stored_file)
        except ObjectNotFoundError as exc:
            raise MediaContentError("Media content is not available.") from exc
    elif (
        asset.channel_type == "whatsapp"
        and asset.direction == "inbound"
        and asset.provider_media_id
    ):
        stream = _whatsapp_media_content(db, asset)
    elif asset.source_url:
        stream = _remote_media_content(asset)
    else:
        raise MediaContentError("Media content is not available.")

    content_type = (
        str(stream.content_type or _content_type(asset))
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    return InboxMediaContent(
        asset_id=asset.id,
        file_name=_filename(asset),
        content_type=content_type,
        stream=stream,
    )


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
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "video/mp4",
        "video/quicktime",
        "video/webm",
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
        uploaded_by=None,
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
            "uploaded_by_person_id": str(uploaded_by) if uploaded_by else None,
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
    if assets:
        message_metadata = dict(message.metadata_ or {})
        attachment_rows = list(message_metadata.get("attachments") or [])
        for asset in assets:
            attachment_rows.append(
                {
                    "id": str(asset.id),
                    "type": asset.asset_type,
                    "file_name": asset.file_name,
                    "filename": asset.file_name,
                    "mime_type": asset.mime_type,
                    "file_size": asset.file_size,
                    "url": media_content_url(asset.id),
                    "download_status": asset.download_status,
                }
            )
        message_metadata["attachments"] = attachment_rows
        message_metadata["inbox_attachment_ids"] = [str(asset.id) for asset in assets]
        message.metadata_ = message_metadata
    db.flush()
    return assets


def validate_staged_asset_ids(
    db: Session,
    *,
    conversation_id: UUID,
    asset_ids: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve staged uploads before an outbound intent can reference them."""
    from app.services.common import coerce_uuid

    requested = tuple(
        value for value in (coerce_uuid(item) for item in asset_ids) if value
    )
    if len(requested) != len(asset_ids) or len(set(requested)) != len(requested):
        raise MediaUploadError("One or more selected attachments are invalid.")
    if not requested:
        return ()
    rows = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.id.in_(requested))
        .filter(InboxMediaAsset.conversation_id == conversation_id)
        .filter(InboxMediaAsset.direction == "outbound")
        .filter(InboxMediaAsset.message_id.is_(None))
        .all()
    )
    found = {row.id for row in rows}
    if found != set(requested):
        raise MediaUploadError(
            "One or more attachments are unavailable. Remove them and upload again."
        )
    return tuple(str(asset_id) for asset_id in requested)


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


def resolve_delivery_attachments(
    db: Session, asset_ids: list[str] | tuple[str, ...]
) -> tuple[InboxDeliveryAttachment, ...]:
    from app.services.common import coerce_uuid

    wanted = [value for value in (coerce_uuid(item) for item in asset_ids) if value]
    if not wanted:
        return ()
    rows = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.id.in_(wanted))
        .filter(InboxMediaAsset.direction == "outbound")
        .order_by(InboxMediaAsset.created_at.asc())
        .all()
    )
    resolved: list[InboxDeliveryAttachment] = []
    for asset in rows:
        media_content = stream_asset_content(db, asset.id)
        resolved.append(
            InboxDeliveryAttachment(
                asset_id=asset.id,
                filename=media_content.file_name,
                content_type=media_content.content_type,
                content=b"".join(media_content.stream.chunks),
                asset_type=asset.asset_type
                or _outbound_asset_type(_content_type(asset)),
            )
        )
    return tuple(resolved)
