"""OLT-side reader — SSH-driven observation of a single ONT.

Composes existing OLT helpers in ``app.services.network.olt_ssh_ont`` to fill
the ``OltObservedFields`` dataclass. Each call opens (typically) one SSH
session per query; batching multiple ``display`` commands over a shared
session is a follow-up optimisation.

Current scope:

* Presence + run/match state via ``adapter.find_ont_by_serial`` and
  ``status.get_ont_status``. These are enough for the planner to decide
  whether the ONT exists in the OLT's table and is alive on the PON.
* Description, profile bindings, mgmt IP, mgmt VLAN, distance via
  ``display ont info``.
* Optical Rx/Tx/temperature via ``display ont optical-info`` —
  best-effort; an unsupported / out-of-range reply leaves the optical
  fields as ``None`` without failing the whole read.
* Service-port enumeration filtered by ONT-ID via
  ``get_service_ports_for_ont`` (which runs ``display service-port port``
  on the PON and filters the table to one ONT). Each entry is recorded as
  a plain dict ``{index, vlan_id, ont_id, gem_index, flow_type,
  flow_para, state, fsp, tag_transform}`` so the JSONB observation row
  stays portable across versions of ``ServicePortEntry``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from app.services.network.olt_ssh_diagnostics import get_ont_optical_info
from app.services.network.olt_ssh_ont.status import (
    get_ont_info_detail,
    get_ont_status,
)
from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont
from app.services.network.olt_write_reconciliation import serial_matches

from ..state import OltObservedFields, OntDesiredState
from ._types import ReadResult

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


# Reachability errors we treat as "couldn't contact the OLT" — anything other
# than these counts as a parseable-but-bad reply.
_UNREACHABLE_FRAGMENTS = (
    "connection failed",
    "timeout",
    "no route to host",
    "connection refused",
    "unable to connect",
)


def read_olt_state(
    adapter: Any,
    desired: OntDesiredState,
    *,
    deadline: datetime | None = None,
) -> ReadResult[OltObservedFields]:
    """Read the OLT-observed fields for one ONT.

    Args:
        adapter: An ``OltProtocolAdapter`` (or any object with the same
            ``find_ont_by_serial`` + ``olt`` attribute surface). The reader
            uses its ``.olt`` for the SSH parameters and its
            ``find_ont_by_serial`` for the presence check.
        desired: The ONT's desired state. Used to resolve ``fsp`` and
            ``olt_ont_id`` for the per-ONT queries.
        deadline: Optional cutoff. Honored at the entry point only — the
            existing SSH helpers don't accept a deadline themselves, so a
            single late call may still take its full configured timeout.
            ``reconcile_ont`` enforces the outer budget.

    Returns:
        A ``ReadResult[OltObservedFields]``.
    """
    olt = getattr(adapter, "olt", None) or getattr(adapter, "_olt", None)
    if olt is None:
        return ReadResult(
            status="unavailable",
            observed=None,
            error="OLT adapter has no .olt attribute",
        )

    # 1. Presence check via serial. Cheaper than display-ont-info and works
    # regardless of whether we already know the ONT-ID.
    find = adapter.find_ont_by_serial(desired.serial_number)
    if not find.success:
        return ReadResult(
            status="unavailable",
            observed=None,
            error=find.message,
            transport_unreachable=_looks_unreachable(find.message),
        )

    registration = (find.data or {}).get("registration") if find.data else None
    target_known = desired.olt_ont_id is not None and bool(desired.fsp)

    if registration is None:
        # OLT confirms the ONT is not registered anywhere. That's a clean
        # read with ``olt_present=False`` — the planner will plan to add it,
        # PROVIDED it knows the target fsp/olt_ont_id to authorize at. When
        # it doesn't, there is nothing to authorize with either, so this is
        # reported as unresolved rather than a plain "not yet authorized".
        return ReadResult(
            status="absent",
            observed=_absent_fields(
                identity_status="bound" if target_known else "unresolved"
            ),
            error=None,
        )

    # Identity binding (Astra Bug 1): a serial-scoped registration lookup
    # proves the serial exists SOMEWHERE on the OLT, but never that it sits
    # at the STORED (``desired.fsp``/``desired.olt_ont_id``) target — reusing
    # its real fsp/onu_id here, or querying the stored coordinates without
    # checking them first, either one can silently read/write the wrong
    # physical port. Every later per-ONT query in this function may use
    # ``desired.fsp``/``desired.olt_ont_id`` ONLY after this confirms they
    # equal what was actually found.
    reg_serial = getattr(registration, "real_serial", None) or getattr(
        registration, "serial_number", None
    )
    reg_fsp = getattr(registration, "fsp", None)
    reg_onu_id = getattr(registration, "onu_id", None)

    if reg_serial is not None and not serial_matches(reg_serial, desired.serial_number):
        # Defensive: a serial-scoped lookup returned an entry for a
        # different serial than requested. Never seen from the production
        # by-serial CLI query (its serial IS the query), but a future
        # adapter implementation (or a scan-based fallback) could return a
        # substring match — refuse rather than trust it.
        #
        # ``status="unavailable"`` — NOT "present" — because this observation
        # cannot be trusted as being of the STORED target at all. A
        # "present"/"absent" result flows straight into
        # ``upsert_ont_observation`` and overwrites the OLT columns with
        # whatever ``observed`` carries; treating an unconfirmed identity as
        # a clean read would silently null the last genuine evidence and
        # stamp a fresh ``olt_observed_at`` for a pass that observed nothing
        # of the named target. See ``ReadResult.identity_status``.
        return ReadResult(
            status="unavailable",
            observed=None,
            error=(
                f"ONT lookup for serial {desired.serial_number} returned a "
                f"registration for a different serial ({reg_serial!r})"
            ),
            identity_status="unresolved",
        )

    if not target_known:
        return ReadResult(
            status="unavailable",
            observed=None,
            error=(
                f"ONT {desired.serial_number} is registered on the OLT, but "
                "the desired state has no fsp/olt_ont_id to confirm it "
                "against — cannot query per-ONT detail safely."
            ),
            identity_status="unresolved",
        )

    if reg_fsp != desired.fsp or reg_onu_id != desired.olt_ont_id:
        return ReadResult(
            status="unavailable",
            observed=None,
            error=(
                f"ONT {desired.serial_number} is registered at {reg_fsp}/"
                f"{reg_onu_id}, not the desired target "
                f"{desired.fsp}/{desired.olt_ont_id} — refusing to read or "
                "write against either."
            ),
            identity_status="mismatch",
        )

    # Bound: confirmed the registration IS an observation of the stored
    # target. ``ont_id`` is a local ``int`` narrowing of
    # ``desired.olt_ont_id`` (``target_known`` above proved it is not
    # ``None``) so the per-ONT helpers below keep their existing ``int``
    # signatures.
    fsp = desired.fsp
    ont_id = desired.olt_ont_id
    assert ont_id is not None  # narrowed by ``target_known``

    # 2. Fetch run/match state for the registered ONT. Some details
    # (description, profiles, optical) require richer parsing not yet wired.
    ok, msg, status_entry = get_ont_status(olt, fsp, ont_id)
    if not ok:
        if _looks_unreachable(msg):
            return ReadResult(
                status="unavailable",
                observed=None,
                error=msg,
                transport_unreachable=True,
            )
        # Treat as present-but-status-unknown rather than a hard failure: the
        # planner can still reason about presence even when run-state is dark.
        logger.debug("olt_reader_status_unavailable", extra={"message": msg})
        return ReadResult(
            status="present",
            observed=_present_with_unknown_state(),
            error=None,
        )

    # 3. Fetch richer ONT info — description, profile ids, mgmt IP, mgmt VLAN,
    # distance. This is a second SSH session (a follow-up could batch with the
    # status query). If it fails, fall back to status-only observation rather
    # than the whole read failing — partial data is still useful to the planner.
    detail_ok, detail_msg, detail = get_ont_info_detail(olt, fsp, ont_id)
    if not detail_ok and _looks_unreachable(detail_msg):
        return ReadResult(
            status="unavailable",
            observed=None,
            error=detail_msg,
            transport_unreachable=True,
        )
    detail = detail or {}
    tr069_profile_id = _int_or_none(detail.get("tr069_profile_id"))
    if tr069_profile_id is None:
        binding_reader = getattr(adapter, "get_tr069_profile_binding", None)
        if callable(binding_reader):
            try:
                binding = binding_reader(fsp, ont_id)
                if binding.success:
                    tr069_profile_id = _int_or_none(
                        (binding.data or {}).get("profile_id")
                    )
            except Exception:
                logger.debug("olt_reader_tr069_binding_unavailable", exc_info=True)

    # 4. Optical levels (Rx/Tx dBm, temperature). Best-effort: optical-info
    # can return an Out-of-range or "Not supported" line on some firmwares
    # and that should not fail the whole read.
    olt_rx_dbm, olt_tx_dbm, olt_temperature_c = _read_optical(olt, fsp, ont_id)

    # 5. Service-port enumeration. Same best-effort policy: if the SSH read
    # fails, leave the tuple empty so the planner falls back to the
    # imported state. Failing here would block all sync writes against an
    # otherwise-healthy ONT.
    olt_service_ports = _read_service_ports(olt, fsp, ont_id)

    return ReadResult(
        status="present",
        observed=OltObservedFields(
            olt_present=True,
            # ``_normalise_state`` narrows the string to the allowed set or
            # returns None; the cast tells mypy that runtime invariant.
            olt_match_state=cast(
                "Literal['match', 'mismatch', 'initial'] | None",
                _normalise_state(
                    status_entry.match_state if status_entry else None,
                    allowed={"match", "mismatch", "initial"},
                ),
            ),
            olt_run_state=cast(
                "Literal['online', 'offline', 'los'] | None",
                _normalise_state(
                    status_entry.run_state if status_entry else None,
                    allowed={"online", "offline", "los"},
                ),
            ),
            olt_distance_m=_int_or_none(detail.get("distance_m")),
            olt_rx_dbm=olt_rx_dbm,
            olt_tx_dbm=olt_tx_dbm,
            olt_temperature_c=olt_temperature_c,
            olt_description=_str_or_none(detail.get("description")),
            olt_mgmt_ip=_str_or_none(detail.get("mgmt_ip")),
            olt_mgmt_vlan=_int_or_none(detail.get("mgmt_vlan")),
            olt_line_profile_id=_int_or_none(detail.get("line_profile_id")),
            olt_service_profile_id=_int_or_none(detail.get("service_profile_id")),
            olt_tr069_profile_id=tr069_profile_id,
            olt_service_ports=olt_service_ports,
        ),
        error=None,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _absent_fields(
    *, identity_status: Literal["bound", "unresolved"] = "bound"
) -> OltObservedFields:
    """Field set for an ONT that the OLT does not have registered."""
    return OltObservedFields(
        olt_present=False,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_tr069_profile_id=None,
        olt_service_ports=(),
        olt_identity_status=identity_status,
    )


def _present_with_unknown_state() -> OltObservedFields:
    """ONT is registered but we couldn't fetch run/match state.

    Conservative: report present=True so the planner doesn't try to re-add
    the ONT, but leave run/match as None so the planner avoids inferring
    anything about its liveness.
    """
    return OltObservedFields(
        olt_present=True,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_tr069_profile_id=None,
        # Never queried on this path either (status query failed first) —
        # unknown, not confirmed-zero.
        olt_service_ports=None,
    )


def _int_or_none(value: Any) -> int | None:
    """Coerce a parser dict value to ``int``, tolerating None / empty / strings."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    """Filter out empty/dash placeholders Huawei emits for "no value"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    return text


def _normalise_state(raw: str | None, *, allowed: set[str]) -> str | None:
    """Reduce vendor state strings to the canonical set we record.

    Huawei sometimes reports ``"online"`` lowercase, sometimes ``"ONLINE"``;
    ``"normal"`` is treated as ``"online"`` (legacy) ... but be conservative —
    if we don't recognise the value, return None rather than guessing.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in allowed:
        return text
    if text == "normal" and "online" in allowed:
        # Some Huawei firmwares report "normal" for a healthy ONT.
        return "online"
    return None


def _looks_unreachable(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(frag in lowered for frag in _UNREACHABLE_FRAGMENTS)


def _read_optical(
    olt: Any, fsp: str, ont_id: int
) -> tuple[float | None, float | None, int | None]:
    """Best-effort optical read.

    Returns ``(olt_rx_dbm, olt_tx_dbm, olt_temperature_c)``.

    - ``olt_rx_dbm`` is the OLT-side received power from this ONT's upstream
      signal (the "drop fiber" alert metric).
    - ``olt_tx_dbm`` is the ONT-reported upstream Tx — the value of the
      laser leaving the ONT, before fiber loss.
    - ``olt_temperature_c`` is the ONT laser temperature (rounded to int).
    """
    try:
        ok, _msg, info = get_ont_optical_info(olt, fsp, ont_id)
    except Exception:
        logger.debug("olt_reader_optical_unavailable", exc_info=True)
        return None, None, None
    if not ok or info is None:
        return None, None, None

    rx = info.olt_rx_power_dbm
    tx = info.tx_power_dbm
    temp_raw = info.temperature_c
    temperature = int(round(temp_raw)) if temp_raw is not None else None
    return rx, tx, temperature


def _read_service_ports(
    olt: Any, fsp: str, ont_id: int
) -> tuple[dict[str, Any], ...] | None:
    """Service-port enumeration for one ONT.

    Each ``ServicePortEntry`` is converted to a plain dict so the planner
    can index it with ``sp.get("index")`` regardless of whether the
    underlying dataclass shape evolves.

    Returns ``None`` — not ``()`` — on SSH failure/exception: the OLT's real
    port set is unknown, and treating "the read failed" as "no ports exist"
    previously planned a CREATE against an index that already had a live
    port (and no delete for anything, since an empty observed list also
    happens to protect every stale port from the delete sweep). ``()`` is
    reserved for a genuine successful read that found none.
    """
    try:
        ok, _msg, entries = get_service_ports_for_ont(olt, fsp, ont_id)
    except Exception:
        logger.debug("olt_reader_service_ports_unavailable", exc_info=True)
        return None
    if not ok:
        return None
    if not entries:
        return ()

    return tuple(
        {
            "index": int(entry.index),
            "vlan_id": int(entry.vlan_id),
            "ont_id": int(entry.ont_id),
            "gem_index": int(entry.gem_index),
            "flow_type": str(entry.flow_type or ""),
            "flow_para": str(entry.flow_para or ""),
            "state": str(entry.state or ""),
            "fsp": str(entry.fsp or ""),
            "tag_transform": str(entry.tag_transform or ""),
        }
        for entry in entries
    )
