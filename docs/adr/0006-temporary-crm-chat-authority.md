# ADR 0006: Temporary CRM live-chat authority

- Status: accepted
- Date: 2026-07-27
- Review owner: Dotmac operations
- Retirement condition: Selfcare Team Inbox passes the final CRM-exit cutover
  gate and becomes the sole live-chat authority

## Context

Selfcare's native widget was activated before the operational CRM inbox
cutover. Production comparison found 70 populated native Selfcare chats and 126
inbound messages that did not exist in CRM. CRM remained the staffed
omnichannel inbox. Keeping both independent writers would continue losing
operator visibility and create an unbounded reconciliation gap.

## Decision

Until the explicit CRM-exit gate passes, CRM is the live-chat transport and
operational inbox authority for customer and reseller portal chat.

- `control.settings_spec` owns the `comms.chat_session_authority` decision.
- `crm` mode brokers an authenticated session through the typed
  `crm.chat_session.v1` capability and returns CRM REST/WebSocket endpoints.
- Selfcare does not persist or mirror CRM conversations or messages in this
  mode.
- The native visitor-message owner fails closed when authority is not
  `selfcare`, including for previously issued tokens.
- Existing Selfcare-only history is moved through a bounded, timestamp-
  preserving, idempotent import that suppresses live CRM automation.

This is a temporary, approved deviation from the target Sub-as-authority
architecture. It does not reintroduce staff credentials, a direct CRM client,
or a dual writer.

## Reversal

At final cutover:

1. reconcile and verify all CRM history required by Selfcare;
2. enable and validate native Selfcare channels and staffing;
3. set `comms.chat_session_authority=selfcare`;
4. verify new portal sessions return `/widget` and `/ws/inbox`;
5. verify the fiber site loads Sub's widget, accepts one exact-origin public
   session as `surface=fiber_website`, and receives an agent reply;
6. verify zero new `surface=customer|reseller_portal|fiber_website` CRM sessions during the
   observation window;
7. disable the `crm.chat_session.v1` binding;
8. remove this temporary capability and control in a focused follow-up.

Historical CRM rows remain audit evidence and are not copied back as live
Selfcare messages.

## Consequences

- CRM stays operationally complete during the transition.
- Authority can be switched without a code rollback.
- A missing/disabled CRM capability fails the broker request; it never silently
  falls back to native Selfcare writes.
- Imported historical CRM conversations are provenance-marked and do not have
  a live portal reply transport. Agents use them as backlog evidence and follow
  up through an active CRM channel.

## Verification

- focused broker tests prove both authority modes and zero native writes in CRM
  mode;
- a native-token test proves the hard write gate;
- connector manifest tests pin CRM 1.0.0 and 1.1.0 during adoption;
- importer tests prove timestamp preservation, identity failure closure, and
  idempotent replay;
- the operator runbook requires source/target count parity and a replay with
  zero creates.
