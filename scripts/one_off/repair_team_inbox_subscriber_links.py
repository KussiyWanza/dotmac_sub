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
from app.services import team_inbox_contact_links
from app.services.owner_commands import CommandContext

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
    skipped: int
    errors: int
    digest: str

    def public_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "scanned": self.scanned,
            "eligible_conversations": len(self.items),
            "projected_linked": len(self.items),
            "linked": 0,
            "ambiguous": self.ambiguous,
            "unmatched": self.unmatched,
            "suppressed": self.suppressed,
            "skipped": self.skipped,
            "conflicts": 0,
            "errors": self.errors,
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
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.created_at.asc(), InboxConversation.id.asc())
        .limit(max(1, min(limit, 5000)))
        .all()
    )
    items: list[SubscriberLinkRepairItem] = []
    counts = {
        "ambiguous": 0,
        "unmatched": 0,
        "suppressed": 0,
        "skipped": 0,
        "errors": 0,
    }
    for conversation in rows:
        if not conversation.contact_address:
            counts["skipped"] += 1
            continue
        provider, provider_account_id, external_subject_id = (
            team_inbox_contact_links.conversation_provider_identity(db, conversation)
        )
        if conversation.channel_type in {
            "facebook_messenger",
            "instagram_dm",
        } and not (provider and provider_account_id and external_subject_id):
            counts["skipped"] += 1
            continue
        try:
            resolution = team_inbox_contact_links.resolve_contact_context(
                db,
                team_inbox_contact_links.ContactResolutionQuery(
                    channel_type=conversation.channel_type,
                    contact_address=conversation.contact_address,
                    contact_name=str(
                        (conversation.metadata_ or {}).get("contact_name") or ""
                    )
                    or None,
                    provider=provider,
                    provider_account_id=provider_account_id,
                    external_subject_id=external_subject_id,
                ),
            )
        except Exception:
            counts["errors"] += 1
            continue
        if resolution.subscriber_id is None:
            if (
                resolution.status
                is team_inbox_contact_links.ContactResolutionStatus.ambiguous
            ):
                counts["ambiguous"] += 1
            elif (
                resolution.status
                is team_inbox_contact_links.ContactResolutionStatus.suppressed_inactive
            ):
                counts["suppressed"] += 1
            else:
                counts["unmatched"] += 1
            continue
        assert resolution.normalized_contact is not None
        items.append(
            SubscriberLinkRepairItem(
                conversation_id=conversation.id,
                subscriber_id=resolution.subscriber_id,
                channel_type=conversation.channel_type,
                normalized_contact_digest=hashlib.sha256(
                    resolution.normalized_contact.encode()
                ).hexdigest(),
            ),
        )
    sorted_items = tuple(
        sorted(
            items,
            key=lambda item: (item.channel_type, str(item.conversation_id)),
        )
    )
    return SubscriberLinkRepairPlan(
        items=sorted_items,
        scanned=len(rows),
        ambiguous=counts["ambiguous"],
        unmatched=counts["unmatched"],
        suppressed=counts["suppressed"],
        skipped=counts["skipped"],
        errors=counts["errors"],
        digest=_digest(sorted_items),
    )


def apply_plan(
    db: Session,
    *,
    plan: SubscriberLinkRepairPlan,
    expected_digest: str,
    actor_person_id: UUID,
    reason: str,
    approval_reference: str,
) -> dict[str, object]:
    if plan.digest != expected_digest.strip():
        raise ValueError("Repair plan digest changed; run a fresh preview.")
    if not reason.strip() or not approval_reference.strip():
        raise ValueError("Reason and approval reference are required.")
    repaired: list[UUID] = []
    counts = {
        "linked": 0,
        "ambiguous": 0,
        "unmatched": 0,
        "skipped": 0,
        "conflicts": 0,
        "errors": 0,
    }
    for item in plan.items:
        db.rollback()
        try:
            result = team_inbox_contact_links.repair_conversation_customer_committed(
                db,
                team_inbox_contact_links.RepairConversationCustomerCommand(
                    context=CommandContext.system(
                        actor=f"person:{actor_person_id}",
                        scope="team-inbox:subscriber-link-repair",
                        reason=(
                            f"Approved repair {approval_reference.strip()}: "
                            f"{reason.strip()}"
                        ),
                        idempotency_key=f"team-inbox-link-repair:{item.conversation_id}",
                    ),
                    conversation_id=item.conversation_id,
                    reason=(
                        f"Approved repair {approval_reference.strip()}: "
                        f"{reason.strip()}"
                    ),
                    expected_subscriber_id=item.subscriber_id,
                ),
            )
        except Exception:
            db.rollback()
            counts["errors"] += 1
            continue
        if result.status is team_inbox_contact_links.AutomaticCustomerLinkStatus.linked:
            counts["linked"] += 1
            repaired.append(item.conversation_id)
        elif (
            result.status
            is team_inbox_contact_links.AutomaticCustomerLinkStatus.already_linked
        ):
            counts["skipped"] += 1
        elif (
            result.status
            is team_inbox_contact_links.AutomaticCustomerLinkStatus.conflict
        ):
            counts["conflicts"] += 1
        elif (
            result.resolution.status
            is team_inbox_contact_links.ContactResolutionStatus.ambiguous
        ):
            counts["ambiguous"] += 1
        else:
            counts["unmatched"] += 1
    return {**counts, "repaired_conversation_ids": [str(item) for item in repaired]}


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
        applied = apply_plan(
            db,
            plan=plan,
            expected_digest=str(args.expected_digest or ""),
            actor_person_id=UUID(str(args.actor or "")),
            reason=str(args.reason or ""),
            approval_reference=str(args.approval_reference or ""),
        )
        report.update({"target": args.target, **applied})
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
