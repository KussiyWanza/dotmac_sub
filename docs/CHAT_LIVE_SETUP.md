# Live chat setup and temporary CRM authority

Live chat has one selected authority at a time. The database-authoritative
`comms.chat_session_authority` setting selects `selfcare` or `crm`; its default
is `selfcare`. `CHAT_LIVE_ENABLED` remains the outer availability gate.

## Selfcare authority

With `chat_session_authority=selfcare`, authenticated customer and reseller
sessions use Sub's native Team Inbox:

- session broker: `/api/v1/me/chat/session`, `/api/v1/reseller/chat/session`,
  and `/portal/chat/session`;
- visitor REST: `/widget`;
- real time: `/ws/inbox`;
- authoritative conversation/message state: `InboxConversation` and
  `InboxMessage`.

The public fiber website uses the same native transport through
`POST /widget/fiber/session`. Its adapter accepts only the exact
`FIBER_CHAT_ALLOWED_ORIGIN` (default `https://fiber.dotmac.ng`), applies a
five-start/fifteen-minute per-address throttle and a hidden honeypot/timing
gate, then issues the same bounded visitor token used by `/widget` and
`/ws/inbox`. Subsequent messages are limited to 30 per minute per session and
address.

Public fiber chat is stored as `channel_type=chat_widget` with
`surface=fiber_website`; this keeps the established reply-capable chat
transport separate from one-way `website_fiber` inquiry conversations. Exact
email/phone matches link an active Subscriber. Unmatched visitors create or
reuse a Party-backed prospect Lead through `sales.capture`; ambiguous matches
create the conversation without a Subscriber or automatic Lead and carry
`identity_review_required` for human review.

The fiber WordPress site loads `/static/js/fiber-chat-widget.js` from the Sub
base URL configured outside Git as `DOTMAC_FIBER_CHAT_API_URL`. That public base
URL is not a secret. The signed inquiry endpoint and its HMAC secret must never
be exposed to the browser.

## Temporary CRM authority

With `chat_session_authority=crm`, the same Sub broker endpoints authenticate
the portal principal and invoke the enabled `crm.chat_session.v1` capability.
CRM returns an opaque visitor token, and the browser talks directly to CRM's
`/widget` REST API and `/ws/widget` WebSocket. Sub does not create or mirror an
Inbox conversation or message in this mode.

The enabled `dotmac.crm` installation must use manifest version 1.1.0 and
configure:

| Field | Meaning |
| --- | --- |
| `base_url` | CRM origin, normally `https://crm.dotmac.io` |
| `chat_widget_config_id` | Active CRM `DotMac Self-care` widget UUID |
| `chat_ws_url` | Optional WebSocket override; otherwise derived from `base_url` |
| `service_credentials` | Existing secret reference for the trusted CRM service principal |

Bind and enable `crm.chat_session.v1`. CRM independently restricts
`POST /api/v1/widget/internal/session` to `CHAT_MINT_SERVICE_ACCOUNTS`.

The native visitor-message command rechecks the authority setting. Switching to
CRM therefore blocks writes from already-issued native tokens immediately;
there is no eight-hour token grace window.

## Historical reconciliation

The temporary rollback does not dual-write history. Use the reviewed,
idempotent operator workflow:

1. Export populated native chats with
   `scripts/one_off/export_native_chat_for_crm.py`.
2. Transfer the private export over the approved SSH channel.
3. Run CRM's `scripts/import_selfcare_chat_history.py` without `--apply`.
4. Apply only when every source subscriber resolves to exactly one active CRM
   Person.
5. Run the importer again and require a zero-create/all-reused result.
6. Compare counts and verify that no new native Selfcare messages appeared
   after authority switched to CRM.
7. Remove both temporary export files.

The CRM importer preserves timestamps and stable source IDs and deliberately
does not emit live `message.inbound` events. Imported history therefore does
not trigger auto-assignment or automated customer delivery.

Full cutover and reversal steps are in
`docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md` and ADR 0006.
