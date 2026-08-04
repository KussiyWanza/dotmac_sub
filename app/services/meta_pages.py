"""Meta Pages service for Facebook Page posting and comments.

Provides functionality for:
- Creating posts on Facebook Pages
- Replying to Facebook comments
- Creating Instagram media posts
- Replying to Instagram comments

Uses the Meta Graph API.
"""

import asyncio
import json
import logging
from typing import Any, cast

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.logging import get_logger
from app.models.domain_settings import SettingDomain
from app.models.oauth_token import OAuthToken
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

logger = get_logger(__name__)

_PAGE_POST_SCOPES = {"pages_manage_posts"}
_PAGE_COMMENT_SCOPES = {"pages_read_user_content"}
_PAGE_MESSAGE_SCOPES = {"pages_messaging"}
_IG_MESSAGE_SCOPES = {"instagram_manage_messages"}
_IG_COMMENT_SCOPES = {"instagram_manage_comments"}
_IG_PUBLISH_SCOPES = {"instagram_content_publish"}
_IG_BASIC_SCOPES = {"instagram_basic"}


def _ensure_token_scopes(
    token: OAuthToken | None, required_scopes: set[str], context: str
) -> None:
    if not token or not token.scopes:
        return
    if not isinstance(token.scopes, (list, tuple, set)):
        return
    granted = {str(scope) for scope in token.scopes}
    missing = required_scopes - granted
    if missing:
        raise ValueError(
            f"Missing required Meta permissions for {context}: {sorted(missing)}"
        )


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    data: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 1,
) -> httpx.Response:
    retries = 0
    while True:
        response = await client.request(
            method, url, data=data, params=params, timeout=timeout
        )
        if response.status_code in {429} or response.status_code >= 500:
            if retries >= max_retries:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = 1.0
            if retry_after:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = 1.0
            await asyncio.sleep(delay)
            retries += 1
            continue
        return response


def _request_with_retry_sync(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    data: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 1,
) -> httpx.Response:
    retries = 0
    while True:
        response = client.request(
            method, url, data=data, params=params, timeout=timeout
        )
        if response.status_code == 429 or response.status_code >= 500:
            if retries >= max_retries:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = 1.0
            if retry_after:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = 1.0
            import time

            time.sleep(delay)
            retries += 1
            continue
        return response


def _get_meta_graph_base_url(db: Session) -> str:
    version = resolve_value(db, SettingDomain.comms, "meta_graph_api_version")
    if not version:
        version = settings.meta_graph_api_version
    return f"https://graph.facebook.com/{version}"


def _get_page_token_record(db: Session, page_id: str) -> OAuthToken | None:
    """Get the access token for a specific Facebook Page."""
    return cast(
        OAuthToken | None,
        (
            db.query(OAuthToken)
            .filter(OAuthToken.provider == "meta")
            .filter(OAuthToken.account_type == "page")
            .filter(OAuthToken.external_account_id == page_id)
            .filter(OAuthToken.is_active.is_(True))
            .first()
        ),
    )


def _get_page_token(db: Session, page_id: str) -> str | None:
    token = _get_page_token_record(db, page_id)
    return token.access_token if token else None


def _get_instagram_token_record(db: Session, ig_account_id: str) -> OAuthToken | None:
    """Get the access token for a specific Instagram Business account.

    Instagram uses the parent Page's token for API access.
    """
    return cast(
        OAuthToken | None,
        (
            db.query(OAuthToken)
            .filter(OAuthToken.provider == "meta")
            .filter(OAuthToken.account_type == "instagram_business")
            .filter(OAuthToken.external_account_id == ig_account_id)
            .filter(OAuthToken.is_active.is_(True))
            .first()
        ),
    )


def _get_instagram_token(db: Session, ig_account_id: str) -> str | None:
    token = _get_instagram_token_record(db, ig_account_id)
    return token.access_token if token else None


# ---------------------------------------------------------------------------
# Facebook Page Posts
# ---------------------------------------------------------------------------


async def create_page_post(
    db: Session,
    page_id: str,
    message: str,
    link: str | None = None,
    published: bool = True,
) -> dict[str, Any]:
    """Create a post on a Facebook Page.

    Args:
        db: Database session
        page_id: Facebook Page ID
        message: Post text content
        link: Optional URL to include in the post
        published: Whether to publish immediately (False = draft)

    Returns:
        Dict with 'id' of the created post on success

    Raises:
        ValueError: If page token not found
        httpx.HTTPStatusError: If API call fails
    """
    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_POST_SCOPES, "facebook_page_post")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{page_id}/feed"
    payload: dict[str, Any] = {
        "message": message,
        "access_token": access_token,
        "published": str(published).lower(),
    }

    if link:
        payload["link"] = link

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "POST", url, data=payload, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    logger.info(
        "fb_page_post_created page_id=%s post_id=%s",
        page_id,
        result.get("id"),
    )

    return result


async def create_page_photo_post(
    db: Session,
    page_id: str,
    photo_url: str,
    caption: str | None = None,
    published: bool = True,
) -> dict[str, Any]:
    """Create a photo post on a Facebook Page.

    Args:
        db: Database session
        page_id: Facebook Page ID
        photo_url: URL of the photo to post
        caption: Optional caption for the photo
        published: Whether to publish immediately

    Returns:
        Dict with 'id' and 'post_id' on success
    """
    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_POST_SCOPES, "facebook_page_photo_post")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{page_id}/photos"
    payload: dict[str, Any] = {
        "url": photo_url,
        "access_token": access_token,
        "published": str(published).lower(),
    }

    if caption:
        payload["caption"] = caption

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "POST", url, data=payload, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    logger.info(
        "fb_page_photo_posted page_id=%s photo_id=%s",
        page_id,
        result.get("id"),
    )

    return result


# ---------------------------------------------------------------------------
# Facebook Comments
# ---------------------------------------------------------------------------


async def reply_to_comment(
    db: Session,
    page_id: str,
    comment_id: str,
    message: str,
) -> dict[str, Any]:
    """Reply to a comment on a Facebook Page post.

    Args:
        db: Database session
        page_id: Facebook Page ID (for token lookup)
        comment_id: ID of the comment to reply to
        message: Reply text

    Returns:
        Dict with 'id' of the created reply
    """
    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_POST_SCOPES, "facebook_comment_reply")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{comment_id}/comments"
    payload = {
        "message": message,
        "access_token": access_token,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "POST", url, data=payload, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    logger.info(
        "fb_comment_reply_created page_id=%s parent_comment=%s reply_id=%s",
        page_id,
        comment_id,
        result.get("id"),
    )

    return result


def reply_to_comment_sync(
    db: Session,
    page_id: str,
    comment_id: str,
    message: str,
) -> dict[str, Any]:
    """Synchronous Meta adapter for transaction-owned inbox comment replies."""

    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_POST_SCOPES, "facebook_comment_reply")
    url = f"{_get_meta_graph_base_url(db).rstrip('/')}/{comment_id}/comments"
    with httpx.Client(timeout=30.0) as client:
        response = _request_with_retry_sync(
            client,
            "POST",
            url,
            data={"message": message, "access_token": token.access_token},
            timeout=30.0,
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())
    logger.info(
        "fb_comment_reply_created page_id=%s parent_comment=%s reply_id=%s",
        page_id,
        comment_id,
        result.get("id"),
    )
    return result


def send_facebook_message_sync(
    db: Session,
    *,
    page_id: str,
    recipient_id: str,
    message: str,
) -> dict[str, Any]:
    """Send one bounded Page Messenger text through the Meta adapter."""

    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_MESSAGE_SCOPES, "facebook_messenger_reply")
    url = f"{_get_meta_graph_base_url(db).rstrip('/')}/{page_id}/messages"
    with httpx.Client(timeout=30.0) as client:
        response = _request_with_retry_sync(
            client,
            "POST",
            url,
            data={
                "recipient": json.dumps({"id": recipient_id}),
                "message": json.dumps({"text": message}),
                "messaging_type": "RESPONSE",
                "access_token": token.access_token,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())
    logger.info(
        "fb_messenger_reply_created page_id=%s message_id=%s",
        page_id,
        result.get("message_id"),
    )
    return result


async def get_post_comments(
    db: Session,
    page_id: str,
    post_id: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Get comments on a Facebook Page post.

    Args:
        db: Database session
        page_id: Facebook Page ID
        post_id: ID of the post
        limit: Maximum number of comments to retrieve

    Returns:
        List of comment objects with id, message, from, created_time
    """
    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_COMMENT_SCOPES, "facebook_comment_list")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{post_id}/comments"
    params = {
        "access_token": access_token,
        "fields": "id,message,from,created_time,comment_count",
        "limit": limit,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "GET", url, params=params, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    return cast(list[dict[str, Any]], result.get("data", []))


async def get_page_posts(
    db: Session,
    page_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get recent posts for a Facebook Page."""
    token = _get_page_token_record(db, page_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Page {page_id}")
    _ensure_token_scopes(token, _PAGE_COMMENT_SCOPES, "facebook_page_posts")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{page_id}/posts"
    params = {
        "access_token": access_token,
        "fields": "id,message,created_time,permalink_url",
        "limit": limit,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "GET", url, params=params, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    return cast(list[dict[str, Any]], result.get("data", []))


# ---------------------------------------------------------------------------
# Instagram Media Posts
# ---------------------------------------------------------------------------


async def create_instagram_image_post(
    db: Session,
    ig_account_id: str,
    image_url: str,
    caption: str | None = None,
) -> dict[str, Any]:
    """Create an image post on Instagram.

    Instagram posting is a two-step process:
    1. Create a media container with the image
    2. Publish the container

    Args:
        db: Database session
        ig_account_id: Instagram Business Account ID
        image_url: Public URL of the image (must be accessible to Instagram)
        caption: Optional post caption (can include hashtags)

    Returns:
        Dict with 'id' of the published media
    """
    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_PUBLISH_SCOPES, "instagram_image_post")
    access_token = token.access_token

    # Step 1: Create media container
    base_url = _get_meta_graph_base_url(db)
    container_url = f"{base_url.rstrip('/')}/{ig_account_id}/media"
    container_payload: dict[str, Any] = {
        "image_url": image_url,
        "access_token": access_token,
    }
    if caption:
        container_payload["caption"] = caption

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Create container
        response = await _request_with_retry(
            client,
            "POST",
            container_url,
            data=container_payload,
            timeout=60.0,
        )
        response.raise_for_status()
        container_result = cast(dict[str, Any], response.json())
        container_id = container_result.get("id")

        if not container_id:
            raise ValueError("Failed to create Instagram media container")

        # Step 2: Publish the container
        publish_url = f"{base_url.rstrip('/')}/{ig_account_id}/media_publish"
        publish_payload = {
            "creation_id": container_id,
            "access_token": access_token,
        }

        response = await _request_with_retry(
            client,
            "POST",
            publish_url,
            data=publish_payload,
            timeout=60.0,
        )
        response.raise_for_status()
        publish_result = cast(dict[str, Any], response.json())

    logger.info(
        "ig_image_posted account_id=%s media_id=%s",
        ig_account_id,
        publish_result.get("id"),
    )

    return publish_result


async def create_instagram_carousel_post(
    db: Session,
    ig_account_id: str,
    media_urls: list[str],
    caption: str | None = None,
) -> dict[str, Any]:
    """Create a carousel (multi-image) post on Instagram.

    Args:
        db: Database session
        ig_account_id: Instagram Business Account ID
        media_urls: List of public image URLs (2-10 images)
        caption: Optional caption for the carousel

    Returns:
        Dict with 'id' of the published carousel
    """
    if len(media_urls) < 2 or len(media_urls) > 10:
        raise ValueError("Carousel must have between 2 and 10 images")

    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_PUBLISH_SCOPES, "instagram_carousel_post")
    access_token = token.access_token

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Step 1: Create containers for each image
        child_container_ids = []
        base_url = _get_meta_graph_base_url(db)
        for image_url in media_urls:
            container_url = f"{base_url.rstrip('/')}/{ig_account_id}/media"
            container_payload = {
                "image_url": image_url,
                "is_carousel_item": "true",
                "access_token": access_token,
            }
            response = await _request_with_retry(
                client,
                "POST",
                container_url,
                data=container_payload,
                timeout=120.0,
            )
            response.raise_for_status()
            container_result = cast(dict[str, Any], response.json())
            container_id = container_result.get("id")
            if not container_id:
                raise ValueError("Failed to create Instagram carousel container")
            child_container_ids.append(container_id)

        # Step 2: Create carousel container
        carousel_url = f"{base_url.rstrip('/')}/{ig_account_id}/media"
        carousel_payload: dict[str, Any] = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_container_ids),
            "access_token": access_token,
        }
        if caption:
            carousel_payload["caption"] = caption

        response = await _request_with_retry(
            client,
            "POST",
            carousel_url,
            data=carousel_payload,
            timeout=120.0,
        )
        response.raise_for_status()
        carousel_result = cast(dict[str, Any], response.json())
        carousel_id = carousel_result.get("id")
        if not carousel_id:
            raise ValueError("Failed to create Instagram carousel")

        # Step 3: Publish carousel
        publish_url = f"{base_url.rstrip('/')}/{ig_account_id}/media_publish"
        publish_payload = {
            "creation_id": carousel_id,
            "access_token": access_token,
        }

        response = await _request_with_retry(
            client,
            "POST",
            publish_url,
            data=publish_payload,
            timeout=120.0,
        )
        response.raise_for_status()
        publish_result = cast(dict[str, Any], response.json())

    logger.info(
        "ig_carousel_posted account_id=%s media_id=%s images=%d",
        ig_account_id,
        publish_result.get("id"),
        len(media_urls),
    )

    return publish_result


# ---------------------------------------------------------------------------
# Instagram Comments
# ---------------------------------------------------------------------------


async def reply_to_instagram_comment(
    db: Session,
    ig_account_id: str,
    comment_id: str,
    message: str,
) -> dict[str, Any]:
    """Reply to a comment on an Instagram post.

    Args:
        db: Database session
        ig_account_id: Instagram Business Account ID
        comment_id: ID of the comment to reply to
        message: Reply text

    Returns:
        Dict with 'id' of the created reply
    """
    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_COMMENT_SCOPES, "instagram_comment_reply")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{comment_id}/replies"
    payload = {
        "message": message,
        "access_token": access_token,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "POST", url, data=payload, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    logger.info(
        "ig_comment_reply_created account_id=%s parent_comment=%s reply_id=%s",
        ig_account_id,
        comment_id,
        result.get("id"),
    )

    return result


def reply_to_instagram_comment_sync(
    db: Session,
    ig_account_id: str,
    comment_id: str,
    message: str,
) -> dict[str, Any]:
    """Synchronous Meta adapter for transaction-owned inbox comment replies."""

    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_COMMENT_SCOPES, "instagram_comment_reply")
    url = f"{_get_meta_graph_base_url(db).rstrip('/')}/{comment_id}/replies"
    with httpx.Client(timeout=30.0) as client:
        response = _request_with_retry_sync(
            client,
            "POST",
            url,
            data={"message": message, "access_token": token.access_token},
            timeout=30.0,
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())
    logger.info(
        "ig_comment_reply_created account_id=%s parent_comment=%s reply_id=%s",
        ig_account_id,
        comment_id,
        result.get("id"),
    )
    return result


def send_instagram_message_sync(
    db: Session,
    *,
    ig_account_id: str,
    recipient_id: str,
    message: str,
) -> dict[str, Any]:
    """Send one bounded Instagram Direct text through the Meta adapter."""

    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_MESSAGE_SCOPES, "instagram_direct_reply")
    url = f"{_get_meta_graph_base_url(db).rstrip('/')}/{ig_account_id}/messages"
    with httpx.Client(timeout=30.0) as client:
        response = _request_with_retry_sync(
            client,
            "POST",
            url,
            data={
                "recipient": json.dumps({"id": recipient_id}),
                "message": json.dumps({"text": message}),
                "access_token": token.access_token,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())
    logger.info(
        "ig_direct_reply_created account_id=%s message_id=%s",
        ig_account_id,
        result.get("message_id"),
    )
    return result


async def get_instagram_media_comments(
    db: Session,
    ig_account_id: str,
    media_id: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Get comments on an Instagram media post.

    Args:
        db: Database session
        ig_account_id: Instagram Business Account ID
        media_id: ID of the media post
        limit: Maximum number of comments to retrieve

    Returns:
        List of comment objects
    """
    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_COMMENT_SCOPES, "instagram_comment_list")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{media_id}/comments"
    params = {
        "access_token": access_token,
        "fields": "id,text,username,timestamp,replies{id,text,username,timestamp}",
        "limit": limit,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "GET", url, params=params, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    return cast(list[dict[str, Any]], result.get("data", []))


async def get_instagram_media(
    db: Session,
    ig_account_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get recent media for an Instagram Business account."""
    token = _get_instagram_token_record(db, ig_account_id)
    if not token or not token.access_token:
        raise ValueError(f"No active token found for Instagram account {ig_account_id}")
    _ensure_token_scopes(token, _IG_BASIC_SCOPES, "instagram_media_list")
    access_token = token.access_token

    base_url = _get_meta_graph_base_url(db)
    url = f"{base_url.rstrip('/')}/{ig_account_id}/media"
    params = {
        "access_token": access_token,
        "fields": "id,caption,media_type,permalink,timestamp",
        "limit": limit,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _request_with_retry(
            client, "GET", url, params=params, timeout=30.0
        )
        response.raise_for_status()
        result = cast(dict[str, Any], response.json())

    return cast(list[dict[str, Any]], result.get("data", []))


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def get_connected_pages(db: Session) -> list[dict[str, Any]]:
    """Get all connected Facebook Pages.

    Returns:
        List of dicts with page_id, name, and token status
    """
    tokens = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.is_active.is_(True))
        .all()
    )

    pages = []
    for token in tokens:
        metadata = token.metadata_ or {}
        pages.append(
            {
                "page_id": token.external_account_id,
                "name": token.external_account_name,
                "category": metadata.get("category"),
                "picture": metadata.get("picture"),
                "token_expires_at": token.token_expires_at,
                "needs_refresh": token.should_refresh(),
            }
        )

    return pages


def get_connected_instagram_accounts(db: Session) -> list[dict[str, Any]]:
    """Get all connected Instagram Business accounts.

    Returns:
        List of dicts with account_id, username, and token status
    """
    tokens = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
        .filter(OAuthToken.is_active.is_(True))
        .all()
    )

    accounts = []
    for token in tokens:
        metadata = token.metadata_ or {}
        accounts.append(
            {
                "account_id": token.external_account_id,
                "username": token.external_account_name,
                "profile_picture_url": metadata.get("profile_picture_url"),
                "token_expires_at": token.token_expires_at,
                "needs_refresh": token.should_refresh(),
            }
        )

    return accounts
