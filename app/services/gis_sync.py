from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from app.models.gis import GeoLocation, GeoLocationType
from app.models.network_monitoring import PopSite
from app.models.subscriber import Address
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.db_session_adapter import db_session_adapter
from app.services.gis import _point_wkt
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)
LAST_SYNC_RUN_KEY = "last_sync_run"


@dataclass
class SyncResult(ListResponseMixin):
    created: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class GeoImportResult(ListResponseMixin):
    """Outcome of an owner-side coordinate write from an external source."""

    matched: int = 0  # target rows found for the supplied ids
    written: int = 0  # coordinates set or changed
    unchanged: int = 0  # target already carried the supplied coordinates
    missing: int = 0  # supplied ids with no matching row (defensive)


def _address_display_name(address: Address) -> str:
    if address.label:
        return address.label
    parts = [address.address_line1]
    if address.city:
        parts.append(address.city)
    if address.region:
        parts.append(address.region)
    if address.postal_code:
        parts.append(address.postal_code)
    return ", ".join([part for part in parts if part])


def _sync_result_payload(result: SyncResult) -> dict[str, int]:
    return {
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
    }


def record_last_sync_run(
    db: Session,
    *,
    status: str,
    started_at: datetime,
    sync_pops: bool,
    sync_addresses: bool,
    deactivate_missing: bool,
    results: dict[str, object] | None = None,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    finished = finished_at or datetime.now(UTC)
    payload: dict[str, Any] = {
        "status": status,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "finished_at": finished.astimezone(UTC).isoformat(),
        "duration_seconds": round((finished - started_at).total_seconds(), 3),
        "options": {
            "sync_pops": sync_pops,
            "sync_addresses": sync_addresses,
            "deactivate_missing": deactivate_missing,
        },
        "results": results or {},
        "error": error,
    }
    domain_settings_service.gis_settings.upsert_by_key(
        db,
        LAST_SYNC_RUN_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_text=None,
            value_json=payload,
            is_active=True,
        ),
    )
    return payload


def get_last_sync_run(db: Session) -> dict[str, Any] | None:
    try:
        setting = domain_settings_service.gis_settings.get_by_key(db, LAST_SYNC_RUN_KEY)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    if isinstance(setting.value_json, dict):
        return setting.value_json
    return None


class GeoSync(ListResponseMixin):
    @staticmethod
    def sync_sources(
        db: Session,
        background_tasks: BackgroundTasks,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
        background: bool,
    ) -> dict[str, object]:
        if background:
            return GeoSync.queue_sync(
                background_tasks, sync_pops, sync_addresses, deactivate_missing
            )
        started_at = datetime.now(UTC)
        try:
            results = GeoSync.run_sync(
                db, sync_pops, sync_addresses, deactivate_missing
            )
        except Exception as exc:
            db.rollback()
            record_last_sync_run(
                db,
                status="error",
                started_at=started_at,
                sync_pops=sync_pops,
                sync_addresses=sync_addresses,
                deactivate_missing=deactivate_missing,
                error=str(exc),
            )
            raise
        record_last_sync_run(
            db,
            status="success",
            started_at=started_at,
            sync_pops=sync_pops,
            sync_addresses=sync_addresses,
            deactivate_missing=deactivate_missing,
            results=results,
        )
        return results

    @staticmethod
    def run_sync(
        db: Session,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
    ) -> dict[str, object]:
        results: dict[str, object] = {}
        if sync_pops:
            result = GeoSync.sync_pop_sites(db, deactivate_missing=deactivate_missing)
            results["pop_sites"] = _sync_result_payload(result)
        if sync_addresses:
            result = GeoSync.sync_addresses(db, deactivate_missing=deactivate_missing)
            results["addresses"] = _sync_result_payload(result)
        return results

    @staticmethod
    def queue_sync(
        background_tasks: BackgroundTasks,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
    ) -> dict[str, object]:
        def _run_sync() -> None:
            session = db_session_adapter.create_session()
            started_at = datetime.now(UTC)
            try:
                results = GeoSync.run_sync(
                    session,
                    sync_pops=sync_pops,
                    sync_addresses=sync_addresses,
                    deactivate_missing=deactivate_missing,
                )
            except Exception as exc:
                session.rollback()
                record_last_sync_run(
                    session,
                    status="error",
                    started_at=started_at,
                    sync_pops=sync_pops,
                    sync_addresses=sync_addresses,
                    deactivate_missing=deactivate_missing,
                    error=str(exc),
                )
                raise
            else:
                record_last_sync_run(
                    session,
                    status="success",
                    started_at=started_at,
                    sync_pops=sync_pops,
                    sync_addresses=sync_addresses,
                    deactivate_missing=deactivate_missing,
                    results=results,
                )
            finally:
                session.close()

        background_tasks.add_task(_run_sync)
        return {"status": "queued"}

    @staticmethod
    def sync_pop_sites(db: Session, deactivate_missing: bool = False) -> SyncResult:
        result = SyncResult()
        pops = db.query(PopSite).all()
        seen_ids: set[uuid.UUID] = set()
        for pop in pops:
            if pop.latitude is None or pop.longitude is None:
                result.skipped += 1
                continue
            seen_ids.add(pop.id)
            existing = (
                db.query(GeoLocation).filter(GeoLocation.pop_site_id == pop.id).first()
            )
            if existing:
                existing.name = pop.name
                existing.location_type = GeoLocationType.pop
                existing.latitude = pop.latitude
                existing.longitude = pop.longitude
                # Spatial reads filter on geom, so a projection carrying only
                # lat/lon is invisible to the nearby and in-area endpoints.
                existing.geom = _point_wkt(float(pop.latitude), float(pop.longitude))
                existing.is_active = pop.is_active
                result.updated += 1
            else:
                db.add(
                    GeoLocation(
                        name=pop.name,
                        location_type=GeoLocationType.pop,
                        latitude=pop.latitude,
                        longitude=pop.longitude,
                        geom=_point_wkt(float(pop.latitude), float(pop.longitude)),
                        pop_site_id=pop.id,
                        is_active=pop.is_active,
                    )
                )
                result.created += 1
        if deactivate_missing:
            missing_query = db.query(GeoLocation).filter(
                GeoLocation.pop_site_id.isnot(None)
            )
            if seen_ids:
                missing_query = missing_query.filter(
                    GeoLocation.pop_site_id.notin_(seen_ids)
                )
            missing_query.update({"is_active": False}, synchronize_session=False)
        db.commit()
        return result

    @staticmethod
    def sync_addresses(db: Session, deactivate_missing: bool = False) -> SyncResult:
        result = SyncResult()
        addresses = db.query(Address).all()
        seen_ids: set[uuid.UUID] = set()
        for address in addresses:
            if address.latitude is None or address.longitude is None:
                result.skipped += 1
                continue
            seen_ids.add(address.id)
            existing = (
                db.query(GeoLocation)
                .filter(GeoLocation.address_id == address.id)
                .first()
            )
            name = _address_display_name(address)
            if existing:
                existing.name = name
                existing.location_type = GeoLocationType.address
                existing.latitude = address.latitude
                existing.longitude = address.longitude
                existing.is_active = True
                result.updated += 1
            else:
                db.add(
                    GeoLocation(
                        name=name,
                        location_type=GeoLocationType.address,
                        latitude=address.latitude,
                        longitude=address.longitude,
                        address_id=address.id,
                        is_active=True,
                    )
                )
                result.created += 1
        if deactivate_missing:
            missing_query = db.query(GeoLocation).filter(
                GeoLocation.address_id.isnot(None)
            )
            if seen_ids:
                missing_query = missing_query.filter(
                    GeoLocation.address_id.notin_(seen_ids)
                )
            missing_query.update({"is_active": False}, synchronize_session=False)
        db.commit()
        return result

    @staticmethod
    def apply_pop_coordinates(
        db: Session, coordinates: dict[uuid.UUID, tuple[float, float]]
    ) -> GeoImportResult:
        """Write POP-site coordinates from an external source, idempotently.

        ``coordinates`` maps ``pop_sites.id`` to ``(latitude, longitude)``. This
        is the owner-side spatial write for ``gis.spatial_sync``: it sets
        ``latitude``/``longitude``/``geom`` only when they differ, then projects
        the POPs into ``geo_locations`` via :meth:`sync_pop_sites`. Callers
        (e.g. the Splynx backfill runner) own resolving the external source to
        Sub ``pop_sites`` ids; they never write the geometry themselves.
        """
        result = GeoImportResult()
        if not coordinates:
            return result
        pops = db.query(PopSite).filter(PopSite.id.in_(coordinates.keys())).all()
        for pop in pops:
            result.matched += 1
            latitude, longitude = coordinates[pop.id]
            if pop.latitude == latitude and pop.longitude == longitude:
                result.unchanged += 1
                continue
            pop.latitude = latitude
            pop.longitude = longitude
            pop.geom = _point_wkt(latitude, longitude)
            result.written += 1
        result.missing = len(coordinates) - result.matched
        db.commit()
        if result.written:
            GeoSync.sync_pop_sites(db)
        return result

    @staticmethod
    def apply_address_coordinates(
        db: Session, coordinates: dict[uuid.UUID, tuple[float, float]]
    ) -> GeoImportResult:
        """Write subscriber-address coordinates from an external source.

        ``coordinates`` maps ``addresses.id`` to ``(latitude, longitude)``.
        Owner-side spatial write for ``gis.spatial_sync``: sets
        ``latitude``/``longitude``/``geom`` only when they differ, then projects
        the affected addresses into ``geo_locations`` via
        :meth:`sync_addresses`. Callers own resolving the external subscriber to
        its Sub ``addresses`` row.
        """
        result = GeoImportResult()
        if not coordinates:
            return result
        rows = db.query(Address).filter(Address.id.in_(coordinates.keys())).all()
        for address in rows:
            result.matched += 1
            latitude, longitude = coordinates[address.id]
            if address.latitude == latitude and address.longitude == longitude:
                result.unchanged += 1
                continue
            address.latitude = latitude
            address.longitude = longitude
            address.geom = _point_wkt(latitude, longitude)
            result.written += 1
        result.missing = len(coordinates) - result.matched
        db.commit()
        if result.written:
            GeoSync.sync_addresses(db)
        return result


geo_sync = GeoSync()
