"""Cross-domain read owner joining native network and NAS inventory.

Lives above the standalone network package because NAS is catalog-owned.
This projection never mutates either inventory or subscription state.
"""

from uuid import UUID

from sqlalchemy import String, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.catalog import NasDevice
from app.models.network import FdhCabinet, OLTDevice, PonPort
from app.models.network_monitoring import DeviceType, NetworkDevice, PopSite
from app.schemas.infrastructure import (
    InfrastructureOption,
    InfrastructureOptions,
    InfrastructureReference,
    InfrastructureSearch,
    InfrastructureType,
)

InfrastructureSelect = (
    Select[tuple[UUID, str, str | None]] | Select[tuple[UUID, str, str]]
)


def _options(
    kind: InfrastructureType, *, term: str | None = None, active_only: bool = True
) -> InfrastructureSelect:
    empty = literal(None, type_=String)
    pattern = f"%{term}%" if term is not None else None
    if kind is InfrastructureType.nas:
        query: InfrastructureSelect = select(
            NasDevice.id, NasDevice.name, NasDevice.nas_ip
        )
        if active_only:
            query = query.where(NasDevice.is_active.is_(True))
        if pattern is not None:
            query = query.where(
                or_(NasDevice.name.ilike(pattern), NasDevice.nas_ip.ilike(pattern))
            )
        return query
    if kind in {InfrastructureType.location, InfrastructureType.base_station}:
        query = select(PopSite.id, PopSite.name, empty)
        if kind is InfrastructureType.base_station:
            query = query.where(PopSite.zabbix_group_id.is_not(None))
        if pattern is not None:
            query = query.where(PopSite.name.ilike(pattern))
        return query
    if kind is InfrastructureType.access_point:
        query = (
            select(NetworkDevice.id, NetworkDevice.name, PopSite.name)
            .outerjoin(PopSite, PopSite.id == NetworkDevice.pop_site_id)
            .where(NetworkDevice.device_type == DeviceType.access_point)
        )
        if active_only:
            query = query.where(NetworkDevice.is_active.is_(True))
        if pattern is not None:
            query = query.where(
                or_(
                    NetworkDevice.name.ilike(pattern),
                    NetworkDevice.hostname.ilike(pattern),
                    NetworkDevice.mgmt_ip.ilike(pattern),
                    PopSite.name.ilike(pattern),
                )
            )
        return query
    if kind is InfrastructureType.olt:
        query = select(OLTDevice.id, OLTDevice.name, OLTDevice.hostname)
        if active_only:
            query = query.where(OLTDevice.is_active.is_(True))
        if pattern is not None:
            query = query.where(
                or_(
                    OLTDevice.name.ilike(pattern),
                    OLTDevice.hostname.ilike(pattern),
                    OLTDevice.mgmt_ip.ilike(pattern),
                )
            )
        return query
    if kind is InfrastructureType.pon_port:
        query = select(PonPort.id, PonPort.name, OLTDevice.name).join(
            OLTDevice, OLTDevice.id == PonPort.olt_id
        )
        if active_only:
            query = query.where(
                PonPort.is_active.is_(True), OLTDevice.is_active.is_(True)
            )
        if pattern is not None:
            query = query.where(
                or_(PonPort.name.ilike(pattern), OLTDevice.name.ilike(pattern))
            )
        return query
    query = select(FdhCabinet.id, FdhCabinet.name, FdhCabinet.code)
    if active_only:
        query = query.where(FdhCabinet.is_active.is_(True))
    if pattern is not None:
        query = query.where(
            or_(FdhCabinet.name.ilike(pattern), FdhCabinet.code.ilike(pattern))
        )
    return query


def search(db: Session, *, query: InfrastructureSearch) -> InfrastructureOptions:
    term = query.query.strip()
    if len(term) < 2:
        return InfrastructureOptions(results=())
    options = _options(query.type, term=term).subquery()
    rows = db.execute(
        select(options).order_by(options.c[1], options.c[0]).limit(query.limit)
    )
    return InfrastructureOptions(
        results=tuple(
            InfrastructureOption(id=row[0], label=row[1], context=row[2])
            for row in rows
        )
    )


def resolve(
    db: Session, *, reference: InfrastructureReference, active_only: bool = True
) -> InfrastructureOption | None:
    options = _options(reference.type, active_only=active_only).subquery()
    row = db.execute(select(options).where(options.c[0] == reference.id)).one_or_none()
    return (
        InfrastructureOption(id=row[0], label=row[1], context=row[2]) if row else None
    )
