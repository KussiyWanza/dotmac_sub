"""Harvest Huawei OLT MAC-forwarding tables into ForwardingObservation rows.

This is the "hop-1" foundation: for each Huawei OLT we walk the ACTIVE PON
ports (bounded from the DB — the ports that already carry online ONTs) and run
the read-only ``display mac-address port <F/S/P>`` command. Each learned
customer/router MAC maps to the exact PON port (F/S/P) and the ONT-ID (the VPI
column in Huawei's output), so we can resolve it back to an ``OntUnit`` and
detect ONT<->subscriber drift.

Design constraints (proven ground truth, Huawei MA5608T BOI OLT):

* ``display mac-address all`` PAGINATES / truncates at "More" — never used.
  We scope strictly per active PON port instead.
* The command is read-only. This module NEVER mutates OLT state and NEVER
  modifies ``OntAssignment`` — drift is logged/counted only.
* MACs are normalized to the canonical uppercase colon-separated form that
  ``subscriptions.mac_address`` is matched against.

The harvest is idempotent: re-running refreshes ``observed_at``/``vlan``/
``pon_port`` in place (upsert on ``(olt_device_id, mac, ont_id_on_olt)``) and
prunes rows older than the configurable age-out (default 6h).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.network import (
    ForwardingObservation,
    OLTDevice,
    OntAssignment,
    OntUnit,
)
from app.services.network._common import normalize_mac_address
from app.services.network.olt_ssh_diagnostics import _run_readonly_command
from app.services.network.serial_utils import parse_ont_id_on_olt
from app.services.radio_registration import _compact_mac_sql, compact_mac

logger = logging.getLogger(__name__)

# Source tag stamped on every row this harvester writes (matches the column
# server_default in migration 212).
SOURCE = "huawei_olt_mac"

# Age-out fallback when the domain setting is unset. Observations older than
# this are pruned each run so the table stays an ephemeral, current snapshot.
DEFAULT_AGE_OUT_HOURS = 6
_AGE_OUT_SETTING_KEY = "olt_mac_harvest_age_out_hours"

_FSP_RE = re.compile(r"^\d+/\d+/\d+$")

# One data row of ``display mac-address port <F/S/P>``. Anchored on the
# dotted-quad MAC so header/separator/"More" lines never match. F/S/P is
# printed with spaces around the slashes ("0 /1 /7"); VPI is the ONT-ID, and
# the trailing integer is the VLAN. VCI (between VPI and VLAN) is skipped.
_MAC_ROW_RE = re.compile(
    r"(?P<mac>[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4})"
    r"\s+(?P<mac_type>\S+)"
    r"\s+(?P<f>\d+)\s*/\s*(?P<s>\d+)\s*/\s*(?P<p>\d+)"
    r"\s+(?P<vpi>\d+)"
    r"\s+\S+"  # VCI (may be "-")
    r"\s+(?P<vlan>\d+)"
)


@dataclass(frozen=True)
class MacForwardingEntry:
    """A single learned MAC parsed from ``display mac-address port``."""

    mac: str  # canonical uppercase colon-separated
    fsp: str  # "0/1/7"
    ont_id: int  # the VPI column = ONT-ID on the OLT
    vlan: int | None = None
    mac_type: str | None = None
    raw: str = ""


@dataclass
class _PortContext:
    """Active PON port discovered from the DB, plus its online ONT lookup."""

    fsp: str
    pon_port_id: uuid.UUID | None
    onts_by_ont_id: dict[int, OntUnit] = field(default_factory=dict)


def parse_mac_address_port(output: str) -> list[MacForwardingEntry]:
    """Parse Huawei ``display mac-address port <F/S/P>`` output.

    Extracts one ``MacForwardingEntry`` per learned MAC, normalizing the
    dotted-quad MAC to canonical form and the F/S/P (printed with spaces) to
    ``"F/S/P"``. Unparseable/header/pagination lines are silently skipped.
    """
    entries: list[MacForwardingEntry] = []
    for line in output.splitlines():
        match = _MAC_ROW_RE.search(line)
        if not match:
            continue
        canonical = normalize_mac_address(match.group("mac"))
        if canonical is None:
            continue
        fsp = f"{match.group('f')}/{match.group('s')}/{match.group('p')}"
        entries.append(
            MacForwardingEntry(
                mac=canonical,
                fsp=fsp,
                ont_id=int(match.group("vpi")),
                vlan=int(match.group("vlan")),
                mac_type=match.group("mac_type").strip().lower() or None,
                raw=line.strip(),
            )
        )
    return entries


def _fsp_from_ont(ont: OntUnit) -> str:
    """Build the canonical F/S/P string from ``OntUnit.board`` + ``port``.

    Mirrors ``reconcile.adapters._fsp_from_ont`` — board holds "F/S", port
    holds "P". Returns "" when they don't form a valid F/S/P.
    """
    board = str(getattr(ont, "board", "") or "").strip()
    port = str(getattr(ont, "port", "") or "").strip()
    if not board or not port:
        return ""
    fsp = f"{board}/{port}"
    return fsp if _FSP_RE.fullmatch(fsp) else ""


def _active_ports_for_olt(db: Session, olt: OLTDevice) -> dict[str, _PortContext]:
    """Discover ACTIVE PON ports for an OLT straight from the DB.

    An "active" port is one that already carries at least one online ONT. This
    bounds the walk to exactly the ports worth querying (instead of the
    paginated ``display mac-address all``) and, as a side effect, builds the
    per-port ONT-ID -> OntUnit lookup used to resolve each learned MAC.
    """
    from app.services.network.ont_status import effective_ont_online_clause

    rows = db.scalars(
        select(OntUnit).where(
            OntUnit.olt_device_id == olt.id,
            effective_ont_online_clause(),
            OntUnit.is_active.is_(True),
        )
    ).all()

    ports: dict[str, _PortContext] = {}
    for ont in rows:
        fsp = _fsp_from_ont(ont)
        if not fsp:
            continue
        ctx = ports.get(fsp)
        if ctx is None:
            ctx = _PortContext(fsp=fsp, pon_port_id=ont.pon_port_id)
            ports[fsp] = ctx
        elif ctx.pon_port_id is None and ont.pon_port_id is not None:
            ctx.pon_port_id = ont.pon_port_id
        ont_id = parse_ont_id_on_olt(ont.external_id)
        if ont_id is not None:
            ctx.onts_by_ont_id.setdefault(ont_id, ont)
    return ports


def _upsert_observation(
    db: Session,
    *,
    olt: OLTDevice,
    ont: OntUnit | None,
    pon_port_id: uuid.UUID | None,
    entry: MacForwardingEntry,
    now: datetime,
) -> None:
    """Insert or refresh the ForwardingObservation for this (olt, mac, ont-id).

    Portable upsert (works on Postgres and the SQLite test schema): select on
    the unique identity, then update in place or insert.
    """
    existing = db.scalars(
        select(ForwardingObservation).where(
            ForwardingObservation.olt_device_id == olt.id,
            ForwardingObservation.mac == entry.mac,
            ForwardingObservation.ont_id_on_olt == entry.ont_id,
        )
    ).first()
    if existing is not None:
        existing.ont_unit_id = ont.id if ont is not None else None
        existing.pon_port_id = pon_port_id
        existing.vlan = entry.vlan
        existing.observed_at = now
        existing.source = SOURCE
        return
    db.add(
        ForwardingObservation(
            olt_device_id=olt.id,
            ont_unit_id=ont.id if ont is not None else None,
            pon_port_id=pon_port_id,
            ont_id_on_olt=entry.ont_id,
            mac=entry.mac,
            vlan=entry.vlan,
            observed_at=now,
            source=SOURCE,
        )
    )


def _subscriber_for_mac(db: Session, mac_canonical: str) -> uuid.UUID | None:
    """Return the single active subscriber owning this MAC, else None.

    Matches ``subscriptions.mac_address`` on the bare-hex form. Returns None
    when there is no active subscription for the MAC OR when the match is
    ambiguous (more than one distinct subscriber) — confident matches only.
    """
    compact = compact_mac(mac_canonical)
    if compact is None:
        return None
    subscriber_ids = (
        db.execute(
            select(Subscription.subscriber_id)
            .where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.mac_address.isnot(None),
                _compact_mac_sql(Subscription.mac_address) == compact,
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    if len(subscriber_ids) == 1:
        return subscriber_ids[0]
    return None


def _active_assignment(db: Session, ont_unit_id: uuid.UUID) -> OntAssignment | None:
    return db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont_unit_id,
            OntAssignment.active.is_(True),
        )
    ).first()


def _evaluate_drift(
    db: Session,
    *,
    olt: OLTDevice,
    ont: OntUnit | None,
    pon_port_id: uuid.UUID | None,
    entry: MacForwardingEntry,
    counters: dict[str, int],
) -> None:
    """Read-only ONT<->subscriber drift check for one observation.

    Never mutates the assignment. Emits a structured log + counter when the
    MAC's active subscriber differs from the ONT's active assignment, or counts
    a linkable ONT that has a confident MAC match but no assignment yet.
    """
    if ont is None:
        return
    subscriber_id = _subscriber_for_mac(db, entry.mac)
    if subscriber_id is None:
        return

    assignment = _active_assignment(db, ont.id)
    if assignment is None:
        counters["linkable_no_assignment"] += 1
        logger.info(
            "olt_mac_ont_linkable_no_assignment "
            "olt_device_id=%s ont_unit_id=%s fsp=%s ont_id_on_olt=%s "
            "mac=%s observation_subscriber_id=%s",
            olt.id,
            ont.id,
            entry.fsp,
            entry.ont_id,
            entry.mac,
            subscriber_id,
        )
        return

    if (
        assignment.subscriber_id is not None
        and assignment.subscriber_id != subscriber_id
    ):
        counters["drift_detected"] += 1
        logger.warning(
            "olt_mac_ont_subscriber_drift "
            "olt_device_id=%s ont_unit_id=%s pon_port_id=%s fsp=%s "
            "ont_id_on_olt=%s mac=%s vlan=%s "
            "observation_subscriber_id=%s assignment_subscriber_id=%s",
            olt.id,
            ont.id,
            pon_port_id,
            entry.fsp,
            entry.ont_id,
            entry.mac,
            entry.vlan,
            subscriber_id,
            assignment.subscriber_id,
        )


def _age_out_delta(db: Session) -> timedelta:
    hours = DEFAULT_AGE_OUT_HOURS
    try:
        from app.services.settings_spec import resolve_value

        value = resolve_value(db, SettingDomain.network, _AGE_OUT_SETTING_KEY)
        if value is not None:
            hours = int(value)
    except Exception:  # noqa: BLE001 - settings resolution is best-effort
        logger.debug("olt_mac_harvest_age_out_resolve_failed", exc_info=True)
    if hours <= 0:
        hours = DEFAULT_AGE_OUT_HOURS
    return timedelta(hours=hours)


def _prune_aged_out(
    db: Session,
    cutoff: datetime,
    *,
    olt_id: uuid.UUID | None = None,
) -> int:
    criteria = [
        ForwardingObservation.source == SOURCE,
        ForwardingObservation.observed_at < cutoff,
    ]
    if olt_id is not None:
        criteria.append(ForwardingObservation.olt_device_id == olt_id)
    result = db.execute(delete(ForwardingObservation).where(*criteria))
    return int(result.rowcount or 0)


def _new_counters() -> dict[str, int]:
    return {
        "olts_polled": 0,
        "ports_walked": 0,
        "observations": 0,
        "macs_seen": 0,
        "olt_errors": 0,
        "pruned": 0,
        "drift_detected": 0,
        "linkable_no_assignment": 0,
    }


def _harvest_olt_mac_tables(
    db: Session,
    *,
    olt_id: uuid.UUID | None,
) -> dict[str, int]:
    """Harvest one or every active Huawei OLT.

    For each OLT: discover active PON ports from the DB, run the read-only
    ``display mac-address port <F/S/P>`` per port, parse learned MACs, upsert a
    ForwardingObservation per (mac, F/S/P, ONT-ID), and run the read-only drift
    check. Per-OLT failures are isolated (SAVEPOINT rollback) so one bad OLT
    can't sink the whole run. The caller owns the outer commit.
    """
    counters = _new_counters()
    now = datetime.now(UTC)
    age_out = _age_out_delta(db)
    macs_seen: set[str] = set()

    query = select(OLTDevice).where(
        OLTDevice.vendor.ilike("%huawei%"),
        OLTDevice.is_active.is_(True),
    )
    if olt_id is not None:
        query = query.where(OLTDevice.id == olt_id)
    olts = db.scalars(query).all()

    # Read stage: materialize every OLT's port context plus a plain-data
    # SSH snapshot, then END the transaction. The SSH walk below takes seconds
    # per port; holding a transaction across it left this connection idle in
    # transaction for minutes, tripping the PostgreSQL health check
    # (idle_in_transaction_over_60s) on every run. The snapshot carries the
    # only fields the SSH layer reads, so collection never touches the session
    # (an expired ORM attribute access would silently begin a new
    # transaction and reintroduce the bug).
    contexts: dict[uuid.UUID, dict[str, _PortContext]] = {}
    ssh_snapshots: dict[uuid.UUID, SimpleNamespace] = {}
    orm_olts: dict[uuid.UUID, OLTDevice] = {}
    for olt in olts:
        counters["olts_polled"] += 1
        orm_olts[olt.id] = olt
        contexts[olt.id] = _active_ports_for_olt(db, olt)
        ssh_snapshots[olt.id] = SimpleNamespace(
            id=olt.id,
            name=olt.name,
            model=olt.model,
            mgmt_ip=olt.mgmt_ip,
            hostname=olt.hostname,
            ssh_username=olt.ssh_username,
            ssh_password=olt.ssh_password,
            ssh_port=olt.ssh_port,
            vendor=olt.vendor,
        )
    db.commit()

    # Collection stage: SSH walks with no transaction open. Raw outputs only;
    # per-OLT failures are isolated so one bad OLT can't sink the run.
    # Iterate the plain snapshots, never the ORM rows: post-commit, even
    # ``olt.id`` is an expired attribute whose refresh would silently begin a
    # new transaction (the regression test proved it).
    collected: list[tuple[uuid.UUID, list[tuple[str, str]]]] = []
    for olt_id, snapshot in ssh_snapshots.items():
        walked: list[tuple[str, str]] = []
        try:
            for fsp in contexts[olt_id]:
                # The snapshot mirrors every attribute the SSH path reads
                # (mgmt_ip/hostname/ssh_*/vendor/model/name/id) as plain data;
                # a live OLTDevice here would reopen a transaction post-commit.
                ok, status, output = _run_readonly_command(
                    cast("OLTDevice", snapshot), f"display mac-address port {fsp}"
                )
                counters["ports_walked"] += 1
                if not ok:
                    logger.warning(
                        "olt_mac_harvest_port_read_failed "
                        "olt_device_id=%s fsp=%s status=%s",
                        olt_id,
                        fsp,
                        status,
                    )
                    continue
                walked.append((fsp, output))
        except Exception:  # noqa: BLE001 - isolate per-OLT failure, keep going
            counters["olt_errors"] += 1
            logger.exception("olt_mac_harvest_olt_failed olt_device_id=%s", olt_id)
            continue
        collected.append((olt_id, walked))

    # Write stage: parse, upsert, and detect drift in per-OLT savepoints on a fresh
    # transaction (begun implicitly by the first query). The caller owns the
    # outer commit, as before.
    for olt_id, walked in collected:
        olt = orm_olts[olt_id]
        savepoint = db.begin_nested()
        try:
            ports = contexts[olt_id]
            for fsp, output in walked:
                ctx = ports[fsp]
                for entry in parse_mac_address_port(output):
                    ont = ctx.onts_by_ont_id.get(entry.ont_id)
                    pon_port_id = (
                        ont.pon_port_id if ont is not None else ctx.pon_port_id
                    )
                    _upsert_observation(
                        db,
                        olt=olt,
                        ont=ont,
                        pon_port_id=pon_port_id,
                        entry=entry,
                        now=now,
                    )
                    counters["observations"] += 1
                    macs_seen.add(entry.mac)
                    _evaluate_drift(
                        db,
                        olt=olt,
                        ont=ont,
                        pon_port_id=pon_port_id,
                        entry=entry,
                        counters=counters,
                    )
            savepoint.commit()
        except Exception:  # noqa: BLE001 - isolate per-OLT failure, keep going
            savepoint.rollback()
            counters["olt_errors"] += 1
            logger.exception(
                "olt_mac_harvest_olt_failed olt_device_id=%s", getattr(olt, "id", "?")
            )

    counters["macs_seen"] = len(macs_seen)
    counters["pruned"] = _prune_aged_out(db, now - age_out, olt_id=olt_id)
    logger.info("olt_mac_harvest_done %s", counters)
    return counters


def harvest_olt_mac_tables(db: Session) -> dict[str, int]:
    """Harvest every active Huawei OLT.

    This compatibility entry point retains the original fleet-wide behavior.
    Scheduled execution dispatches :func:`harvest_olt_mac_table` per OLT so a
    slow device cannot monopolize one task for the entire fleet.
    """

    return _harvest_olt_mac_tables(db, olt_id=None)


def harvest_olt_mac_table(db: Session, olt_id: uuid.UUID) -> dict[str, int]:
    """Harvest one active Huawei OLT and prune only that OLT's stale rows."""

    return _harvest_olt_mac_tables(db, olt_id=olt_id)
