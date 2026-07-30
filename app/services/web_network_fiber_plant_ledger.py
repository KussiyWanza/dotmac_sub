"""Fiber-plant ledger page data — a projection over the fiber SOT owners.

Consolidates the per-asset fiber inventory pages into one archetype-D ledger.
Each asset type is sourced from its OWNER `.list()` read (not the legacy
web_network_fdh/... adapters, which bypass the owners) and normalised into a
uniform {columns, rows} shape so the template renders generically. Status tone
comes from the server-owned fiber presentations. Read-only projection.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import fiber_change_requests
from app.services.network.fiber_services import (
    fiber_splice_closures as closure_owner,
)
from app.services.network.fiber_services import (
    fiber_strands as strand_owner,
)
from app.services.network.splitters import (
    fdh_cabinets as fdh_owner,
)
from app.services.network.splitters import (
    splitters as splitter_owner,
)
from app.services.status_presentation import (
    fiber_change_request_status_presentation,
    fiber_strand_status_presentation,
)

_LIMIT = 500

# facet order + labels
ASSET_TYPES: tuple[tuple[str, str], ...] = (
    ("fdh", "FDH cabinets"),
    ("splitters", "Splitters"),
    ("strands", "Strands"),
    ("closures", "Splice closures"),
    ("change_requests", "Change requests"),
)
_VALID = {key for key, _ in ASSET_TYPES}


def _loc(obj: object) -> str:
    lat = getattr(obj, "latitude", None)
    lng = getattr(obj, "longitude", None)
    if lat is None or lng is None:
        return "—"
    return f"{float(lat):.5f}, {float(lng):.5f}"


def _fdh_rows(db: Session) -> tuple[list, list]:
    columns = [("Name", "name"), ("Code", "code"), ("Location", "location")]
    rows = [
        {"id": str(c.id), "name": c.name, "code": c.code or "—", "location": _loc(c)}
        for c in fdh_owner.list(db, limit=_LIMIT)
    ]
    return columns, rows


def _splitter_rows(db: Session) -> tuple[list, list]:
    columns = [("Name", "name"), ("Ratio", "ratio"), ("Outputs", "ports")]
    rows = [
        {
            "id": str(s.id),
            "name": s.name,
            "ratio": s.splitter_ratio or "—",
            "ports": s.output_ports,
        }
        for s in splitter_owner.list(db, limit=_LIMIT)
    ]
    return columns, rows


def _strand_rows(db: Session) -> tuple[list, list]:
    columns = [("Cable", "cable"), ("Strand", "number"), ("Status", "__status")]
    rows = [
        {
            "id": str(s.id),
            "cable": s.cable_name,
            "number": s.strand_number,
            "status": fiber_strand_status_presentation(s.status),
        }
        for s in strand_owner.list(db, limit=_LIMIT)
    ]
    return columns, rows


def _closure_rows(db: Session) -> tuple[list, list]:
    columns = [("Name", "name"), ("Location", "location")]
    rows = [
        {"id": str(c.id), "name": c.name, "location": _loc(c)}
        for c in closure_owner.list(db, limit=_LIMIT)
    ]
    return columns, rows


def _change_request_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Asset", "asset"),
        ("Operation", "operation"),
        ("Status", "__status"),
        ("Requested", "requested"),
    ]
    rows = []
    for r in fiber_change_requests.list_requests(db):
        operation = getattr(r.operation, "value", r.operation)
        requested = r.created_at.strftime("%b %d, %Y") if r.created_at else "—"
        rows.append(
            {
                "id": str(r.id),
                "asset": str(r.asset_type),
                "operation": str(operation).title(),
                "status": fiber_change_request_status_presentation(r.status),
                "requested": requested,
            }
        )
    return columns, rows


_DISPATCH = {
    "fdh": _fdh_rows,
    "splitters": _splitter_rows,
    "strands": _strand_rows,
    "closures": _closure_rows,
    "change_requests": _change_request_rows,
}


def fiber_plant_ledger_data(db: Session, asset_type: str = "fdh") -> dict:
    """Return the ledger page data for one fiber asset type (from its owner)."""
    from app.services.network import as_built_plant_projection

    asset_type = asset_type if asset_type in _VALID else "fdh"
    columns, rows = _DISPATCH[asset_type](db)
    return {
        # An accepted as-built whose projected cable is still inactive is
        # absent from every ledger facet and every map, so the plant hub counts
        # it explicitly rather than leaving it to be noticed.
        "awaiting_activation_count": (
            as_built_plant_projection.awaiting_activation_count(db)
        ),
        "asset_type": asset_type,
        "asset_label": dict(ASSET_TYPES)[asset_type],
        "asset_types": [{"key": k, "label": lbl} for k, lbl in ASSET_TYPES],
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "detail_base": {
            "fdh": "/admin/network/fdh-cabinets/",
            "splitters": "/admin/network/splitters/",
            "closures": "/admin/network/splice-closures/",
            "change_requests": "/admin/network/fiber-change-requests/",
        }.get(asset_type, ""),
    }
