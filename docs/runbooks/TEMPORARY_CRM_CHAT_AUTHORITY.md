# Temporary CRM chat authority runbook

## Scope and targets

This runbook applies only to `selfcare.dotmac.io` and `crm.dotmac.io`. It moves
portal live-chat authority temporarily from Selfcare to CRM and reconciles
native history without dual writing.

## Preconditions

- CRM deployment containing `scripts/import_selfcare_chat_history.py`;
- Sub deployment containing `crm.chat_session.v1` and the native hard gate;
- the existing `dotmac.crm` installation adopted to manifest 1.1.0;
- `chat_widget_config_id` points to the active `DotMac Self-care` CRM widget;
- `crm.chat_session.v1` is bound, statically validated, connection-validated,
  and enabled;
- `CHAT_LIVE_ENABLED=true`;
- no message bodies or private exports are printed to logs.

## Cut to CRM

1. Record current Selfcare populated-conversation and inbound-message counts.
2. Create a preflight export in an operator-owned mode-0600 file outside the
   repository and transfer it to a mode-0600 path on `crm.dotmac.io`.
3. Run the CRM importer in dry-run mode. Stop on an unmapped or ambiguous
   identity, count mismatch, digest mismatch, or existing-content conflict.
4. Set `comms.chat_session_authority=crm`. This is the write barrier: existing
   native tokens now fail closed and all new portal sessions use CRM.
5. Create and transfer a new final export after the barrier. Dry-run that exact
   file; do not apply the preflight file. This final snapshot closes the race
   where a native message could arrive during preflight.
6. Open a customer portal chat and verify the broker returns the CRM origin and
   CRM WebSocket. Send one controlled message and verify it appears once in CRM
   and zero times in Selfcare.
7. Apply the CRM history import.
8. Replay the importer. Require zero created conversations/messages and all
   source items reported reused.
9. Compare source and target provenance counts.
10. Remove both private exports from both hosts.

## Roll back to Selfcare

1. Stop and investigate any unresolved CRM-to-Selfcare history gap.
2. Set `comms.chat_session_authority=selfcare`.
3. Open a controlled portal chat and verify `/widget` plus `/ws/inbox`.
4. Verify one native message appears once in Selfcare and no new CRM portal
   session is created.
5. Observe traffic and delivery health.
6. Disable `crm.chat_session.v1` after the observation gate passes.

Changing the authority setting is the rollback. Do not enable two chat writers
or add a fallback that writes locally when CRM is unavailable.
