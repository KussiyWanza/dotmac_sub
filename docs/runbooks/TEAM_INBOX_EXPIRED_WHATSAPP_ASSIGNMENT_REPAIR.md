# Expired WhatsApp Assignment Repair

This operation repairs historical Team Inbox rows where the canonical WhatsApp
customer-service window is expired but an active assignment remains. It never
resolves a conversation, changes Customer/Lead identity, sends traffic, or
creates a queue record.

## Preconditions

1. Deploy the schema migration and application code to the selected environment.
   The schedule stores a one-time `managed_after` rollout watermark, so the
   recurring worker handles only windows expiring after activation.
2. Confirm the expiry scheduler is healthy and has a timezone-aware
   `managed_after` keyword argument.
3. Obtain explicit approval before applying on production.
4. Run from a non-deployment operator shell configured for the target database.

## Preview

```bash
poetry run python -m scripts.one_off.repair_expired_whatsapp_assignments
```

The default is read-only dry-run. Review `examined`,
`stale_assignments_found`, `stale_queues_found`, `already_correct`, `conflicts`,
and `errors`.

## Apply

```bash
poetry run python -m scripts.one_off.repair_expired_whatsapp_assignments --apply
```

Apply rechecks every candidate under the conversation lock. It ends only a
still-active stale assignment, writes one routing event with reason
`whatsapp_window_expired`, preserves the assignment interval, and settles an
impossible active FIFO generation if present. A standalone stale FIFO
generation is settled without inventing an assignment.

Run the preview again. A converged result reports zero stale assignments and
zero stale queues; repeating Apply is a no-op.

## Rollback

Do not reactivate historical assignments automatically. If application rollback
is required, disable the `team_inbox_whatsapp_window_expiry` schedule first,
roll back application code, and leave ended assignment evidence intact. A
customer inbound will follow the normal routing policy. Any exceptional manual
reassignment must use the normal routing command and must not target an expired
window.
