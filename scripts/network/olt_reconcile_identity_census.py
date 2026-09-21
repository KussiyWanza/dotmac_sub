"""Pre-merge census for the OLT reconciliation identity/service-port fixes.

Astra audit findings (Bug 1: identity never bound; Bug 2: service ports
compared by index alone) are fixed by binding a found OLT registration's
fsp/onu_id against the desired ``fsp``/``olt_ont_id`` target before treating
it as an observation, and by comparing service ports on vlan_id/gem_index/
ont_id/fsp rather than index alone. Both fixes change reconciler BEHAVIOR
for any ONT that is currently relying on the old, buggy substitutions to
"work" — so before this ships, we need to know how many ONTs are affected
and in what way.

Four counts, each naming a distinct risk:

* ``null_board_or_port`` — ``OntUnit.board``/``.port`` missing, so
  ``desired.fsp`` resolves to ``""``. Under the fix, every such ONT reports
  ``OLT_IDENTITY_UNRESOLVED`` and stops receiving OLT writes until an owner
  fills in board/port.
* ``unparseable_external_id`` — ``parse_ont_id_on_olt(external_id)`` returns
  ``None``. Before the fix this silently became ONT-ID 0 (a real, possibly
  IN-USE id on the OLT); under the fix it is ``OLT_IDENTITY_UNRESOLVED`` —
  the same outcome as the row above, from the other required field.
* ``observed_vlan_gem_mismatch`` — the last-persisted OLT observation has a
  service port sitting at the desired mgmt/wan index with a DIFFERENT
  vlan_id/gem_index. Before the fix this was silently accepted as "already
  correct" (index-only comparison); under the fix it becomes unrepairable
  drift that blocks convergence until a human resolves it (no auto
  delete+recreate).
* ``null_service_port_index`` — ``mgmt_service_port_index``/
  ``wan_service_port_index`` unresolved. Already gated by the existing
  SERVICE_PORT_INDEX_UNALLOCATED refusal (Bug 4, fixed on main before this
  branch); counted here for completeness since it interacts with the same
  service-port code path this PR touches.

This script does not repair anything. It is read-only by construction — one
REPEATABLE READ, READ ONLY snapshot, rolled back rather than committed — and
performs no device I/O. Exit 0 means every active ONT's OLT identity and
persisted service-port evidence is clean; exit 1 means at least one of the
four counts is non-zero and lists the affected rows so an operator can
triage before/after this PR merges.

Usage::

    poetry run python scripts/network/olt_reconcile_identity_census.py

Modeled on ``scripts/network/pon_port_identity_census.py`` — same
read-only-snapshot pattern, same read-report-rollback shape.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import read_only_snapshot_session
from app.models.network import OntUnit
from app.models.ont_observation import OntObservation
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.reconcile.adapters import _FSP_RE
from app.services.network.serial_utils import parse_ont_id_on_olt

EXIT_CLEAN = 0
EXIT_DIRTY = 1


@dataclass
class Row:
    ont_unit_id: str
    serial_number: str
    board: str | None
    port: str | None
    external_id: str | None
    issues: list[str] = dc_field(default_factory=list)


def _fsp(board: str | None, port: str | None) -> str:
    """Exactly ``adapters._fsp_from_ont`` — imported ``_FSP_RE`` rather than a
    local copy, so this census can never drift from what production actually
    validates. A truthy board+port is not sufficient: ``board="0"``,
    ``port="1"`` joins to ``"0/1"``, which is only two segments and fails
    ``_FSP_RE`` in production (resolves to ``fsp=""``) even though a naive
    "both fields are set" check would call it clean.
    """
    board = (board or "").strip()
    port = (port or "").strip()
    if not board or not port:
        return ""
    fsp = f"{board}/{port}"
    return fsp if _FSP_RE.fullmatch(fsp) else ""


def _int_or_none(value: object) -> int | None:
    """Exactly ``adapters._int_or_none`` — coerce a raw config value to
    ``int``, tolerating ``None``/empty/non-numeric strings. The adapter runs
    this BEFORE ``desired.wan_gem_index or 1`` in the planner; skipping this
    step and doing ``values.get("wan_gem_index") or 1`` directly on the raw
    value computes gem 0 for a stored ``"0"`` string (truthy, unlike the
    int ``0``) instead of production's 1, and raises uncaught on a
    non-numeric string instead of degrading to 1 the way the adapter does.
    """
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _sp_int(sp: object, *names: str) -> int | None:
    if not isinstance(sp, dict):
        return None
    for name in names:
        value = sp.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _observed_mismatch_at_index(
    observed_ports: list[dict] | None,
    *,
    index: int | None,
    vlan: int | None,
    gem_index: int,
    ont_id: int | None,
    fsp: str,
) -> bool:
    """Whether a persisted observation shows drift at the desired ``index``.

    Mirrors ``reconcile.planner._plan_service_ports``'s per-port loop, not
    just ``_service_port_matches`` in isolation — the planner never even
    calls ``_service_port_matches`` when the desired vlan is unset for an
    allocated index; it routes straight to ``mismatched_at_index`` (see
    ``planner.py``'s mgmt/wan branches: ``if desired.mgmt_vlan is not None
    and _service_port_matches(...): continue`` / ``mismatched_at_index["mgmt"]
    = sp``). So an ALLOCATED index with a persisted port already sitting
    there, but no desired vlan to compare it against, is unrepairable drift
    in production — NOT a clean row, and NOT skippable the way "no index at
    all" is. Below that point, a real desired vlan/gem is compared with a
    plain ``!=``: ``None != 201`` is ``True``, so a persisted port with
    unpopulated vlan_id/gem_index is also drift, never "no evidence, skip".
    Not imported directly from ``reconcile.planner`` so this script keeps
    working independent of that module's internals; kept in sync by design
    intent, not by import — re-diff against ``planner.py`` on future changes.
    """
    if index is None or not observed_ports:
        return False
    for sp in observed_ports:
        if _sp_int(sp, "index") != index:
            continue
        if vlan is None:
            # A port already occupies this allocated index, but the desired
            # vlan itself is unresolved — the planner records this directly
            # as unrepairable drift without ever reaching a value
            # comparison. Not "no evidence to compare", not clean.
            return True
        observed_vlan = _sp_int(sp, "vlan_id", "vlan")
        observed_gem = _sp_int(sp, "gem_index", "gem")
        if observed_vlan != vlan or observed_gem != gem_index:
            return True
        observed_ont_id = _sp_int(sp, "ont_id")
        if observed_ont_id is not None and ont_id is not None:
            if observed_ont_id != ont_id:
                return True
        observed_fsp = sp.get("fsp") if isinstance(sp, dict) else None
        if observed_fsp not in (None, "", fsp):
            return True
        return False
    return False


def collect(db: Session) -> list[Row]:
    ont_ids = (
        db.execute(select(OntUnit.id).where(OntUnit.is_active.is_(True)))
        .scalars()
        .all()
    )

    observations: dict[str, OntObservation] = {
        str(obs.ont_unit_id): obs
        for obs in db.execute(
            select(OntObservation).where(OntObservation.ont_unit_id.in_(ont_ids))
        ).scalars()
    }

    rows: list[Row] = []
    for ont in db.execute(select(OntUnit).where(OntUnit.id.in_(ont_ids))).scalars():
        row = Row(
            ont_unit_id=str(ont.id),
            serial_number=ont.serial_number or "",
            board=ont.board,
            port=ont.port,
            external_id=ont.external_id,
        )

        if not _fsp(ont.board, ont.port):
            row.issues.append("null_board_or_port")

        if parse_ont_id_on_olt(ont.external_id) is None:
            row.issues.append("unparseable_external_id")

        try:
            effective = resolve_effective_ont_config(db, ont)
        except Exception:  # noqa: BLE001 - a census must not abort on one bad row
            effective = {}
        values = effective.get("values", {}) if isinstance(effective, dict) else {}
        mgmt_index = values.get("mgmt_service_port_index")
        wan_index = values.get("wan_service_port_index")
        mgmt_vlan = values.get("mgmt_vlan")
        wan_vlan = values.get("wan_vlan")
        wan_gem_index = _int_or_none(values.get("wan_gem_index")) or 1

        # Coerced the same way ``adapters.desired_from_ont_unit`` coerces
        # every one of these fields — a non-numeric stored value must count
        # as unresolved here too, not as "set" because the raw string was
        # truthy.
        mgmt_index_int = _int_or_none(mgmt_index)
        wan_index_int = _int_or_none(wan_index)
        mgmt_vlan_int = _int_or_none(mgmt_vlan)
        wan_vlan_int = _int_or_none(wan_vlan)

        if mgmt_index_int is None or wan_index_int is None:
            row.issues.append("null_service_port_index")

        observation = observations.get(row.ont_unit_id)
        observed_ports = (
            list(observation.olt_service_ports or [])
            if observation is not None and observation.olt_service_ports is not None
            else None
        )

        fsp = _fsp(ont.board, ont.port)
        ont_id = parse_ont_id_on_olt(ont.external_id)
        if _observed_mismatch_at_index(
            observed_ports,
            index=mgmt_index_int,
            vlan=mgmt_vlan_int,
            gem_index=2,
            ont_id=ont_id,
            fsp=fsp,
        ) or _observed_mismatch_at_index(
            observed_ports,
            index=wan_index_int,
            vlan=wan_vlan_int,
            gem_index=wan_gem_index,
            ont_id=ont_id,
            fsp=fsp,
        ):
            row.issues.append("observed_vlan_gem_mismatch")

        if row.issues:
            rows.append(row)

    return rows


def main() -> int:
    with read_only_snapshot_session() as db:
        rows = collect(db)
        db.rollback()

    counts = Counter(issue for row in rows for issue in row.issues)
    report = {
        "affected_active_onts": len(rows),
        "counts": dict(counts.most_common()),
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    if rows:
        print(
            "\nActive ONTs affected by the OLT identity / service-port fix "
            "(review before merge):",
            file=sys.stderr,
        )
        for row in sorted(rows, key=lambda r: r.serial_number):
            print(
                f"  {row.ont_unit_id}  serial={row.serial_number:<20} "
                f"board={row.board!r} port={row.port!r} "
                f"external_id={row.external_id!r}  issues={','.join(row.issues)}",
                file=sys.stderr,
            )
        return EXIT_DIRTY
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
