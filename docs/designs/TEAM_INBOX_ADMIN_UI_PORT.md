# Team Inbox: CRM import triage and the single build ordering

Status: triage complete.

- `feat/inbox-reconnect-and-context` — slices 1, 2 and the two read-only edges
  of slice 3. Green on seabone. Not merged, no PR opened.
- `feat/conversation-ticket-handoff` — stacked on the above; the conversation →
  ticket handoff. Structurally complete and its boundary tests pass, but its
  behaviour tests are **blocked on an ownership decision** about nesting the
  Ticket create command. See §5, slice 3.

See §5 for per-slice status.

Two things prompted this document: 21 CRM web files were copied into
`app/web/admin/`, and the question of what it takes to make sub's inbox the only
inbox. They have the same answer, and it is mostly not a port.

All figures verified against `origin/main` @ `9845f7dcf`.

---

## 1. The import is not portable

The 21 files (7,193 lines) are byte-identical copies of `dotmac_crm/app/web/admin/`.
They are the **top** of the CRM stack; none of the stack underneath came with them.

| Required by the import | In sub? |
| --- | --- |
| `app/services/crm/` (46 modules, incl. the 32-module `inbox/` subpackage) | absent |
| `app/models/crm/` (`team`, `conversation`, `enums`, `sales`, `referral`) | absent |
| `app/logic/` (`private_note_logic`, 9 call-sites) | absent |
| `app/models/person.py` + `app/services/person` | absent — **sub has no `Person` entity at all** |
| `app/web/admin/_auth_helpers.py` (42 call-sites) | absent — sub exposes these from `app/web/admin/__init__.py` |
| `app/services/{regions,storage,time_preferences,customer_retention}.py` | absent |
| `templates/admin/crm/**` | absent — 36 of 39 referenced templates |

Only 12 module paths and 1 template resolve. One resolves at module but not symbol
level: `from app.services.subscriber import subscriber` — sub's is a module
exposing `subscribers`, not a package.

`person_id` columns across ~20 sub models are bare `UUID` with no FK, carrying
external CRM/staff UUIDs (`app/models/organization.py:241`). Native identity is
`app/models/party.py`. Any CRM screen editing agents *as Person rows* has no
backing entity here.

**Resolved during triage:** `crm_referrals 2.py` deleted — the raw CRM original of
work already completed as tracked `crm_referrals.py` ("Ported from CRM … restyled
to sub conventions"). Nothing imported it; the space in the filename made it
unimportable. The other 20 files moved to gitignored
`scratchpad/crm-import-reference/`, outside the `app` package.

**Do not port:** `crm.py` (router aggregator, superseded by `inbox.py`) ·
`crm_contacts.py` (sub: `customers.py` + `contacts_router` + `party.registry`) ·
`crm_leads/quotes/sales.py` (sub: `app/web/admin/sales.py`) · `crm_widget.py`
(`team_inbox_widget` + `app/api/chat_widget.py`) · `crm_support.py` · `campaigns.py`
(`comms_campaigns`, 1,419 lines, + `app/api/campaigns.py`). None add an edge.

---

## 2. Constraints on all work below

1. **`app.web.admin.inbox` is the only named HTTP translator** for
   `communications.team_inbox` (SOT map note 9). New sibling modules must update
   that note and `app/services/sot_relationships.py` in the same change.
2. **Every write goes through `team_inbox_commands`.** House rule confirmed in
   `inbox.py`: `team_inbox_operations` is imported for reads and its
   `InboxOperationError` type only; all 17 mutations call `team_inbox_commands`.
   *"Inbox ORM rows have no writer outside the `team_inbox_*` family."*
3. Permissions are `require_permission("support:ticket:read" | ":update")`, not
   CRM's role/scope helpers.
4. Sub's route shapes win: `/inbox/{id}`, `/inbox/bulk` — not CRM's
   `/inbox/conversation/{id}`, `/inbox/conversations/bulk`.
5. Precedent for any port: `crm_referrals.py` — thin routes over native context
   builders, restyled, not transliterated.

---

## 3. Ingestion is already push — never add polling

CRM polls IMAP. **Sub has no IMAP code at all** (`git grep imaplib` over `app/`
returns nothing). Every path is push:

| Concern | Mechanism | Status |
| --- | --- | --- |
| Inbound email | `app/team_inbox_smtp.py` + `team_inbox_smtp_inbound` (aiosmtpd) → `rfc822` → `team_inbox_receive` | built, config-gated |
| Inbound WhatsApp/Meta | signed webhooks `app/api/{inbox,meta_inbox}_webhooks.py` → `team_inbox_channel_receive` | built |
| UI liveness | `@router.websocket("/ws/inbox")` + manager ← `team_inbox_realtime` → `realtime_platform`, published after commit | built (#1550) |

WebSockets solve UI liveness, not ingestion — no mail provider pushes over them.
The two must not be conflated.

**The real email gap is a routing table, not a connector engine.**
`TeamInboxEmailRoute` (`email_address` → `service_team_id`, `is_primary`,
`priority`) has a model and is read by `team_inbox_routing.build_email_team_routing_plan`,
but has **no API, no admin UI, no service CRUD** — reachable only by direct DB
insert. Hence 0 rows against 6 live mailboxes. SMTP config knobs stay env-owned in
`app/config.py`; do not build UI for those.

Cutover means pointing MX or per-mailbox forwarding at the inbound listener — a
mail-side change, verified by the existing `verify_smtp_probe_delivery` probe. No
IMAP bridge, ever.

---

## 4. Diagnosis: the inbox is an island, and the UI already admits it

**No edges exist.** `git grep conversation` over `app/services/support*` and
`app/services/ticket*` → no hits. No `conversation_id` on any work-order or
dispatch model. `app/web/admin/customers.py` has **zero** inbox references — the
customer 360 page runs Identity → Contact → Business → Portal → Subscriptions →
Invoices → Payments → Proofs → Credit Notes → Extensions with **no communications
section**. `customer_experience_lifecycle` has no inbox references.

**The isolation already forked the domain.** `app/models/field_chat.py` says so:
*"Sub has no CRM inbox/conversation dependency for field operations, so field chat
persists directly against the work order instead of a conversation."* Sub now has
four message stores: `inbox_messages`, `inbox_comments`,
`field_job_chat_messages`, `portal_messages`.

**But the spine is built.** `InboxContactLink` is evidence-bound: unique on
`(channel_type, normalized_contact)`, FK → `party_contact_points.id`
`ondelete=RESTRICT`, a CHECK requiring any party binding to carry `bound_at` +
`binding_source` + `binding_reason`, and a CHECK enforcing `subscriber_id` XOR
`reseller_id`. The join from a WhatsApp number → party → subscriber → tickets,
work orders, invoices exists. Nothing reads it across a boundary yet.

**Pattern to copy for any edge:** `support.ticket_work_order_handoff` —
`projection_writer` / `coordinator_managed`, with a design doc, cutover runbook,
boundary tests, and migrations 382 + 406.

**The UI states the gap itself.** `grep -i demo templates/admin/inbox/` → 18 hits,
including *"Demo state — ready to map to the start-conversation API"* and
*"Preview form. The support-ticket create API will replace this demo adapter."*
The parity target is not CRM; it is sub's own unkept promises.

### What the active workspace wires

Extracted from the rendered templates + `static/js/admin-inbox.js` (845 lines, one
`fetch`; the rest HTMX and server-rendered forms).

**Wired (12):** list/filters · `bulk` · `filters/save` · `filters/{id}/delete` ·
`reports/outbox-failures` · `{id}/contact` · `{id}/contact-link` · `{id}/labels` ·
`{id}/note` · `{id}/reply` · `{id}/status` · `{id}/workflow`.

**Backend exists, active workspace never calls it:** `{id}/read` ·
`{id}/comments` · `comments/{id}/resolve` · `{id}/macros/create` ·
`{id}/templates/create`.

Those controls survive as markup in `templates/admin/inbox/detail.html`, which the
active GET detail route no longer renders. It is a parts bin, not dead code.

---

## 5. The build ordering

One ordering. It supersedes the Stage/Phase/Tier sequences used in earlier
revisions of this document. Slices are ordered by dependency and risk, but they
are not strictly gated on one another — a later slice may begin once its inputs
exist.

### Slice 1 — Workspace integrity  *(delivered)*

Things that are currently wrong or silently lossy.

| Item | Finding |
| --- | --- |
| Mark-read | `POST {id}/read` exists and is permission-gated; the workspace renders `operator_unread_count` and `is_unread` but never calls it. Unread counts drift permanently. |
| **Macro/template identity** | The composer sends **body text only**. `team_inbox_commands.reply()` *already accepts* `macro_id` and `template_id`; the UI simply omits them. With `template_id`, `reply()` loads the template, **uses the template body**, and emits `reply_metadata["whatsapp_template"] = {name, language, inbox_template_id}` from `provider_template_name` / `provider_template_language`. Without it, a WhatsApp send carries no provider-template identity — **a cutover blocker**, since out-of-session WhatsApp requires an approved template. With `macro_id`, `reply()` calls `record_macro_use` only; it does **not** run `execute_macro_actions` (that remains unwired anywhere, and is Slice 4 work, not a claim to make here). |
| Duplicate cohort | `team_inbox_projection` literally assigns `unreplied=queue_metrics.needs_response` and `needs_attention=queue_metrics.needs_response`, and `applyAssignmentFilter` maps both `'unreplied'` and `'attention'` to `needs_response=true`. Two labels, one cohort. |
| Failed-message remediation | **Already exposed** — `_sidebar.html:39` links to the report, which carries "Retry first 50" and per-message "Retry". No work required; recorded here to prevent redundant effort. |

### Slice 2 — Collaboration and context  *(delivered)*

- Comments + resolution (`{id}/comments`, `comments/{id}/resolve`) — internal
  collaboration is entirely invisible in the workspace today.
- Save macro / template from the composer (`{id}/macros/create`,
  `{id}/templates/create`) — agents cannot build a canned-response library where
  they need it.
- **Richer ISP customer drawer.** `subscriber_summary` already returns
  `connection` (`online`, `last_seen_at`, `ip`), full `balance` (`outstanding`,
  `overdue`, `overdue_count`, `days_overdue`, `prepaid`), `plan` (price,
  `next_billing_at`, status), `address`, `active_plan_count`, `since`. The drawer
  renders status, online/offline, account/subscriber number, plan name and
  outstanding — but not overdue amount, days overdue, last-seen, IP, next billing
  or address.

  This needs **no new domain owner and no new query** — but it is not "no backend
  work". It surfaces customer IP and financial position in a support surface, so
  it requires: a permission decision on who sees IP and arrears, sensitivity
  review, explicit freshness/staleness semantics for connection state, currency
  and null formatting, and empty/stale-state tests.

  It is also the clearest thing CRM structurally cannot match — CRM would API-hop
  into sub for connection state and balance; sub answers in the same query.

### Slice 3 — The integration promises  *(2 of 3 delivered)*

The demo buttons that are actually the edges.

- **Customer 360 Communications section — done.** The customer support tab now
  lists the five most recent conversations. Implemented as a customer-scoped
  read: `team_inbox_read.list_conversations` gained an additive `subscriber_id`
  filter (the conversation already carries the resolved subscriber, so no join
  through the contact link is needed). Read-only, gated on `support:ticket:read`.
- **Conversation history on ticket detail — done.** Same read, so an agent sees
  what the customer already said on other channels before replying. Scoped by
  subscriber; once the handoff owner lands it can narrow to the originating
  conversation.
- **Native conversation → ticket handoff — open.** New coordinator
  `communications.conversation_ticket_handoff` on the `ticket_work_order_handoff`
  pattern. `support.ticket_lifecycle` owns the ticket official timeline and will
  not accept a foreign writer, so this is built natively rather than ported from
  CRM's `resolve-with-ticket-handoff` / `resolve-with-lead`.

  Built on branch `feat/conversation-ticket-handoff`: migration 422, model
  column, keyword-only provenance on the create command, coordinator service,
  registry contract, map rows and note, route, UI swap, 10 behaviour tests and
  6 boundary tests. **Boundary tests and the registry contract pass. The
  behaviour tests are blocked**, and not by a bug in the new code:

  > `OwnerCommandError: A public owner command cannot run inside another owner
  > command.`

  `Tickets.create` is decorated `@ticket_owner_command("create")`, which takes
  the participant path **only when `support.ticket_lifecycle`'s own command is
  already active**. A different owner — this coordinator — falls through to
  `execute_owner_command` and is rejected as nested.

  The `ticket_work_order_handoff` precedent never hits this because
  `work_order_commands.create` is an undecorated function with a `commit=False`
  participant flag. `support.ticket_lifecycle` exposes no equivalent.

  Three ways forward; the choice is an ownership decision, not an
  implementation detail:

  1. **Give `Tickets.create` a participant mode** — a `commit=False` flag, or
     let the decorator participate when any owner command is active. Cleanest,
     and matches the work-order precedent.

     An earlier revision of this document called the root-path-only
     `_notify_workqueue` call a lost *notification* and treated it as a blocking
     caveat. That was wrong. `_notify_workqueue` calls
     `workqueue.events.emit_item_change`, which the module documents as
     *"Realtime workqueue invalidations … neither [WebSocket nor SSE adapter]
     becomes workqueue state authority."* It is a cache-invalidation ping for a
     live pane, already best-effort (it swallows every exception so realtime can
     never fail a write), and workqueue holds no authority. Notification is a
     different domain entirely — `communications.notification_service` and
     `communications.staff_notifications`. Missing an invalidation means a
     workqueue pane refreshes on its next aggregate instead of instantly; it
     loses no business fact. The coordinator can simply emit the invalidation
     itself after its own commit. No restructuring of ticket-lifecycle side
     effects is required.
  2. **Drop the coordinator's own transaction**, letting `Tickets.create` be the
     root and staging provenance plus audit via `execute_owner_savepoint`.
     Smaller blast radius, weaker atomicity story for the audit fact.
  3. **Two-phase** — create the ticket, then attach provenance in a second
     committed command. Simplest, but leaves a window where a ticket exists
     without its origin, which is the drift the standard exists to prevent.

  Recommendation: (1) — the only option preserving one transaction and one
  writer. With the workqueue point corrected above, its blast radius is a
  participant flag on the create command plus a post-commit invalidation from
  the coordinator. It still changes `support.ticket_lifecycle`'s command
  surface, so it wants an explicit decision rather than being slipped in from
  the communications side.

#### Open design question: work in sub is not ticket-gated

The demo button assumed every escalation becomes a ticket. The codebase does
not agree, and this shapes what the handoff should ultimately offer.

- `work_order.origin_ticket_id` is **nullable**, and three of the four
  work-order creation paths never involve a ticket: `app/services/dispatch.py`
  (direct admin dispatch), `app/services/subscription_change_execution.py`
  (a subscription change spawning field work), and
  `app/services/network/fiber_field_verification_job_plans.py`. Only
  `ticket_work_order_handoff` starts from a ticket.
- `project_tasks.ticket_id` is **nullable** too — project tasks exist without
  tickets.

So conversation → ticket is one legitimate escalation, not the only one. A
thread that is plainly "my fibre is cut, send someone" would today have to mint
a ceremonial ticket purely to reach a work order, which the architecture does
not otherwise require.

This does **not** block the ticket handoff — a tracked incident is the right
outcome for most inbound threads, and the ticket path is the one the workspace
already promises. It is flagged because the eventual shape may be a second
edge (conversation → work order, with its own provenance column and owner)
rather than routing all field work through a ticket. Decide that before adding
more escalation targets, not after.

### Slice 4 — Remaining demo-to-real workflows  *(partly delivered)*

**Delivered:** direct teammate escalation, macro execution, the combined
my-team filter (#1604); custom-date snooze and the AI-handling, sent-to-ticket
and activity-window filters.

**Still open, each needing new domain work rather than an adapter:**

- **Until-next-reply snooze.** Needs the ingestion path to clear the snooze on
  the next inbound message. That is a change to `team_inbox_channel_receive`
  affecting *every* inbound message, so it wants its own change and its own
  regression cover — not a rider on a UI slice.
- **Attachment upload.** `team_inbox_media.promote_message_attachments` promotes
  *inbound* provider media; there is no operator upload path.
  `app/services/file_upload.py` supplies validation and storage primitives, so
  the work is a media-owner entry point plus an outbound attachment contract.
- **Conversation initiation.** `team_inbox_outbound` can only reply to an
  existing conversation; starting one needs a create-and-send command, and a
  decision about contact resolution for an address with no thread.
- **Scheduled send.** Needs a queued-send model and a scheduler entry; the
  composer's `scheduledAt` is currently local state only.
- **Email transcript.** Render the thread and deliver through the canonical
  notification point.
- **Real AI draft — blocked.** Sub has **no LLM client**. `ai_operations` stores
  insights with provider/model/token provenance fields for a caller to populate,
  but nothing generates. Making this real means introducing an external AI
  dependency, with its own config, secrets, cost and data-handling decisions.
  That is a product decision, not a UI gap.

### Slice 5 — Migration readiness gate

Browser workflow verification, RBAC, audit coverage, responsive states, and tests
at representative volume. This is the gate before any traffic moves — not a
formality, given production has never exercised operator workflows at scale.

### Slice 6 — History migration and staged channel cutover  *(code delivered)*

**Delivered:** mailbox routing CRUD and an admin page at
`/admin/inbox/settings/email-routes`. `TeamInboxEmailRoute` previously had a
model and a consumer but no writer outside direct SQL, which is why production
ran six live mailboxes against zero rows.

**Still open, and not code:** forward one low-volume mailbox to the inbound
listener, confirm the probe and conversation materialization, then move the
rest. That is an MX or per-mailbox forwarding change on the mail side, and a
decision about which mailbox goes first. WhatsApp needs no new ingestion code.
Production: 84 conversations, all `chat_widget`, against CRM's ~37,539.

### Slice 7 — Explicit field-chat decision

Slice 3 does **not** eliminate `field_job_chat_messages`. It creates the point at
which bridging or retirement can be decided safely, with the conversation ↔
work-order relationship actually available. Record the decision either way.
