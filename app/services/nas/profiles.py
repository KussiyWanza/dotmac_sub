"""
RADIUS Profile Service.

Manages CRUD operations for RADIUS profiles used in NAS device provisioning.
Extracted from the monolithic nas.py service.
"""

import logging
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasVendor,
    RadiusProfile,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class RadiusProfiles(ListResponseMixin):
    """Service class for RADIUS profile operations."""

    @staticmethod
    def get(db: Session, profile_id: str | UUID) -> RadiusProfile:
        """Get a RADIUS profile by ID."""
        profile_id = coerce_uuid(profile_id)
        profile = cast(RadiusProfile | None, db.get(RadiusProfile, profile_id))
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        return profile

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        is_active: bool | None = None,
    ) -> list[RadiusProfile]:
        """List RADIUS profiles with filtering."""
        query = select(RadiusProfile).order_by(RadiusProfile.name)

        if vendor:
            query = query.where(RadiusProfile.vendor == vendor)
        if connection_type:
            query = query.where(RadiusProfile.connection_type == connection_type)
        if is_active is not None:
            query = query.where(RadiusProfile.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def generate_mikrotik_rate_limit(profile: RadiusProfile) -> str:
        """Generate MikroTik rate-limit string from profile settings."""
        if profile.mikrotik_rate_limit:
            return str(profile.mikrotik_rate_limit)

        if not profile.download_speed or not profile.upload_speed:
            return ""

        # MikroTik format: rx-rate[/tx-rate] [rx-burst-rate[/tx-burst-rate]
        # [rx-burst-threshold[/tx-burst-threshold] [rx-burst-time[/tx-burst-time]]]]
        #
        # rx/tx are NAS-perspective, so rx is the subscriber's UPLOAD and tx
        # the subscriber's DOWNLOAD — the convention encoded canonically in
        # app.services.bandwidth.to_subscriber_directions. Every pair below is
        # therefore upload-first.
        download_k = f"{profile.download_speed}k"
        upload_k = f"{profile.upload_speed}k"

        rate_limit = f"{upload_k}/{download_k}"

        if profile.burst_download and profile.burst_upload:
            burst_down = f"{profile.burst_download}k"
            burst_up = f"{profile.burst_upload}k"
            rate_limit += f" {burst_up}/{burst_down}"

            if profile.burst_threshold:
                threshold = f"{profile.burst_threshold}k"
                rate_limit += f" {threshold}/{threshold}"

                if profile.burst_time:
                    rate_limit += f" {profile.burst_time}s/{profile.burst_time}s"

        return rate_limit
