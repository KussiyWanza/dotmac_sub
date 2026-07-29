"""Dry-run-first remediation for the July 20 CRM name-overwrite incident."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber
from app.services.customer_identity_normalization import (
    collapse_whitespace,
    customer_name_fingerprint,
    customer_name_signature,
    is_placeholder_name,
)
from app.services.web_customer_actions import approve_subscriber_name_correction

WINDOW_START = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 20, 16, 0, tzinfo=UTC)
REMEDIATION_MARKER = "crm_customer_name_remediation_digest"


@dataclass(frozen=True)
class NameChangeRecord:
    audit_event_id: UUID
    subscriber_id: UUID
    old_first_name: str | None
    old_last_name: str | None
    old_display_name: str | None
    new_first_name: str | None
    new_last_name: str | None
    new_display_name: str | None
    current_name_fingerprint: str
    restored_name_fingerprint: str
    row_fingerprint: str

    def manifest_row(self) -> dict[str, Any]:
        return {
            "subscriber_id": str(self.subscriber_id),
            "row_fingerprint": self.row_fingerprint,
        }


@dataclass(frozen=True)
class NameRemediationPlan:
    window_start: datetime
    window_end: datetime
    deployment_target: str
    selected_rows: tuple[NameChangeRecord, ...]
    review_rows: tuple[dict[str, str], ...]
    manifest: dict[str, Any]

    @property
    def digest(self) -> str:
        payload = dict(self.manifest)
        payload.pop("digest", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _current_name(subscriber: Subscriber) -> dict[str, str | None]:
    return {
        "first_name": collapse_whitespace(subscriber.first_name),
        "last_name": collapse_whitespace(subscriber.last_name),
        "display_name": collapse_whitespace(subscriber.display_name),
    }


def _name_snapshot(event: AuditEvent, *, side: str) -> dict[str, str | None]:
    changes = dict(event.metadata_ or {}).get("changes") or {}
    snapshot: dict[str, str | None] = {
        "first_name": None,
        "last_name": None,
        "display_name": None,
    }
    for field in snapshot:
        change = changes.get(field)
        if isinstance(change, dict):
            value = change.get(side)
            snapshot[field] = collapse_whitespace(value) if value is not None else None
    if not snapshot["display_name"]:
        joined = " ".join(
            part for part in (snapshot["first_name"], snapshot["last_name"]) if part
        )
        snapshot["display_name"] = joined or None
    return snapshot


def _row_fingerprint(
    *,
    subscriber_id: UUID,
    audit_event_ids: tuple[UUID, ...],
    current_name_fingerprint: str,
    restored_name_fingerprint: str,
) -> str:
    payload = {
        "contract_version": 1,
        "subscriber_id": str(subscriber_id),
        "audit_event_ids": [str(value) for value in audit_event_ids],
        "current_name_fingerprint": current_name_fingerprint,
        "restored_name_fingerprint": restored_name_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _first_non_placeholder_name(
    snapshot: dict[str, str | None],
) -> dict[str, str | None]:
    display = snapshot["display_name"]
    first = snapshot["first_name"]
    last = snapshot["last_name"]
    if display and not is_placeholder_name(display):
        if first and last:
            return snapshot
        name_parts = display.split(maxsplit=1)
        if len(name_parts) == 2:
            return {
                "first_name": name_parts[0],
                "last_name": name_parts[1],
                "display_name": display,
            }
    if first and last and not is_placeholder_name(f"{first} {last}"):
        return {
            "first_name": first,
            "last_name": last,
            "display_name": f"{first} {last}",
        }
    return {"first_name": None, "last_name": None, "display_name": None}


def _final_observed_placeholder(snapshot: dict[str, str | None]) -> bool:
    display = snapshot["display_name"]
    first = snapshot["first_name"]
    last = snapshot["last_name"]
    if display and is_placeholder_name(display):
        return True
    if first and last and is_placeholder_name(f"{first} {last}"):
        return True
    return False


def _load_audit_events(
    db: Session, *, window_start: datetime, window_end: datetime
) -> dict[UUID, list[AuditEvent]]:
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_type == "subscriber")
        .where(AuditEvent.action == "crm_customer_identity_update")
        .where(AuditEvent.occurred_at >= window_start)
        .where(AuditEvent.occurred_at < window_end)
        .order_by(AuditEvent.entity_id, AuditEvent.occurred_at.asc())
    ).all()
    grouped: dict[UUID, list[AuditEvent]] = defaultdict(list)
    for event in rows:
        if not event.entity_id:
            continue
        try:
            subscriber_id = UUID(event.entity_id)
        except ValueError:
            continue
        grouped[subscriber_id].append(event)
    return grouped


def build_name_remediation_plan(
    db: Session,
    *,
    deployment_target: str,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
) -> NameRemediationPlan:
    if not deployment_target.strip():
        raise ValueError("deployment_target is required")

    grouped = _load_audit_events(db, window_start=window_start, window_end=window_end)
    selected: list[NameChangeRecord] = []
    review_rows: list[dict[str, str]] = []

    for subscriber_id, events in sorted(grouped.items(), key=lambda item: str(item[0])):
        subscriber = db.get(Subscriber, subscriber_id)
        if subscriber is None:
            continue
        current_name = _current_name(subscriber)
        current_signature = customer_name_signature(
            current_name["first_name"],
            current_name["last_name"],
            current_name["display_name"],
        )
        current_fingerprint = customer_name_fingerprint(
            first_name=current_name["first_name"],
            last_name=current_name["last_name"],
            display_name=current_name["display_name"],
            party_id=subscriber.party_id,
        )

        final_snapshot = _name_snapshot(events[-1], side="new")
        restored_snapshot = _first_non_placeholder_name(
            _name_snapshot(events[0], side="old")
        )
        restored_signature = customer_name_signature(
            restored_snapshot["first_name"],
            restored_snapshot["last_name"],
            restored_snapshot["display_name"],
        )
        final_signature = customer_name_signature(
            final_snapshot["first_name"],
            final_snapshot["last_name"],
            final_snapshot["display_name"],
        )
        audit_event_ids = tuple(event.id for event in events)

        if (
            final_signature
            and current_signature == final_signature
            and _final_observed_placeholder(final_snapshot)
            and restored_signature
            and not is_placeholder_name(restored_signature)
        ):
            row_fingerprint = _row_fingerprint(
                subscriber_id=subscriber_id,
                audit_event_ids=audit_event_ids,
                current_name_fingerprint=current_fingerprint,
                restored_name_fingerprint=customer_name_fingerprint(
                    first_name=restored_snapshot["first_name"],
                    last_name=restored_snapshot["last_name"],
                    display_name=restored_snapshot["display_name"],
                    party_id=subscriber.party_id,
                ),
            )
            selected.append(
                NameChangeRecord(
                    audit_event_id=events[0].id,
                    subscriber_id=subscriber_id,
                    old_first_name=restored_snapshot["first_name"],
                    old_last_name=restored_snapshot["last_name"],
                    old_display_name=restored_snapshot["display_name"],
                    new_first_name=final_snapshot["first_name"],
                    new_last_name=final_snapshot["last_name"],
                    new_display_name=final_snapshot["display_name"],
                    current_name_fingerprint=current_fingerprint,
                    restored_name_fingerprint=customer_name_fingerprint(
                        first_name=restored_snapshot["first_name"],
                        last_name=restored_snapshot["last_name"],
                        display_name=restored_snapshot["display_name"],
                        party_id=subscriber.party_id,
                    ),
                    row_fingerprint=row_fingerprint,
                )
            )
        else:
            review_rows.append(
                {
                    "subscriber_id": str(subscriber_id),
                    "row_fingerprint": _row_fingerprint(
                        subscriber_id=subscriber_id,
                        audit_event_ids=audit_event_ids,
                        current_name_fingerprint=current_fingerprint,
                        restored_name_fingerprint=current_fingerprint,
                    ),
                }
            )

    manifest = {
        "contract_version": 1,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "deployment_target": deployment_target,
        "counts": {
            "audited_subscribers": len(grouped),
            "selected": len(selected),
            "review": len(review_rows),
        },
        "rows": [row.manifest_row() for row in selected],
        "review_rows": review_rows,
    }
    encoded = json.dumps(
        {key: value for key, value in manifest.items() if key != "digest"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest["digest"] = hashlib.sha256(encoded).hexdigest()
    return NameRemediationPlan(
        window_start=window_start,
        window_end=window_end,
        deployment_target=deployment_target,
        selected_rows=tuple(selected),
        review_rows=tuple(review_rows),
        manifest=manifest,
    )


def apply_name_remediation_plan(
    db: Session,
    plan: NameRemediationPlan,
    *,
    expected_digest: str,
    deployment_target: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    if plan.digest != expected_digest:
        raise ValueError("manifest digest does not match the selected plan")
    if deployment_target.strip() != plan.deployment_target.strip():
        raise ValueError("deployment_target does not match the selected plan")

    already_applied = True
    for row in plan.selected_rows:
        subscriber = db.get(Subscriber, row.subscriber_id)
        if subscriber is None:
            raise ValueError(f"subscriber {row.subscriber_id} is missing")
        marker = dict(subscriber.metadata_ or {}).get(REMEDIATION_MARKER)
        current_name = _current_name(subscriber)
        if (
            marker != plan.digest
            or customer_name_fingerprint(
                first_name=current_name["first_name"],
                last_name=current_name["last_name"],
                display_name=current_name["display_name"],
                party_id=subscriber.party_id,
            )
            != row.restored_name_fingerprint
        ):
            already_applied = False
            break
    if already_applied and plan.selected_rows:
        return {
            "status": "already_applied",
            "manifest_digest": plan.digest,
            "deployment_target": plan.deployment_target,
            "selected": len(plan.selected_rows),
        }

    savepoint = db.begin_nested()
    try:
        for row in plan.selected_rows:
            subscriber = (
                db.query(Subscriber)
                .filter(Subscriber.id == row.subscriber_id)
                .with_for_update()
                .one_or_none()
            )
            if subscriber is None:
                raise ValueError(f"subscriber {row.subscriber_id} is missing")
            if subscriber.party_id is not None:
                raise ValueError(
                    f"subscriber {row.subscriber_id} is party-bound and cannot be repaired"
                )
            current_name = _current_name(subscriber)
            current_fingerprint = customer_name_fingerprint(
                first_name=current_name["first_name"],
                last_name=current_name["last_name"],
                display_name=current_name["display_name"],
                party_id=subscriber.party_id,
            )
            if current_fingerprint == row.restored_name_fingerprint:
                metadata = dict(subscriber.metadata_ or {})
                metadata[REMEDIATION_MARKER] = plan.digest
                subscriber.metadata_ = metadata
                continue
            if current_fingerprint != row.current_name_fingerprint:
                raise ValueError(
                    f"subscriber {row.subscriber_id} drifted since plan generation"
                )

            updated = approve_subscriber_name_correction(
                db,
                subscriber_id=str(subscriber.id),
                first_name=row.old_first_name or "",
                last_name=row.old_last_name or "",
                display_name=row.old_display_name,
                expected_current_fingerprint=row.current_name_fingerprint,
                actor_id=actor_id,
                reason="July 20 CRM name remediation",
                manifest_digest=plan.digest,
                commit=False,
            )
            metadata = dict(updated.metadata_ or {})
            metadata[REMEDIATION_MARKER] = plan.digest
            updated.metadata_ = metadata
        db.flush()
        savepoint.commit()
        db.commit()
    except Exception:
        savepoint.rollback()
        raise

    return {
        "status": "applied",
        "manifest_digest": plan.digest,
        "deployment_target": plan.deployment_target,
        "selected": len(plan.selected_rows),
        "review": len(plan.review_rows),
    }
