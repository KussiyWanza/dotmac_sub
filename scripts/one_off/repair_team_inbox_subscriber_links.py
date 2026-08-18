#!/usr/bin/env python3
"""Preview or apply exact Team Inbox conversation-to-Subscriber repairs.

Dry-run is the default. Apply requires the exact digest from a fresh preview,
an attributable approval reference, actor, reason, and named target. Output is
PII-free: only conversation and Subscriber UUIDs, resolution classes, and
counts are printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.team_inbox import InboxConversation
from app.services import team_inbox_channel_receive, team_inbox_contact_links

FINAL_CONFIRMATION = "APPLY_TEAM_INBOX_SUBSCRIBER_LINK_REPAIR"


@dataclass(frozen=True, slots=True)
class SubscriberLinkRepairItem:
    conversation_id: UUID
    subscriber_id: UUID
    channel_type: str
    normalized_contact_digest: str

    def canonical(self) -> dict[str, str]:
        return {
            "conversation_id": str(self.conversation_id),
            "subscriber_id": str(self.subscriber_id),
            "channel_type": self.channel_type,
            "normalized_contact_digest": self.normalized_contact_digest,
        }


@dataclass(frozen=True, slots=True)
class SubscriberLinkRepairPlan:
    items: tuple[SubscriberLinkRepairItem, ...]
    scanned: int
    ambiguous: int
    unmatched: int
    suppressed: int
    digest: str

    def public_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "scanned": self.scanned,
            "eligible_routes": len(self.items),
            "ambiguous": self.ambiguous,
            "unmatched": self.unmatched,
            "suppressed": self.suppressed,
            "items": [item.canonical() for item in self.items],
        }


def _digest(items: tuple[SubscriberLinkRepairItem, ...]) -> str:
    payload = json.dumps(
        [item.canonical() for item in items],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def build_plan(db: Session, *, limit: int) -> SubscriberLinkRepairPlan:
    rows = (
        db.query(InboxConversation)
        .filter(InboxConversation.subscriber_id.is_(None))
        .filter(InboxConversation.contact_address.isnot(None))
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.created_at.asc(), InboxConversation.id.asc())
        .limit(max(1, min(limit, 5000)))
        .all()
    )
    items_by_route: dict[tuple[str, str], SubscriberLinkRepairItem] = {}
    counts = {"ambiguous": 0, "unmatched": 0, "suppressed": 0}
    for conversation in rows:
        resolution = team_inbox_channel_receive.resolve_contact_context(
            db,
            channel_type=conversation.channel_type,
            contact_address=conversation.contact_address or "",
        )
        if resolution.subscriber_id is None:
            if resolution.status == "ambiguous":
                counts["ambiguous"] += 1
            elif resolution.status == "suppressed_inactive":
                counts["suppressed"] += 1
            else:
                counts["unmatched"] += 1
            continue
        route = (conversation.channel_type, resolution.normalized_contact)
        items_by_route.setdefault(
            route,
            SubscriberLinkRepairItem(
                conversation_id=conversation.id,
                subscriber_id=resolution.subscriber_id,
                channel_type=conversation.channel_type,
                normalized_contact_digest=hashlib.sha256(
                    resolution.normalized_contact.encode()
                ).hexdigest(),
            ),
        )
    items = tuple(
        sorted(
            items_by_route.values(),
            key=lambda item: (item.channel_type, str(item.conversation_id)),
        )
    )
    return SubscriberLinkRepairPlan(
        items=items,
        scanned=len(rows),
        ambiguous=counts["ambiguous"],
        unmatched=counts["unmatched"],
        suppressed=counts["suppressed"],
        digest=_digest(items),
    )


def apply_plan(
    db: Session,
    *,
    plan: SubscriberLinkRepairPlan,
    expected_digest: str,
    actor_person_id: UUID,
    reason: str,
    approval_reference: str,
) -> tuple[UUID, ...]:
    if plan.digest != expected_digest.strip():
        raise ValueError("Repair plan digest changed; run a fresh preview.")
    if not reason.strip() or not approval_reference.strip():
        raise ValueError("Reason and approval reference are required.")
    repaired: list[UUID] = []
    for item in plan.items:
        db.rollback()
        result = team_inbox_contact_links.link_conversation_contact_by_id_committed(
            db,
            conversation_id=item.conversation_id,
            subscriber_id=item.subscriber_id,
            linked_by_person_id=actor_person_id,
            note=(
                f"Approved historical Subscriber-link repair "
                f"{approval_reference.strip()}: {reason.strip()}"
            ),
        )
        repaired.append(item.conversation_id)
        repaired.extend(result.repaired_conversation_ids)
    return tuple(dict.fromkeys(repaired))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--approval-reference")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--target")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        plan = build_plan(db, limit=args.limit)
        report = plan.public_dict()
        if not args.apply:
            db.rollback()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if args.confirm != FINAL_CONFIRMATION:
            raise ValueError(f"Apply requires --confirm {FINAL_CONFIRMATION}")
        if not str(args.target or "").strip():
            raise ValueError("Apply requires an explicitly named --target.")
        repaired = apply_plan(
            db,
            plan=plan,
            expected_digest=str(args.expected_digest or ""),
            actor_person_id=UUID(str(args.actor or "")),
            reason=str(args.reason or ""),
            approval_reference=str(args.approval_reference or ""),
        )
        report.update(
            {
                "target": args.target,
                "applied": len(repaired),
                "repaired_conversation_ids": [str(item) for item in repaired],
            }
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
