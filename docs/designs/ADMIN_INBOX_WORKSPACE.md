# Admin Inbox Workspace

Status: implementation contract

Screen identifier: `admin.inbox.workspace`

Route: `/admin/inbox`

## Operational job

Support staff use one workspace to find, prioritise, read, reply to, assign, and
resolve customer conversations without losing subscriber and ticket context.
The first viewport must expose the active queue, the selected thread, and the
next valid action.

## Ownership

| Concern | Owner |
| --- | --- |
| Conversation, message, assignment, delivery, and read facts | Contracted `communications.team_inbox_*` owner family |
| List/detail/count/action projection | `communications.team_inbox_projection` |
| Operator mutations | `communications.team_inbox_commands` |
| Routing and assignment decisions | `communications.team_inbox_routing` |
| Outbound reply intent | `communications.team_inbox_outbound_intents` |
| Realtime delivery hints | `communications.team_inbox_realtime` through the shared realtime platform |
| Subscriber context | Subscriber summary and customer-experience owners |
| Ticket creation | Support ticket command owner |
| Responsive layout, progressive disclosure, draft state, and keyboard interaction | `admin.inbox.workspace` UI contract |

Templates, browser code, routes, WebSocket handlers, and demo fixtures are
adapters. They do not reinterpret conversation state or become business
writers.

## First viewport

- Inbox identity, unread total, realtime connection state, search, and primary
  new-conversation action.
- Current operator availability with explicit online, away, and offline
  controls. Only online operators are eligible for automatic Team Inbox
  conversation assignment.
- Common status and assignment cohorts with authoritative counts where the
  backend currently exposes them.
- A paginated conversation queue ordered by the canonical list projection.
- Either a selected conversation or an empty state linking to common cohorts.
- When selected: contact identity, channel, status, ownership, recent messages,
  and a reply/private-note composer.

## Actions

Primary page action: start a conversation.

Common conversation actions: reply, add private note, assign, change status,
change priority, snooze, mute, apply labels, retry a failed message, open
contact context, and start a ticket handoff.

Operator availability action: set my Team Inbox availability to online, away,
or offline through `communications.team_inbox_commands`. The routing owner uses
effective availability for automatic conversation assignment; away and offline
operators remain visible but are not auto-selected.

The UI projection owns the labels for the existing lower-is-more-urgent numeric
priority: `100` none, `75` low, `50` medium, `25` high, and `0` urgent. Unknown
legacy values remain visible as their exact numeric priority.

Bulk actions appear only after selection and delegate to the existing bulk
command owner. Destructive actions require explicit confirmation.

## Page state

Server state is projected by `team_inbox_projection`. URL query parameters own
shareable list filters and the directly selected `conversation_id`. Local
storage owns only device-local preferences: sidebar width, filter disclosure,
notification sound, and unsent drafts.

Missing backend capabilities may be represented by the isolated browser demo
adapter. Demo controls must be labelled, remain non-authoritative, and be
replaceable without changing the page's presentational components.

## Stats and filters control

The collapsible Stats and Filters control is a page-scoped visual exception to
the shared branding palette and 8px compact-control radius. Its static Tailwind
v4 amber, status, assignment, saved-view, dark-mode, and `rounded-xl` group
classes are confined to `templates/admin/inbox/_sidebar.html`; it does not
change the global theme, Tailwind configuration, or shared components.

The same page-scoped exception covers the sidebar shell, header, action
tooltips, live/offline indicator, and search field. The header keeps the real
new-conversation, local notification-sound, manager-dashboard, and settings
actions. Notification sound remains a local preference and never opens an
overlay. Settings uses `/admin/crm/inbox/settings` as an adapter entry point
which renders the canonical mailbox-routing settings view.

The New Conversation action opens the page-owned centred modal while retaining
`communications.team_inbox_commands.start_conversation` as the transaction
owner. Channel-specific controls submit only the active Email, WhatsApp,
Facebook Messenger, or Instagram DM address fields. WhatsApp templates retain
their template identity and provider variables; attachments are staged and
bound to the successful opening message in the same owner transaction. The
form exposes CC and BCC for the specified layout but fails closed when either is
used because the canonical notification transport does not yet support copy
recipients; the UI must not claim that undelivered copies were sent. A receiving
Inbox selector remains hidden until the projection can supply a real provider
account or mailbox identifier, so Team is not relabelled as Inbox.

The Manager Dashboard is a non-modal, upper-right floating panel, distinct from
the Stats and Filters disclosure. The current manager-level UI gate is
`support:ticket:update`, matching the repository's existing inbox permission
contract; there is no separate inbox-manager permission. Its presence, assigned
load, status, channel, and active-chat values come from the typed
`communications.team_inbox_projection` read owner. It closes from its button,
an outside click, Escape, or the header toggle without obscuring or disabling
the inbox behind it.

Search remains a server-owned list query. The browser waits 300ms after input,
updates only the `search` URL parameter, resets pagination, preserves the other
active filters and selected conversation, and refreshes the authoritative
sidebar projection. The filter form carries the current search value forward
when another filter is submitted.

The desktop sidebar resize handle is an absolutely positioned 12px by 56px
control attached to the sidebar's right edge. It is available from 640px
upward, hidden while the Manager Dashboard panel is open, and exposes a visible
hover/focus tooltip. Pointer dragging clamps the sidebar to 288-448px, applies a
document-wide resize cursor and selection lock for the drag, restores the prior
document styles on pointer-up/cancel or window blur, and persists the final
width in local storage. The default remains 320px.

Status, assignment, channel, team, agent, activity-window, unread, and saved-view
controls continue to submit the canonical projection filters. Channel remains
the communication method and Team remains staff ownership. A separate Inbox
selector is rendered only after the projection exposes a specific receiving
account or mailbox identifier and real choices; Team must never be relabelled
as Inbox.

Advanced Service Team conditions sit behind an optional progressive-disclosure
panel. Operators can require all conditions, require at least one condition in
an OR group, use positive or negative team membership, or select conversations
with or without any active team link. The browser only builds the shared JSON
transport and preserves it in the URL and saved views. The server projection
validates active team identifiers and owns all queue-membership semantics;
invalid input returns a controlled adapter error rather than widening the
queue.

## Queue below Stats and Filters

Saved Views remains part of the expanded Stats and Filters disclosure. The
section after that disclosure is a single vertical stack: new-activity notice,
selected-conversation bulk toolbar, scrollable conversation list or empty
state, then the inbox-scoped pagination footer. It does not reuse the shared
triage-row or general list-pagination macros, so its dense avatar, channel,
badge, unread, hover, and partial-pagination presentation cannot restyle other
admin lists.

Realtime activity leaves the visible queue stable and sets a browser-only
notice. `Refresh list` refetches and swaps only
`#inbox-conversation-queue`; pagination uses the same HTMX select-and-swap
boundary and preserves the selected conversation in the URL. Thread refresh
remains independent, so a focused composer is never replaced.

All list-changing interactions use one latest-request-wins coordinator: sidebar
filters and KPI links, search, saved views, pagination, browser history, manual
refresh, read-state refresh, realtime refresh, and fallback polling. Each
request receives a monotonic client sequence; a newer operator action aborts
older list work, and an obsolete response is refused at the swap boundary even
if transport cancellation loses a race. Background work yields to operator
navigation. The UI exposes immediate accessible busy/error state and restores
the last successfully rendered URL after a real request failure.

Sidebar- and queue-targeted requests preserve the selected conversation
identifier but do not rebuild its detail projection, the new-conversation
template catalog, or the manager dashboard. A normal full-page request remains
the deterministic rebuild path for those projections.

Each queue row displays only projected facts. Contact display identity resolves
in this order: the linked canonical Party name, the linked legacy Subscriber
name, the latest inbound provider-observed name, an operator/provider name on
the conversation, then the channel address. The same projected name and
initials drive the queue, detail header, contact drawer, and avatar hover text.
Unread count is the number of inbound messages after the operator's
authoritative read cursor, and ticket, status, priority, assignment/team, and
label badges come from the existing queue projection. Only the first two labels
render, with a numeric remainder. The receiving Inbox badge remains absent
until a real provider account/mailbox identity is projected.

`Needs attention` is a live, counted cohort distinct from Unreplied. It selects
an active conversation only after a customer message, a successful human-agent
reply, and a later customer follow-up, while no successful human-agent reply
follows that latest message. It applies immediately and does not depend on an
overdue timer. Successful replies require agent provenance and a
successful/accepted delivery state; failed, scheduled, AI-intake, and
explicitly no-response-required messages do not qualify.

The cohort excludes resolved, snoozed, inactive, ticketed, Facebook comment,
and Instagram comment conversations. It is recomputed by
`communications.team_inbox_projection` on every read, so message, delivery,
status, snooze, activation, or ticket changes are reflected on refetch without
a persisted UI flag.

## Loading and failure behaviour

- List, thread, and contact context load independently.
- A request already in flight for the same resource is not repeated.
- Realtime events update safe surfaces in place. A focused composer is never
  replaced; the UI shows a new-activity banner instead.
- When realtime is disconnected, the visible page polls the list projection.
- Partial failures remain inside the affected pane and do not blank the
  workspace.

## Responsive contract

- The admin shell uses the dynamic viewport height and keeps the 64px top bar
  outside the scrollable page region.
- The page wrapper fills the remaining height and applies 24px vertical
  padding, with 16px horizontal padding on mobile, 24px from 640px, and 32px
  from 1024px.
- The Alpine workspace remains a padding-free, full-size positioning
  container. A separate full-size inbox frame provides the 16px radius,
  white/slate-900 surface, and translucent slate border.
- The page behind the frame uses the shared subtle noise and gradient treatment
  over slate-100 in light mode and slate-900 in dark mode.
- Desktop: resizable 288–448px list, flexible thread, optional 320px context.
- Tablet: fixed list and thread; context is an overlay drawer.
- Mobile: list or thread, never squeezed columns. Thread provides a back action.

## Authoritative customer context

The CRM visual structure is replicated inside the admin page-content boundary.
Existing queue, conversation, contact, ticket, assignment, status, note,
attachment, transcript, and outbound-message actions continue to use their
registered backend owners.

The contact drawer contains no CRM customer placeholders. Party profile,
Lead, active Ticket, recent conversation, Project, and Project Task sections
are composed by `communications.team_inbox_contact_context` from exact
structural relationships and permission-scoped owner queries. A successful
empty query renders `0`; missing/not-applicable values render `—`; unavailable,
not-calculated, stale, and restricted outcomes remain distinct and are never
coerced to zero. Sections fail independently.

`communications.inbox_lead_actions` resolves the profile and Lead controls.
It reuses a direct Lead, requires an authoritative pipeline before examining
Party Leads, requires explicit selection when several are eligible, creates a
Lead without replacing an exact Party, and sends ambiguous identities to
review. New-prospect authoring creates Party, Lead, origin evidence, and both
conversation relationships in one owner transaction.

Event-only replica surfaces remain hidden by default. The `crm_preview` query parameter may show
an event preview (`comment`, `reply-failed`, `notifications`, `incoming-call`,
or `active-call`) or `all`. Preview values are not submitted and must not be
treated as customer facts.

The following remain non-authoritative preview UI:

- persistent notification and reply-failure cards;
- incoming and active WhatsApp call controls;
- social-post comment thread and public comment reply;
- AI reply generation and voice capture;
- fine-grained WhatsApp template parameter fields;
- The document does not scroll; list, timeline, context, and long overlays
  scroll independently.

## Accessibility

All actions use semantic controls and visible focus styles. Dialogs trap focus,
menus expose expanded state, status is not represented by colour alone, touch
targets are at least 40px, and motion respects `prefers-reduced-motion`.
Keyboard shortcuts are disabled while focus is in an editable control.

## Validation

Focused tests cover direct conversation selection, search/filter URL state,
advanced Service Team positive, negative, empty, AND/OR, invalid-input, and
saved-view behavior, duplicate-send prevention, draft restoration, status and
assignment controls,
attachment staging, realtime activity handling, mobile navigation, dark-mode
classes, and keyboard navigation. Repository formatter, linter, type checker,
architecture tests, and relevant service tests remain the merge gate.
