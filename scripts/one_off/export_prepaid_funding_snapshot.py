"""Export a sealed, independently reconstructed prepaid funding manifest.

This command is the bridge between the billing alignment replay and the final
prepaid funding reconstruction owner. It does not decide who is prepaid, how
much funding is required, whether an account is shielded, or what access
consequence follows:

* ``financial.prepaid_enforcement`` selects the candidate cohort;
* the alignment replay reconstructs available funding from the final Splynx
  position plus proven post-legacy facts;
* the reconstruction owner verifies and materializes the sealed baseline;
* enforcement services apply profile, activation, shield, health, and lifecycle
  policy from config-owned inputs.

The export is complete-or-error. Every migrated candidate must have one frozen
source row whose active transaction net reconciles to the final Splynx
position; a complete empty transaction set is zero. A native account created
after the fixed handoff has an explicit zero history component plus canonical
native facts. Missing, duplicated, malformed, or unreconciled source evidence
aborts the whole artifact; there is no customer-level unknown or quarantine
result.

Safety: this command has no apply mode, sets its PostgreSQL transaction read
only, requires an explicitly approved primary override for an ephemeral restore,
and always rolls the session back.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import display_format
from app.services.billing.splynx_history_opening import (
    SplynxHistoryOpeningQuery,
    resolve_splynx_history_opening_targets,
)
from app.services.prepaid_enforcement_planner import (
    candidate_prepaid_funding_account_ids,
)
from app.services.prepaid_funding_attestation import (
    RECONSTRUCTION_MANIFEST_SCHEMA,
    candidate_cohort_sha256,
    canonical_payload_sha256,
    sign_prepaid_funding_manifest,
)
from app.services.secrets import is_openbao_ref, resolve_secret
from scripts.one_off.adjudicate_prepaid_funding_gaps import (
    ACTION_PLAN_SCHEMA,
    NO_PAID_THROUGH_DUE_IMMEDIATELY,
    NO_PAID_THROUGH_REASON,
)
from scripts.one_off.billing_alignment_audit import (
    LEGACY_FINANCIAL_REPLAY_AT,
    _configure_read_only_session,
)

_ACTION_PLAN_FIELDS = {
    "schema",
    "blocker_manifest_sha256",
    "candidate_cohort_sha256",
    "review_id",
    "reviewed_by",
    "reviewed_at",
    "status",
    "action_count",
    "disposition_counts",
    "actions",
}
_NO_PAID_THROUGH_ACTION_FIELDS = {
    "account_id",
    "reason",
    "disposition",
    "evidence_ref",
    "action_owner",
    "next_action",
}
_READY_ACTION_STATUS = "reviewed_no_paid_through_decisions_ready_for_replay"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("snapshot timestamp must include a timezone offset")
    return value.astimezone(UTC)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _payload_sha256(payload: dict[str, Any]) -> str:
    return canonical_payload_sha256(payload)


@dataclass(frozen=True)
class FundingSnapshotExport:
    captured_at: datetime
    source: str
    currency: str
    candidate_ids: tuple[str, ...]
    positions: dict[str, Decimal]
    incomplete: dict[str, tuple[str, ...]]
    missing_baseline: tuple[str, ...]
    adjudication_sha256: str | None = None
    service_cycle_gaps: tuple[Any, ...] = ()

    @property
    def ready(self) -> bool:
        return not (self.incomplete or self.missing_baseline)

    @property
    def quarantined_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.incomplete) | set(self.missing_baseline)))

    @property
    def enforceable_ids(self) -> tuple[str, ...]:
        quarantined = set(self.quarantined_ids)
        return tuple(
            account_id
            for account_id in self.candidate_ids
            if account_id not in quarantined
        )

    def funding_payload(self) -> dict[str, Any]:
        if not self.ready:
            raise ValueError(
                "funding reconstruction is blocked by incomplete provenance"
            )
        blocker_manifest = self.blocker_manifest_payload()
        return {
            "schema": RECONSTRUCTION_MANIFEST_SCHEMA,
            "source": self.source,
            "captured_at": self.captured_at.isoformat().replace("+00:00", "Z"),
            "currency": self.currency,
            "candidate_accounts": len(self.candidate_ids),
            "candidate_cohort_sha256": blocker_manifest["candidate_cohort_sha256"],
            "blocker_manifest_sha256": _payload_sha256(blocker_manifest),
            "blocker_count": len(self.quarantined_ids),
            "blocker_manifest": blocker_manifest,
            "accounts": [
                {
                    "account_id": account_id,
                    "available_balance": _money(self.positions[account_id]),
                }
                for account_id in self.candidate_ids
            ],
        }

    def sealed_funding_payload(
        self,
        *,
        private_key_pem: str,
        signed_at: datetime | None = None,
    ) -> dict[str, Any]:
        return sign_prepaid_funding_manifest(
            self.funding_payload(),
            private_key_pem=private_key_pem,
            signed_at=signed_at,
        )

    def blocker_manifest_payload(self) -> dict[str, Any]:
        blockers = {
            (account_id, reason)
            for account_id, reasons in self.incomplete.items()
            for reason in reasons
        }
        blockers.update(
            (account_id, "missing_source_baseline")
            for account_id in self.missing_baseline
        )
        return {
            "schema": "dotmac.prepaid_funding_blockers.v1",
            "source": self.source,
            "captured_at": self.captured_at.isoformat().replace("+00:00", "Z"),
            "financial_handoff_at": LEGACY_FINANCIAL_REPLAY_AT.isoformat().replace(
                "+00:00", "Z"
            ),
            "currency": self.currency,
            "candidate_accounts": len(self.candidate_ids),
            "candidate_cohort_sha256": candidate_cohort_sha256(self.candidate_ids),
            "blockers": [
                {"account_id": account_id, "reason": reason}
                for account_id, reason in sorted(blockers)
            ],
        }

    @property
    def blocker_manifest_sha256(self) -> str:
        return _payload_sha256(self.blocker_manifest_payload())

    def diagnostics_payload(self) -> dict[str, Any]:
        reason_counts = Counter(
            reason for reasons in self.incomplete.values() for reason in reasons
        )
        blocker_manifest = self.blocker_manifest_payload()
        return {
            "source": self.source,
            "captured_at": self.captured_at.isoformat().replace("+00:00", "Z"),
            "currency": self.currency,
            "ready": self.ready,
            "candidate_accounts": len(self.candidate_ids),
            "reconstructed_accounts": len(self.positions),
            "enforceable_accounts": len(self.enforceable_ids),
            "quarantined_accounts": len(self.quarantined_ids),
            "blocker_counts": {
                "incomplete_replay": len(self.incomplete),
                "missing_source_baseline": len(self.missing_baseline),
            },
            "incomplete_reason_counts": dict(sorted(reason_counts.items())),
            "adjudication_sha256": self.adjudication_sha256,
            "blocker_manifest_sha256": _payload_sha256(blocker_manifest),
            "blocker_manifest": blocker_manifest,
            "blockers": {
                "incomplete_replay": [
                    {"account_id": account_id, "reasons": list(reasons)}
                    for account_id, reasons in sorted(self.incomplete.items())
                ],
                "missing_source_baseline": list(self.missing_baseline),
            },
        }


def build_prepaid_funding_snapshot(
    db: Session,
    *,
    snapshot_at: datetime,
    source: str,
) -> FundingSnapshotExport:
    """Build one complete history-derived candidate snapshot.

    The frozen Splynx transaction net is the source opening at the legacy
    handoff. Canonical Sub-native facts advance it to ``snapshot_at``. Any
    source-integrity defect raises and prevents an artifact; no customer is
    classified as unknown or silently assigned a fallback.
    """
    captured_at = _as_utc(snapshot_at)
    source_label = source.strip()
    if not source_label:
        raise ValueError("source label must not be empty")

    candidate_ids = tuple(
        sorted(
            (str(value) for value in candidate_prepaid_funding_account_ids(db)),
            key=str,
        )
    )
    snapshot = resolve_splynx_history_opening_targets(
        db,
        SplynxHistoryOpeningQuery(
            account_ids=tuple(UUID(value) for value in candidate_ids),
            currency=display_format.default_currency(db),
            native_after=LEGACY_FINANCIAL_REPLAY_AT,
            position_at=captured_at,
        ),
    )
    positions = {str(row.account_id): row.target_position for row in snapshot.rows}
    effective_source = (
        f"{source_label};splynx-history-sha256={snapshot.source_fingerprint}"
    )
    if len(effective_source) > 240:
        raise ValueError("history-derived reconstruction source exceeds 240 characters")
    return FundingSnapshotExport(
        captured_at=captured_at,
        source=effective_source,
        currency=snapshot.currency,
        candidate_ids=candidate_ids,
        positions=positions,
        incomplete={},
        missing_baseline=(),
        service_cycle_gaps=(),
    )


def apply_reviewed_no_paid_through_actions(
    export: FundingSnapshotExport,
    action_plan: dict[str, Any],
) -> FundingSnapshotExport:
    """Resolve one exact reviewed no-payment blocker cohort without changing money."""
    if set(action_plan) != _ACTION_PLAN_FIELDS:
        raise ValueError("reviewed gap action plan fields are incomplete or unexpected")
    if action_plan.get("schema") != ACTION_PLAN_SCHEMA:
        raise ValueError("unsupported reviewed gap action plan schema")
    if action_plan.get("status") != _READY_ACTION_STATUS:
        raise ValueError("gap action plan is not ready for no-paid-through replay")

    raw_blockers = export.blocker_manifest_payload()
    if action_plan.get("blocker_manifest_sha256") != _payload_sha256(raw_blockers):
        raise ValueError("gap action plan does not match the current blocker manifest")
    if (
        action_plan.get("candidate_cohort_sha256")
        != raw_blockers["candidate_cohort_sha256"]
    ):
        raise ValueError("gap action plan does not match the current candidate cohort")

    actions = action_plan.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("gap action plan must contain reviewed actions")
    if action_plan.get("action_count") != len(actions):
        raise ValueError("gap action count does not match its actions")
    if action_plan.get("disposition_counts") != {
        NO_PAID_THROUGH_DUE_IMMEDIATELY: len(actions)
    }:
        raise ValueError("gap action plan contains another disposition")

    expected = {
        (str(item["account_id"]), str(item["reason"]))
        for item in raw_blockers["blockers"]
    }
    reviewed: set[tuple[str, str]] = set()
    for action in actions:
        if (
            not isinstance(action, dict)
            or set(action) != _NO_PAID_THROUGH_ACTION_FIELDS
        ):
            raise ValueError("reviewed no-paid-through action fields are invalid")
        if (
            action.get("disposition") != NO_PAID_THROUGH_DUE_IMMEDIATELY
            or action.get("reason") != NO_PAID_THROUGH_REASON
            or action.get("action_owner") != "financial.prepaid_funding_reconstruction"
            or action.get("next_action")
            != "preserve_opening_funding_and_mark_service_due_immediately"
        ):
            raise ValueError("reviewed gap action is not a no-paid-through decision")
        evidence_ref = str(action.get("evidence_ref") or "").strip()
        if not evidence_ref or len(evidence_ref) > 200:
            raise ValueError("reviewed gap action evidence_ref is invalid")
        key = (str(action.get("account_id")), str(action.get("reason")))
        if key in reviewed:
            raise ValueError("reviewed gap action is duplicated")
        reviewed.add(key)
    if reviewed != expected:
        raise ValueError("reviewed gap actions must cover the exact blocker set")

    action_hash = _payload_sha256(action_plan)
    source = f"{export.source};gap-actions-sha256={action_hash}"
    if len(source) > 240:
        raise ValueError("adjudicated reconstruction source exceeds 240 characters")
    incomplete = {
        account_id: tuple(
            reason for reason in reasons if reason != NO_PAID_THROUGH_REASON
        )
        for account_id, reasons in export.incomplete.items()
    }
    incomplete = {
        account_id: reasons for account_id, reasons in incomplete.items() if reasons
    }
    return replace(
        export,
        source=source,
        incomplete=incomplete,
        adjudication_sha256=action_hash,
    )


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _require_ephemeral_postgres(db: Session) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        raise RuntimeError("export requires the isolated PostgreSQL audit restore")
    if os.getenv("BILLING_AUDIT_EPHEMERAL") != "1":
        raise RuntimeError("set BILLING_AUDIT_EPHEMERAL=1 for the isolated audit DB")
    database_name = str(db.scalar(text("SELECT current_database()")) or "")
    if not database_name.endswith("_audit"):
        raise RuntimeError(
            "refusing non-audit database; current_database() must end in _audit"
        )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return _as_utc(parsed)


def _resolve_signing_key(reference: str | None) -> str:
    normalized = str(reference or "").strip()
    if not is_openbao_ref(normalized):
        raise RuntimeError(
            "ready export requires --signing-key-ref with an OpenBao reference"
        )
    resolved = str(resolve_secret(normalized) or "").strip()
    if not resolved:
        raise RuntimeError("prepaid reconstruction signing key is empty")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-at", type=_parse_timestamp, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--blockers-out", type=Path)
    parser.add_argument(
        "--signing-key-ref",
        help="OpenBao reference to the Ed25519 private signing key",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=60000,
        help="PostgreSQL timeout per query (default: 60000)",
    )
    parser.add_argument(
        "--allow-primary",
        action="store_true",
        help="allow the explicitly approved ephemeral audit primary",
    )
    args = parser.parse_args()
    if args.statement_timeout_ms <= 0:
        parser.error("--statement-timeout-ms must be greater than zero")

    db = SessionLocal()
    try:
        _require_ephemeral_postgres(db)
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        export = build_prepaid_funding_snapshot(
            db,
            snapshot_at=args.snapshot_at,
            source=args.source,
        )
        diagnostics = export.diagnostics_payload()
        print(
            json.dumps(
                {
                    key: value
                    for key, value in diagnostics.items()
                    if key not in {"blockers", "blocker_manifest"}
                },
                indent=2,
                sort_keys=True,
            )
        )
        if args.blockers_out is not None:
            _write_json(args.blockers_out, diagnostics, overwrite=args.overwrite)
        if not export.ready:
            print("BLOCKED - no sealed funding reconstruction was written")
            return 2
        sealed = export.sealed_funding_payload(
            private_key_pem=_resolve_signing_key(args.signing_key_ref),
        )
        _write_json(args.out, sealed, overwrite=args.overwrite)
        print(f"Sealed funding reconstruction written: {args.out}")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
