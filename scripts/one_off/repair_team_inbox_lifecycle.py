"""Dry-run-first Team Inbox Lead identity and WhatsApp expiry repair.

Examples:
    poetry run python -m scripts.one_off.repair_team_inbox_lifecycle
    poetry run python -m scripts.one_off.repair_team_inbox_lifecycle --apply-lead-endpoints
    poetry run python -m scripts.one_off.repair_team_inbox_lifecycle --apply-expired-assignments

Neither apply mode merges Parties, resolves conversations, creates queue rows, or
changes Customer identity. Conflicting identity ownership is report-only.
"""

from __future__ import annotations

import argparse
import json

from app.services import team_inbox_contact_links, team_inbox_maintenance
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="operator:team-inbox-lifecycle-repair",
        scope="team-inbox:maintenance",
        reason=reason,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply-lead-endpoints", action="store_true")
    parser.add_argument("--apply-expired-assignments", action="store_true")
    args = parser.parse_args()

    with db_session_adapter.owner_command_session() as session:
        lead_result = team_inbox_contact_links.repair_inbox_lead_endpoints(
            session,
            team_inbox_contact_links.RepairInboxLeadEndpointsCommand(
                context=_context(
                    "repair reviewed Inbox Lead endpoint identity"
                    if args.apply_lead_endpoints
                    else "preview Inbox Lead endpoint identity repair"
                ),
                dry_run=not args.apply_lead_endpoints,
                limit=args.limit,
            ),
        )

    with db_session_adapter.owner_command_session() as session:
        expiry_result = team_inbox_maintenance.repair_expired_whatsapp_assignments(
            session,
            team_inbox_maintenance.RepairExpiredWhatsAppAssignmentsCommand(
                context=_context(
                    "release historical expired WhatsApp routing state"
                    if args.apply_expired_assignments
                    else "preview historical expired WhatsApp routing repair"
                ),
                dry_run=not args.apply_expired_assignments,
                limit=args.limit,
            ),
        )

    with db_session_adapter.session() as session:
        collision_result = team_inbox_contact_links.lead_identity_collision_diagnostics(
            session, limit=args.limit
        )

    payload = {
        "lead_endpoint_repair": {
            "mode": "apply" if args.apply_lead_endpoints else "dry_run",
            "examined": lead_result.examined,
            "already_correct": lead_result.already_correct,
            "safe_candidates": lead_result.safe_candidates,
            "repaired": lead_result.repaired,
            "conflicts": lead_result.conflicts,
            "errors": lead_result.errors,
            "findings": [
                {
                    "conversation": str(item.conversation_id),
                    "lead": str(item.lead_id),
                    "party": str(item.party_id),
                    "channel": item.channel_type,
                    "inbound_endpoint": item.inbound_endpoint,
                    "provider_account_scope": item.provider_account_scope,
                    "existing_party_contact_points": [
                        str(point_id) for point_id in item.existing_party_contact_points
                    ],
                    "conflict_state": item.disposition.value,
                    "proposed_action": item.proposed_action,
                }
                for item in lead_result.findings
            ],
        },
        "expired_whatsapp_repair": {
            "mode": "apply" if args.apply_expired_assignments else "dry_run",
            "examined": expiry_result.examined,
            "stale_assignments_found": (expiry_result.stale_assignments_found),
            "stale_queues_found": expiry_result.stale_queues_found,
            "assignments_released": expiry_result.assignments_released,
            "queues_cancelled": expiry_result.queues_cancelled,
            "already_correct": expiry_result.already_correct,
            "conflicts": expiry_result.conflicts,
            "errors": expiry_result.errors,
        },
        "lead_identity_collisions": {
            "examined_contact_points": collision_result.examined_contact_points,
            "collisions": [
                {
                    "channel": item.channel_type,
                    "normalized_endpoint": item.normalized_endpoint,
                    "provider": item.provider,
                    "provider_account_id": item.provider_account_id,
                    "external_subject_id": item.external_subject_id,
                    "party_ids": [str(value) for value in item.party_ids],
                    "lead_ids": [str(value) for value in item.lead_ids],
                    "disposition": item.disposition.value,
                }
                for item in collision_result.collisions
            ],
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
