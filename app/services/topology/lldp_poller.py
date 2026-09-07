"""LLDP neighbor collector for observed interface adjacency.

Reads each MikroTik router's ``/ip/neighbor`` (LLDP/CDP/MNDP discovery, enabled
fleet-wide via the ``lldp-infra`` setting) and records device/interface
adjacency observations. Read-only against routers; the reconcile owns
``source='lldp_neighbor'`` rows (upsert + soft-prune) and never touches
manual/other links. These rows can verify `network.forwarding_topology`
declarations but never create official path, role, site, or outage ancestry.

Fetch mechanism: the neighbor tables are read over the RouterOS **binary API**
(port 8728, the ``routeros_api`` library) — the exact transport the bandwidth
poller (``app/poller/mikrotik_poller.py``) already uses against these routers.
REST is enabled on most of the fleet but binary works on more of it, so binary
is tried first. We iterate the ``routers`` table directly (each row carries the
same user account for API and REST) and map each router to its
``network_device_id``.

REST (443) fallback: the fleet is mixed — a few core routers (Garki Core,
Abuja Medallion; both CCR1072) have 8728 FILTERED and answer only on HTTPS/REST
(443). The binary transport reaches most routers but not those REST-only
cores. Therefore a binary read that fails
with a CONNECTION-class error (timeout / refused / no route /
``RouterOsApiConnectionError``) falls back to ``GET /rest/ip/neighbor`` over 443
(:func:`_read_neighbors_via_rest`, the pre-#840 REST path restored). An AUTH
failure is deliberately NOT retried over REST — REST hits the same user/RADIUS
backend, so it would fail identically (e.g. Kubwa's device-side RADIUS "could
not authenticate") — and counts as a plain ``routers_failed`` as before.

Match (``_match_with_strategy``, additive + conservative): exact normalized
neighbor ``identity`` -> network_device name/hostname; then IPv4
``address``/``address4`` -> mgmt_ip (a colon marks an IPv6/``fe80::`` value and
is ignored so link-local-only neighbors still fall through to identity); then a
guarded fuzzy identity match (token subset, ambiguous -> no match) so aliases
like ``BOI Asokoro Access`` -> ``BOI Asokoro`` and ``Abuja Core I Garki`` ->
``Garki Core`` resolve. Empty identity with no IP hit or no match -> dropped
(CPE/unknown). Discovered pairs that already have an authoritative manual link
are not duplicated. There is no device-MAC column on ``network_devices``, so a
MAC strategy is intentionally not implemented (no schema is invented).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC
from typing import Literal, TypeAlias

import routeros_api
from billiard.exceptions import SoftTimeLimitExceeded
from routeros_api.exceptions import (
    RouterOsApiConnectionError,
    RouterOsApiParsingError,
)
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from app.models.event_store import EventStatus
from app.models.network_monitoring import (
    NetworkDevice,
    NetworkTopologyLink,
    TopologyLinkMedium,
)
from app.models.router_management import Router
from app.services.credential_crypto import decrypt_credential
from app.services.domain_errors import DomainError
from app.services.event_store import create_event_record
from app.services.events.types import Event, EventType
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.topology.lldp_contracts import (
    LldpDevice,
    LldpEdge,
    LldpJumpHost,
    LldpLinkState,
    LldpPoll,
    LldpReadQuery,
    LldpRouter,
    LldpSnapshot,
    LldpStats,
    ReconcileLldpCommand,
)

NeighborRow: TypeAlias = dict[str, str]
NeighborReader: TypeAlias = Callable[
    [LldpRouter],
    tuple[list[NeighborRow], Literal["binary", "rest"]] | list[NeighborRow],
]

logger = logging.getLogger(__name__)

SOURCE = "lldp_neighbor"

# RouterOS binary API port used fleet-wide for bandwidth polling. REST (443) is
# disabled on every production router, so neighbors are read here instead.
BINARY_API_PORT = 8728

# Discovery-grade per-router socket timeout. This is a read-only hourly poll —
# "skip this router this hour, retry next hour" beats hanging on a silently-
# dropping router. 2/25 routinely time out (heavily-loaded cores); they are
# counted, not fatal.
ROUTER_SOCKET_TIMEOUT = 15.0

# Discovery-grade REST fallback tunables for REST-only cores (Garki Core /
# Abuja Medallion: 8728 filtered,
# HTTPS answers). One attempt, tight timeouts (~12s read bound), same "skip this
# router this hour, retry next" philosophy as the binary socket timeout above.
ROUTER_REST_CONNECT_TIMEOUT = 5.0
ROUTER_REST_READ_TIMEOUT = 12.0

# Wall-clock safety net: the Celery task has soft_time_limit=300 — stop
# attempting new devices before that so the run finishes cleanly (upsert +
# prune) instead of timing out mid-fleet.
TIME_BUDGET_SECONDS = 240.0

# routeros_api surfaces the cleartext password in some exception strings; strip
# it before anything reaches the logs.
_PASSWORD_RE = re.compile(r"=password=[^\x00 ]*")


def _sanitize_exc(exc: BaseException) -> str:
    """Strip routeros_api's cleartext =password=... from exception text."""
    message = _PASSWORD_RE.sub("=password=<redacted>", str(exc))
    return message or type(exc).__name__


def _norm(value: str | None) -> str:
    """Lowercase + collapse runs of whitespace/hyphens to a single space."""
    return re.sub(r"[\s\-]+", " ", (value or "").strip().lower())


# Generic role/site tokens stripped before fuzzy identity matching. A neighbor's
# advertised /system identity and the modeled device name often differ only by
# these ("BOI Asokoro Access" vs "BOI Asokoro"; "Gudu SW" vs "Gudu Switch" —
# 'sw' and 'switch' both drop, so they compare equal; "Abuja Core I Garki" vs
# "Garki Core"). Kept deliberately small/conservative: it only removes generic
# words, never the distinguishing site token.
ROLE_WORDS = frozenset({"access", "switch", "sw", "router", "abj", "core"})


def _strip_tokens(value: str | None) -> frozenset[str]:
    """Significant-token set of a name/identity for fuzzy matching.

    Lowercase, split on any non-alphanumeric run, and drop the generic
    ``ROLE_WORDS``. Returns the remaining tokens as a set so that word order and
    generic role/site suffixes don't defeat the comparison.
    """
    tokens = re.split(r"[^a-z0-9]+", (value or "").lower())
    return frozenset(t for t in tokens if t and t not in ROLE_WORDS)


def _neighbor_identity(nb: dict) -> str:
    return str(nb.get("identity") or "")


def _neighbor_address(nb: dict) -> str | None:
    """The neighbor's IPv4 address, or None.

    Only an IPv4 address can key into ``mgmt_ip``; a colon marks an IPv6 value
    (including the ``fe80::`` link-local that MANY neighbors advertise as their
    ONLY address). Such neighbors must not be dropped — returning None here lets
    them fall through to identity matching instead of failing on the address."""
    for key in ("address4", "address"):
        val = nb.get(key)
        if val and ":" not in val:
            return val
    return None


def build_device_index(
    session: Session,
) -> tuple[dict[str, NetworkDevice], dict[str, NetworkDevice]]:
    """Index active nodes by normalized name/hostname and by mgmt_ip."""
    by_name: dict[str, NetworkDevice] = {}
    by_ip: dict[str, NetworkDevice] = {}
    for d in (
        session.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    ):
        for label in (d.name, d.hostname):
            n = _norm(label)
            if n:
                by_name.setdefault(n, d)
        if d.mgmt_ip:
            by_ip.setdefault(d.mgmt_ip, d)
    return by_name, by_ip


def _build_match_index(
    session: Session,
) -> tuple[dict[str, NetworkDevice], dict[str, NetworkDevice], list]:
    """Richer index for the poller's matcher: (by_name, by_ip, stripped).

    ``stripped`` is a list of ``(token_set, device)`` used for the conservative
    fuzzy identity fallback (strategy c). Kept separate from
    ``build_device_index`` — whose 2-tuple shape other callers rely on — so
    those callers stay untouched. Devices contribute one entry per label
    (name/hostname); matches are deduplicated by device id."""
    by_name: dict[str, NetworkDevice] = {}
    by_ip: dict[str, NetworkDevice] = {}
    stripped: list[tuple[frozenset[str], NetworkDevice]] = []
    for d in (
        session.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    ):
        for label in (d.name, d.hostname):
            n = _norm(label)
            if n:
                by_name.setdefault(n, d)
            toks = _strip_tokens(label)
            if toks:
                stripped.append((toks, d))
        if d.mgmt_ip:
            by_ip.setdefault(d.mgmt_ip, d)
    return by_name, by_ip, stripped


def _match_stripped(stripped, identity: str) -> NetworkDevice | None:
    """Conservative fuzzy identity match (strategy c).

    Compare the neighbor identity's stripped token-set to each device's. A match
    requires one set to be a (non-empty) subset of the other — so
    ``BOI Asokoro Access`` <-> ``BOI Asokoro`` and ``Abuja Core I Garki`` ->
    ``Garki Core`` resolve, while generic role words never distinguish. If more
    than one distinct device matches, it is ambiguous -> None (never guess)."""
    id_set = _strip_tokens(identity)
    if not id_set:
        return None
    matches: dict = {}
    for dev_set, dev in stripped:
        if dev_set and (dev_set <= id_set or id_set <= dev_set):
            matches[dev.id] = dev
    if len(matches) == 1:
        return next(iter(matches.values()))
    return None


def _match_with_strategy(index, nb: dict) -> tuple[NetworkDevice | None, str | None]:
    """Match a neighbor row to a device, returning (device, strategy).

    Priority (each additive, conservative, guarded):
      identity           - exact normalized identity -> name/hostname
      address            - neighbor IPv4 address == a device mgmt_ip
      stripped_identity  - fuzzy token-subset identity match, unique device only
    Empty identity with no IP hit (CPE) or no match at all returns (None, None).

    Back-compatible with the classic ``build_device_index`` 2-tuple ``(by_name,
    by_ip)``: when the ``stripped`` list is absent the fuzzy strategy is simply
    skipped (exact identity + mgmt_ip still run), so ANY caller's index works —
    not just ``poll_all``'s 3-tuple ``_build_match_index``."""
    by_name, by_ip, *rest = index
    stripped = rest[0] if rest else []
    norm = _norm(_neighbor_identity(nb))
    if norm and norm in by_name:
        return by_name[norm], "identity"
    addr = _neighbor_address(nb)
    if addr and addr in by_ip:
        return by_ip[addr], "address"
    remote = _match_stripped(stripped, _neighbor_identity(nb))
    if remote is not None:
        return remote, "stripped_identity"
    return None, None


def match_in_index(index, nb: dict) -> NetworkDevice | None:
    """Match a neighbor against a prebuilt (by_name, by_ip, stripped) index."""
    return _match_with_strategy(index, nb)[0]


def match_neighbor(session: Session, nb: dict) -> NetworkDevice | None:
    """Match a single ``/ip/neighbor`` row to a known network_device, or None.

    Priority: exact identity -> address (mgmt_ip) -> conservative fuzzy identity.
    Empty identity with no IP hit (CPE) or no match at all returns None.
    """
    return match_in_index(_build_match_index(session), nb)


# --- Edge building -----------------------------------------------------------


def _medium(local_iface: str | None) -> TopologyLinkMedium:
    i = (local_iface or "").lower()
    if i.startswith("sfp"):
        return TopologyLinkMedium.fiber
    if i.startswith("ether"):
        return TopologyLinkMedium.ethernet
    return TopologyLinkMedium.unknown


def _canonical(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Order a device pair deterministically so A->B and B->A collapse to one."""
    return (a, b) if str(a) <= str(b) else (b, a)


def accumulate_edges(
    edges: dict,
    local: NetworkDevice | LldpDevice,
    neighbors: list[dict],
    index,
    strategy_counter: dict | None = None,
) -> dict:
    """Add this node's matched neighbor edges into ``edges`` (keyed by canonical
    device pair). Drops empty-identity/unmatched neighbors (CPE/unknown) and
    self-links; first observation of a pair wins (A<->B + repeats dedup).

    The neighbor ``interface`` string (e.g. ``"sfp-sfpplus3=>AFR Fiber"`` from
    the binary API) is the local port on ``local`` and is stashed on the edge.

    ``strategy_counter`` (if given) is bumped per new edge with the match
    strategy that resolved it (``matched_by_identity`` /
    ``matched_by_address`` / ``matched_by_stripped_identity``) for debugging."""
    for nb in neighbors:
        remote, strategy = _match_with_strategy(index, nb)
        if remote is None or remote.id == local.id:
            continue
        key = _canonical(local.id, remote.id)
        if key in edges:
            continue
        if strategy_counter is not None and strategy:
            strategy_counter[f"matched_by_{strategy}"] += 1
        local_iface = nb.get("interface") or ""
        edges[key] = {
            "source_device_id": key[0],
            "target_device_id": key[1],
            "medium": _medium(local_iface),
            "metadata": {
                "observed_from": str(local.id),
                "local_interface": local_iface,
                "remote_identity": _neighbor_identity(nb),
                "remote_board": (
                    nb.get("board") or nb.get("board-name") or nb.get("platform")
                ),
            },
        }
    return edges


# --- Connection + poll -------------------------------------------------------


def _is_empty_neighbor_table(exc: BaseException) -> bool:
    """True iff ``exc`` is routeros_api's parse failure for an EMPTY reply.

    A router whose ``/ip/neighbor`` table is empty answers with a bare
    ``!empty`` sentence that some ``routeros_api`` builds refuse to parse,
    raising ``RouterOsApiParsingError('Malformed sentence %s', [b'!empty', ...])``.
    That is legitimately "0 neighbors", not a router failure — so it is
    swallowed. Surgical: matches ONLY the ``!empty`` marker; every other parse
    error (genuinely malformed sentence/attribute) still propagates."""
    if not isinstance(exc, RouterOsApiParsingError):
        return False
    if "!empty" in str(exc):
        return True
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, (list, tuple)) and any(
            isinstance(a, (bytes, bytearray)) and b"!empty" in a for a in arg
        ):
            return True
    return False


def _read_neighbors_via_binary_api(
    router: LldpRouter, pool_factory=None
) -> list[NeighborRow]:
    """Read a router's ``/ip/neighbor`` over the RouterOS binary API (8728).

    Mirrors the bandwidth poller's proven connection path: construct a
    ``RouterOsApiPool`` on 8728 with ``plaintext_login`` and the router's
    decrypted API credentials (RouterOS uses the same user accounts for API and
    REST), then read ``/ip/neighbor``.

    CONNECTION HYGIENE: the pool opens a session on the router, so it is ALWAYS
    disconnected in ``finally`` — including when ``get_api()`` or the read fails
    after the pool was constructed — otherwise the session lingers for days and
    the fleet accrues an 8728 leak. This follows the bandwidth poller's
    ``_release_pool`` contract. ``pool_factory`` is injectable for tests.
    """
    pool_factory = pool_factory or routeros_api.RouterOsApiPool
    username = decrypt_credential(router.rest_api_username) or router.rest_api_username
    password = decrypt_credential(router.rest_api_password) or router.rest_api_password

    pool = pool_factory(
        router.management_ip,
        username=username,
        password=password,
        port=BINARY_API_PORT,
        plaintext_login=True,
    )
    try:
        # Bound blocking socket I/O so a half-open/firewalled router can't hang
        # this poll (best-effort: tolerate older library builds lacking it).
        try:
            pool.set_timeout(ROUTER_SOCKET_TIMEOUT)
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 - timeout tuning is best-effort
            pass
        api = pool.get_api()
        try:
            rows = list(api.get_resource("/ip/neighbor").get())
        except RouterOsApiParsingError as exc:
            # An empty neighbor table (bare '!empty' reply) is 0 neighbors, not
            # a router failure — real parse errors still propagate.
            if _is_empty_neighbor_table(exc):
                return []
            raise
        return [dict(row) for row in rows]
    finally:
        # The pool (and thus the router-side session) exists the moment
        # pool_factory returned; release it no matter how we leave.
        try:
            pool.disconnect()
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - never mask the real error
            logger.warning(
                "lldp_poll_pool_disconnect_failed router=%s: %s",
                router.name,
                _sanitize_exc(exc),
            )


def _read_neighbors_via_rest(router: LldpRouter) -> list[NeighborRow]:
    """Read a router's ``/ip/neighbor`` over the RouterOS REST API (443).

    Fallback transport for the REST-only cores (Garki Core, Abuja Medallion)
    whose 8728 is filtered but whose HTTPS/REST answers. This restores the
    established REST transport: it reuses
    :class:`RouterConnectionService` — the exact connection layer config
    snapshots use — with discovery-grade tunables (one attempt, tight timeouts)
    so a dead router costs seconds, not the snapshot retry budget. The service
    decrypts the same ``rest_api_*`` credentials, builds the
    ``management_ip``/``rest_api_port``/``use_ssl`` base URL, prepends ``/rest``,
    and closes the HTTP client via its context manager (no session leak).

    TLS: RouterOS presents a self-signed cert, so verification is governed by
    the router row's ``verify_tls`` column (``RouterConnectionService.get_client``
    passes ``verify=router.verify_tls``) — a per-router, settings/DB-driven flag,
    NOT a hand-rolled ``verify=False``. This is the established project pattern,
    consistent with the prior REST poller and every other config-snapshot
    call; no new insecure default is introduced here.

    REST returns ``/ip/neighbor`` as a JSON array whose objects already carry the
    hyphenated fields (``identity``, ``address``, ``address4``, ``address6``,
    ``mac-address``, ``interface``, ``board``/``board-name``/``platform``) — the
    SAME dict shape :func:`_read_neighbors_via_binary_api` yields — so downstream
    matching/edge-building is unchanged.
    """
    # Imported lazily: the REST connection layer pulls in its transport and is
    # only needed on the rare REST-only-core fallback path.
    from app.services.router_management.connection import RouterConnectionService

    # LLDP has always used one REST attempt. The established connection owner
    # provides an explicit one-shot boundary that cannot resolve DB settings.
    data = RouterConnectionService.execute_once(
        router,
        method="GET",
        path="/ip/neighbor",
        payload=None,
        connect_timeout=ROUTER_REST_CONNECT_TIMEOUT,
        read_timeout=ROUTER_REST_READ_TIMEOUT,
    )
    return [dict(row) for row in data] if isinstance(data, list) else []


# A binary-API read failure that WARRANTS a REST(443) retry: the router didn't
# answer on 8728 at all — connection refused / timeout / no route / TLS/socket
# error. ``RouterOsApiConnectionError`` (and its ConnectionClosedError subclass)
# is the library's wrapper; a bare ``OSError``/``socket.timeout`` can also reach
# us. An AUTH failure surfaces as ``RouterOsApiCommunicationError`` (NOT in this
# tuple), so it is never retried over REST — REST hits the same auth backend.
_BINARY_CONNECTION_ERRORS = (RouterOsApiConnectionError, OSError)


def _read_neighbors(
    router: LldpRouter,
    *,
    binary_reader: Callable[[LldpRouter], list[NeighborRow]] | None = None,
    rest_reader: Callable[[LldpRouter], list[NeighborRow]] | None = None,
) -> tuple[list[NeighborRow], Literal["binary", "rest"]]:
    """Dispatch a neighbor read: binary API (8728) first, REST (443) on fallback.

    Returns ``(neighbors, transport)`` where ``transport`` is ``"binary"`` or
    ``"rest"``. Tries the binary API; on a CONNECTION-class failure (see
    ``_BINARY_CONNECTION_ERRORS`` — timeout / refused / no route) falls back to
    the REST reader for the REST-only cores. Does NOT fall back on an AUTH
    failure (``RouterOsApiCommunicationError``: REST would fail identically) nor
    on ``SoftTimeLimitExceeded`` (which must reach the task's graceful handler);
    auth failures count as ``routers_failed``; soft limits fail the whole run. The
    sub-readers are injectable for tests. ``binary_reader`` returning ``[]`` for
    an empty neighbor table is a SUCCESS, not a fallback trigger."""
    binary_reader = binary_reader or _read_neighbors_via_binary_api
    rest_reader = rest_reader or _read_neighbors_via_rest
    try:
        return binary_reader(router), "binary"
    except SoftTimeLimitExceeded:
        raise
    except _BINARY_CONNECTION_ERRORS as exc:
        logger.info(
            "lldp_poll_binary_unreachable router=%s: %s -- falling back to REST(443)",
            router.name,
            _sanitize_exc(exc),
        )
    return rest_reader(router), "rest"


def poll_all(
    *,
    snapshot: LldpSnapshot,
    read_neighbors: NeighborReader | None = None,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
) -> LldpPoll:
    """Collect detached neighbor observations without database access.

    Iterates active ``routers`` rows and reads each one's ``/ip/neighbor`` via
    the transport dispatcher (:func:`_read_neighbors`): binary API on 8728 first
    (``via_binary_api``), falling back to REST on 443 (``via_rest``) for the
    REST-only cores whose 8728 is filtered. A router maps to its
    ``network_device_id``; a router without one is counted in
    ``skipped_no_device`` (it cannot anchor an edge). A router that is
    unreachable or errors mid-read is counted in ``routers_failed`` and skipped
    — it never aborts the run or prunes others' edges (2/25 routinely time out).
    Once ``time_budget_seconds`` of wall clock is spent, remaining routers are
    counted in ``skipped_time_budget`` and the run still reconciles what it saw.
    A ``SoftTimeLimitExceeded`` raised mid-poll propagates so the task's
    graceful timeout handler fires instead of running into the hard kill.

    Idempotent: edges are keyed by canonical device pair (NULL interfaces), so a
    re-run only bumps ``last_seen_at``.
    """
    read_neighbors = read_neighbors or _read_neighbors
    started = snapshot.started_at
    budget_logged = False
    by_name: dict[str, LldpDevice] = {}
    by_ip: dict[str, LldpDevice] = {}
    stripped: list[tuple[frozenset[str], LldpDevice]] = []
    for device in snapshot.devices:
        if not device.is_active:
            continue
        for label in (device.name, device.hostname):
            if normalized := _norm(label):
                by_name.setdefault(normalized, device)
            if tokens := _strip_tokens(label):
                stripped.append((tokens, device))
        if device.mgmt_ip:
            by_ip.setdefault(device.mgmt_ip, device)
    index = (by_name, by_ip, stripped)
    stats: Counter[str] = Counter(
        {
            "routers_polled": 0,
            "routers_failed": 0,
            "via_binary_api": 0,
            "via_rest": 0,
            "skipped_no_device": 0,
            "skipped_time_budget": 0,
            "neighbors_seen": 0,
            "created": 0,
            "updated": 0,
            "pruned": 0,
            "edges": 0,
            # Debug: how many edges each match strategy resolved this run.
            "matched_by_identity": 0,
            "matched_by_address": 0,
            "matched_by_stripped_identity": 0,
            # Canonical pairs left to an authoritative manual/other-source link.
            "skipped_manual_dup": 0,
        }
    )

    routers = snapshot.routers
    devices = {device.id: device for device in snapshot.devices}

    edges: dict = {}
    # NetworkDevice ids of routers we SUCCESSFULLY read this run. Only these
    # devices could have re-observed (or stopped observing) their neighbors, so
    # only their edges are eligible for pruning below. Routers that failed
    # (routers_failed) or were skipped (skipped_time_budget / no-device) never
    # enter this set, so edges whose only observer was unreachable this cycle
    # are left untouched instead of flapping active/inactive every run.
    polled_device_ids: set = set()
    for router in routers:
        local = (
            devices.get(router.network_device_id) if router.network_device_id else None
        )
        if local is None:
            stats["skipped_no_device"] += 1
            logger.info(
                "lldp_poll_skipped_no_device router=%s (no network_device_id)",
                router.name,
            )
            continue
        elapsed = time.monotonic() - started
        attempted_reads = stats["routers_polled"] + stats["routers_failed"]
        if attempted_reads > 0 and elapsed > time_budget_seconds:
            if not budget_logged:
                logger.warning(
                    "lldp_poll_time_budget_exhausted after %.0fs; "
                    "skipping remaining routers this run",
                    elapsed,
                )
                budget_logged = True
            stats["skipped_time_budget"] += 1
            continue
        try:
            result = read_neighbors(router)
        except SoftTimeLimitExceeded:
            # Celery's soft timeout must reach the task's graceful handler —
            # counting it as routers_failed would keep looping until the hard
            # time_limit SIGKILLs the worker.
            raise
        except Exception as exc:  # one unreachable router must not abort the run
            stats["routers_failed"] += 1
            logger.warning(
                "lldp_poll_router_failed router=%s: %s",
                router.name,
                _sanitize_exc(exc),
            )
            continue
        # The dispatcher reports the transport it used as ``(neighbors,
        # transport)``; a legacy/injected reader returning a bare list is
        # treated as the binary path so existing callers/tests are unaffected.
        if isinstance(result, tuple):
            neighbors, transport = result
        else:
            neighbors, transport = result, "binary"
        stats["routers_polled"] += 1
        stats["via_rest" if transport == "rest" else "via_binary_api"] += 1
        stats["neighbors_seen"] += len(neighbors)
        polled_device_ids.add(local.id)
        accumulate_edges(edges, local, neighbors, index, strategy_counter=stats)

    stats["edges"] = len(edges)
    return LldpPoll(
        snapshot=snapshot,
        edges=tuple(
            LldpEdge(
                source_device_id=edge["source_device_id"],
                target_device_id=edge["target_device_id"],
                medium=edge["medium"],
                observed_from=uuid.UUID(edge["metadata"]["observed_from"]),
                local_interface=edge["metadata"]["local_interface"],
                remote_identity=edge["metadata"]["remote_identity"],
                remote_board=edge["metadata"]["remote_board"],
            )
            for edge in edges.values()
        ),
        polled_device_ids=frozenset(polled_device_ids),
        stats=LldpStats(**dict(stats)),
    )


def _reconcile(session: Session, poll: LldpPoll) -> LldpStats:
    # Serialize LLDP runs and block inventory/manual-link edits only during the
    # short write phase. Table locks also cover inserts (row locks alone don't).
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(
            text("LOCK TABLE routers, jump_hosts, network_devices IN SHARE MODE")
        )
        session.execute(
            text("LOCK TABLE network_topology_links IN SHARE ROW EXCLUSIVE MODE")
        )
    current = read_snapshot(session, query=LldpReadQuery(poll.snapshot.observed_at))
    observed_at = poll.snapshot.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    newer_observation = any(
        link.source == SOURCE
        and link.last_seen_at is not None
        and (
            link.last_seen_at
            if link.last_seen_at.tzinfo is not None
            else link.last_seen_at.replace(tzinfo=UTC)
        )
        > observed_at
        for link in current.links
    )
    if (
        {row.id: row for row in current.routers}
        != {row.id: row for row in poll.snapshot.routers}
        or {row.id: row for row in current.devices}
        != {row.id: row for row in poll.snapshot.devices}
        or current.links != poll.snapshot.links
        or newer_observation
    ):
        raise DomainError(
            code="network.lldp_observations.stale_snapshot",
            message="LLDP inventory or topology changed during collection; poll discarded",
        )
    now = poll.snapshot.observed_at
    stats = Counter(poll.stats.to_dict())
    polled_device_ids = poll.polled_device_ids
    edges = {
        (edge.source_device_id, edge.target_device_id): edge for edge in poll.edges
    }

    # Upsert by canonical pair, scoped to our source (query-before-insert: the
    # 4-tuple unique constraint treats NULL interfaces as distinct, so we keep
    # one-row-per-pair in code).
    seen_pairs: set = set()
    for key, e in edges.items():
        seen_pairs.add(key)
        metadata = {
            "observed_from": str(e.observed_from),
            "local_interface": e.local_interface,
            "remote_identity": e.remote_identity,
            "remote_board": e.remote_board,
        }
        link = (
            session.query(NetworkTopologyLink)
            .filter(
                NetworkTopologyLink.source == SOURCE,
                NetworkTopologyLink.source_device_id == e.source_device_id,
                NetworkTopologyLink.target_device_id == e.target_device_id,
                NetworkTopologyLink.source_interface_id.is_(None),
                NetworkTopologyLink.target_interface_id.is_(None),
            )
            .first()
        )
        if link is None:
            # Operators model the abuja/lagos backbone by hand (TopologyLinks.create,
            # source='manual'/NULL, topology_group in {abuja-backbone,...}). Now that
            # the improved matcher can rediscover those same canonical pairs, do NOT
            # create a SECOND active row — leave the manual link authoritative. Check
            # BOTH endpoint orderings since manual links aren't canonicalized.
            a, b = e.source_device_id, e.target_device_id
            manual = (
                session.query(NetworkTopologyLink)
                .filter(
                    NetworkTopologyLink.is_active.is_(True),
                    or_(
                        NetworkTopologyLink.source != SOURCE,
                        NetworkTopologyLink.source.is_(None),
                    ),
                    or_(
                        and_(
                            NetworkTopologyLink.source_device_id == a,
                            NetworkTopologyLink.target_device_id == b,
                        ),
                        and_(
                            NetworkTopologyLink.source_device_id == b,
                            NetworkTopologyLink.target_device_id == a,
                        ),
                    ),
                )
                .first()
            )
            if manual is not None:
                stats["skipped_manual_dup"] += 1
                continue
            session.add(
                NetworkTopologyLink(
                    source_device_id=e.source_device_id,
                    target_device_id=e.target_device_id,
                    source=SOURCE,
                    medium=e.medium,
                    metadata_=metadata,
                    is_active=True,
                    discovered_at=now,
                    last_seen_at=now,
                )
            )
            stats["created"] += 1
        else:
            link.medium = e.medium
            link.metadata_ = metadata
            link.is_active = True
            link.last_seen_at = now
            stats["updated"] += 1
    session.flush()

    # Soft-prune our rows not seen this run — but ONLY when a router that could
    # have re-observed the edge was actually polled this run and didn't report
    # it. An edge whose only observing router failed or was skipped this cycle
    # is left active (it'll be re-verified once that router is reachable again),
    # otherwise the 2/25 routers that routinely time out would flap their edges
    # active/inactive on every run and churn the topology graph.
    pruned = 0
    for link in (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    ):
        if _canonical(link.source_device_id, link.target_device_id) in seen_pairs:
            continue
        observer = (link.metadata_ or {}).get("observed_from")
        observed_by_polled_router = (
            observer in {str(device_id) for device_id in polled_device_ids}
            if observer
            else {link.source_device_id, link.target_device_id} <= polled_device_ids
        )
        if observed_by_polled_router:
            link.is_active = False
            pruned += 1
    session.flush()

    stats["edges"] = len(edges)
    stats["pruned"] = pruned
    return LldpStats(**dict(stats))


def read_snapshot(session: Session, *, query: LldpReadQuery) -> LldpSnapshot:
    """Materialize all attributes, including the lazy jump-host relationship."""
    started_at = time.monotonic()
    devices = [
        LldpDevice(d.id, d.name, d.hostname, d.mgmt_ip, d.is_active)
        for d in session.query(NetworkDevice)
        .populate_existing()
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    ]
    device_ids = {device.id for device in devices}
    routers: list[LldpRouter] = []
    for router in (
        session.query(Router)
        .populate_existing()
        .filter(Router.is_active.is_(True))
        .all()
    ):
        if router.network_device_id and router.network_device_id not in device_ids:
            # Local devices were historically eligible even when inactive;
            # only the remote matching index is restricted to active nodes.
            local = session.get(NetworkDevice, router.network_device_id)
            if local is not None:
                devices.append(
                    LldpDevice(
                        local.id,
                        local.name,
                        local.hostname,
                        local.mgmt_ip,
                        local.is_active,
                    )
                )
                device_ids.add(local.id)
        jump = router.jump_host
        routers.append(
            LldpRouter(
                id=router.id,
                name=router.name,
                management_ip=router.management_ip,
                rest_api_port=router.rest_api_port,
                rest_api_username=router.rest_api_username,
                rest_api_password=router.rest_api_password,
                use_ssl=router.use_ssl,
                verify_tls=router.verify_tls,
                access_method=router.access_method,
                network_device_id=router.network_device_id,
                jump_host=LldpJumpHost(
                    jump.id,
                    jump.hostname,
                    jump.port,
                    jump.username,
                    jump.ssh_key,
                    jump.ssh_password,
                    jump.is_active,
                )
                if jump
                else None,
            )
        )
    links = tuple(
        LldpLinkState(
            link.id,
            link.source_device_id,
            link.target_device_id,
            link.source_interface_id,
            link.target_interface_id,
            link.source,
            link.medium,
            link.is_active,
            link.last_seen_at,
            json.dumps(link.metadata_, sort_keys=True),
        )
        for link in session.query(NetworkTopologyLink)
        .populate_existing()
        .order_by(NetworkTopologyLink.id)
        .all()
    )
    return LldpSnapshot(
        query.observed_at, tuple(routers), tuple(devices), links, started_at
    )


def reconcile_poll(session: Session, *, command: ReconcileLldpCommand) -> LldpStats:
    """Commit revalidated LLDP observations in one registered owner transaction."""

    def operation() -> LldpStats:
        result = _reconcile(session, command.poll)
        create_event_record(
            session,
            event=Event(
                event_type=EventType.lldp_observations_reconciled,
                event_id=command.context.command_id,
                actor=command.context.actor,
                payload={
                    "schema_version": 1,
                    "correlation_id": str(command.context.correlation_id),
                    "observed_at": command.poll.snapshot.observed_at.isoformat(),
                    "stats": result.to_dict(),
                },
            ),
            status=EventStatus.pending,
        )
        return result

    return execute_owner_command(
        session,
        definition=OwnerCommandDefinition(
            owner="network.lldp_observations",
            concern="LLDP adjacency observation reconciliation",
            name="reconcile_poll",
        ),
        context=command.context,
        operation=operation,
    )
