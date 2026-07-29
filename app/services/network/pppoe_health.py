"""PPPoE connectivity health classification for the ONT fleet.

Classifies each online ONT into a PPPoE health category based on
credential state, ACS registration, and observed WAN IP. Used by
the ONT Fleet page to surface connectivity issues for NOC operators.

This module is a **detector only** — it classifies and never repairs.
``CATEGORY_CREDENTIAL_MISMATCH`` (an ONT dialing something other than the
subscriber's authoritative access credential) is repaired by
``app.services.cpe_dialer_credential_reconcile``, which owns the derived CPE
dialer projection and runs inside the scheduled ONT reconcile sweep. Note the
comparison here is username-only, so it is deliberately weaker than the
reconciler's keyed fingerprint over the full ``(username, secret)`` pair: a
row reading OK here can still be repaired there.

Credential lookup is injected via
:class:`~app.services.network._credentials.PppoeCredentialProvider`
so this module never imports from the subscription/catalog domain.
Callers that have an ``AccessCredential``-backed store should wire up
``app.services.network_credential_bridge.AccessCredentialAdapter``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select

from app.models.network import OntAssignment, OntUnit
from app.models.tr069 import Tr069CpeDevice
from app.services.common import coerce_uuid
from app.services.network._credentials import (
    PppoeCredential,
    PppoeCredentialProvider,
)
from app.services.network.ont_desired_config import (
    desired_config_column,
    get_desired_config_value,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Health categories
# ---------------------------------------------------------------------------

CATEGORY_OK = "ok"
CATEGORY_NO_CREDENTIAL = "no_credential"
CATEGORY_NOT_IN_ACS = "not_in_acs"
CATEGORY_CREDENTIAL_MISMATCH = "credential_mismatch"
CATEGORY_NO_WAN_IP = "no_wan_ip"
CATEGORY_BRIDGE_MODE = "bridge_mode"
CATEGORY_UNASSIGNED = "unassigned"

ISSUE_CATEGORIES = frozenset(
    {
        CATEGORY_NO_CREDENTIAL,
        CATEGORY_NOT_IN_ACS,
        CATEGORY_CREDENTIAL_MISMATCH,
        CATEGORY_NO_WAN_IP,
    }
)

CATEGORY_DISPLAY: dict[str, str] = {
    CATEGORY_OK: "OK",
    CATEGORY_NO_CREDENTIAL: "No Credential",
    CATEGORY_NOT_IN_ACS: "Not in ACS",
    CATEGORY_CREDENTIAL_MISMATCH: "Mismatch",
    CATEGORY_NO_WAN_IP: "No WAN IP",
    CATEGORY_BRIDGE_MODE: "Bridge/DHCP",
    CATEGORY_UNASSIGNED: "Unassigned",
}

# Tailwind badge classes — full strings to survive PurgeCSS.
CATEGORY_CLASSES: dict[str, str] = {
    CATEGORY_OK: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    CATEGORY_NO_CREDENTIAL: "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    CATEGORY_NOT_IN_ACS: "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    CATEGORY_CREDENTIAL_MISMATCH: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
    CATEGORY_NO_WAN_IP: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
    CATEGORY_BRIDGE_MODE: "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-200",
    CATEGORY_UNASSIGNED: "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-400",
}


@dataclass(frozen=True, slots=True)
class PppoeHealthInfo:
    """Classification result for a single ONT."""

    category: str
    category_display: str
    category_class: str
    credential_username: str | None
    ont_pppoe_username: str | None
    has_tr069: bool
    has_wan_ip: bool


# ---------------------------------------------------------------------------
# Core query builder (shared by all public methods)
# ---------------------------------------------------------------------------


def _health_base_query() -> Any:
    """Build the base SELECT + FROM with ONT/assignment/TR-069 joins.

    Credential data is intentionally NOT joined here — it's fetched via
    :class:`PppoeCredentialProvider` so the network domain does not have
    to import the catalog ORM model.

    Returns the select statement so callers can add WHERE/GROUP BY clauses.
    """
    stmt = (
        select(
            OntUnit.id.label("ont_id"),
            desired_config_column(OntUnit).label("desired_config"),
            OntAssignment.subscriber_id.label("subscriber_id"),
            OntUnit.observed_wan_ip.label("observed_wan_ip"),
            Tr069CpeDevice.id.label("tr069_device_id"),
        )
        .outerjoin(
            OntAssignment,
            and_(
                OntAssignment.ont_unit_id == OntUnit.id,
                OntAssignment.active.is_(True),
            ),
        )
        .outerjoin(
            Tr069CpeDevice,
            and_(
                Tr069CpeDevice.ont_unit_id == OntUnit.id,
                Tr069CpeDevice.is_active.is_(True),
            ),
        )
    )
    return stmt


def _row_pppoe_username(row: Any) -> str | None:
    config = row._mapping.get("desired_config") if hasattr(row, "_mapping") else None
    if config is None:
        config = getattr(row, "desired_config", None)
    config = config if isinstance(config, dict) else {}
    username = get_desired_config_value(config, "wan", "pppoe_username")
    return str(username or "").strip() or None


_NO_WAN_IP_VALUES = ("", "0.0.0.0")  # nosec B104  # noqa: S104


def _classify_row(
    subscriber_id: object,
    credential: PppoeCredential | None,
    ont_pppoe_username: str | None,
    observed_wan_ip: str | None,
    tr069_device_id: object,
) -> str:
    """Derive the PPPoE health category from a single joined row."""
    has_assignment = subscriber_id is not None
    has_credential = credential is not None
    has_tr069 = tr069_device_id is not None
    has_wan_ip = bool(observed_wan_ip) and observed_wan_ip not in _NO_WAN_IP_VALUES

    if not has_assignment:
        return CATEGORY_UNASSIGNED

    if not has_credential:
        if has_wan_ip:
            return CATEGORY_BRIDGE_MODE
        return CATEGORY_NO_CREDENTIAL

    # Has credential — check for mismatch
    assert credential is not None  # for type checker  # noqa: S101
    if (
        ont_pppoe_username
        and credential.username
        and ont_pppoe_username != credential.username
    ):
        return CATEGORY_CREDENTIAL_MISMATCH

    if has_wan_ip:
        return CATEGORY_OK

    if not has_tr069:
        return CATEGORY_NOT_IN_ACS

    return CATEGORY_NO_WAN_IP


def _build_info(
    category: str,
    credential_username: str | None,
    ont_pppoe_username: str | None,
    tr069_device_id: object,
    observed_wan_ip: str | None,
) -> PppoeHealthInfo:
    return PppoeHealthInfo(
        category=category,
        category_display=CATEGORY_DISPLAY.get(category, category),
        category_class=CATEGORY_CLASSES.get(
            category, CATEGORY_CLASSES[CATEGORY_UNASSIGNED]
        ),
        credential_username=credential_username,
        ont_pppoe_username=ont_pppoe_username,
        has_tr069=tr069_device_id is not None,
        has_wan_ip=bool(observed_wan_ip),
    )


def _collect_subscriber_ids(rows: list[Any]) -> list[UUID]:
    """Extract unique, non-null subscriber ids from a result set."""
    seen: dict[Any, None] = {}
    for row in rows:
        sid = row.subscriber_id
        if sid is not None and sid not in seen:
            seen[sid] = None
    # Keys are whatever SQLAlchemy returned — usually UUID instances.
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PppoeHealthClassifier:
    """PPPoE connectivity health classification for the ONT fleet.

    Parameters
    ----------
    credentials:
        Provider used to look up access credentials for assigned
        subscribers. When ``None`` (standalone mode) every assigned
        ONT is treated as having no credential, so the classifier
        still works in deployments where the catalog/subscription
        module is not wired up.
    """

    def __init__(self, credentials: PppoeCredentialProvider | None = None):
        self._credentials = credentials

    def _load_credentials(self, rows: list[Any]) -> dict[Any, PppoeCredential]:
        """Bulk-load credentials for every subscriber in ``rows``."""
        if self._credentials is None:
            return {}
        subscriber_ids = _collect_subscriber_ids(rows)
        if not subscriber_ids:
            return {}
        return dict(self._credentials.get_active_by_subscriber_ids(subscriber_ids))

    def classify_fleet(
        self,
        db: Session,
        ont_ids: list[str],
    ) -> dict[str, PppoeHealthInfo]:
        """Classify PPPoE health for a batch of ONTs.

        Args:
            db: Database session.
            ont_ids: OntUnit IDs to classify (typically one page of results).

        Returns:
            Dict keyed by ONT ID string → PppoeHealthInfo.
        """
        if not ont_ids:
            return {}

        stmt = _health_base_query().where(OntUnit.id.in_(ont_ids))
        rows = db.execute(stmt).all()

        credentials_by_sub = self._load_credentials(list(rows))

        result: dict[str, PppoeHealthInfo] = {}
        for row in rows:
            ont_id_str = str(row.ont_id)
            ont_pppoe_username = _row_pppoe_username(row)
            credential = (
                credentials_by_sub.get(row.subscriber_id)
                if row.subscriber_id is not None
                else None
            )
            category = _classify_row(
                row.subscriber_id,
                credential,
                ont_pppoe_username,
                row.observed_wan_ip,
                row.tr069_device_id,
            )
            result[ont_id_str] = _build_info(
                category,
                credential.username if credential else None,
                ont_pppoe_username,
                row.tr069_device_id,
                row.observed_wan_ip,
            )
        return result

    def count_issues(
        self,
        db: Session,
        *,
        olt_id: str | None = None,
    ) -> int:
        """Count online, assigned ONTs with a PPPoE issue.

        This powers the stat card number. Only counts ONTs that are online
        and assigned to a subscriber — unassigned ONTs are not issues.
        """
        issue_ids = self.list_ont_ids_by_health(db, "issues", olt_id=olt_id)
        return len(issue_ids)

    def list_ont_ids_by_health(
        self,
        db: Session,
        category: str,
        *,
        olt_id: str | None = None,
    ) -> list[str]:
        """Return ONT IDs matching a specific PPPoE health category.

        When category is ``"issues"``, returns all ONTs with any issue.
        """
        from app.services.network.ont_status import effective_ont_online_clause

        stmt = _health_base_query().where(
            effective_ont_online_clause(), OntUnit.is_active.is_(True)
        )

        if olt_id:
            from app.models.network import PonPort

            stmt = stmt.outerjoin(
                PonPort, PonPort.id == OntAssignment.pon_port_id
            ).where(
                func.coalesce(PonPort.olt_id, OntUnit.olt_device_id)
                == coerce_uuid(olt_id)
            )

        rows = db.execute(stmt).all()
        credentials_by_sub = self._load_credentials(list(rows))

        target_categories = ISSUE_CATEGORIES if category == "issues" else {category}

        matching_ids: list[str] = []
        for row in rows:
            ont_pppoe_username = _row_pppoe_username(row)
            credential = (
                credentials_by_sub.get(row.subscriber_id)
                if row.subscriber_id is not None
                else None
            )
            cat = _classify_row(
                row.subscriber_id,
                credential,
                ont_pppoe_username,
                row.observed_wan_ip,
                row.tr069_device_id,
            )
            if cat in target_categories:
                matching_ids.append(str(row.ont_id))

        return matching_ids
