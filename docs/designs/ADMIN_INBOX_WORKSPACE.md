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

Each queue row displays only projected facts. Contact display name is sourced
from bounded conversation metadata when available, unread count is the number
of inbound messages after the operator's authoritative read cursor, and ticket,
status, priority, assignment/team, and label badges come from the existing
queue projection. Only the first two labels render, with a numeric remainder.
The receiving Inbox badge remains absent until a real provider account/mailbox
identity is projected.

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

- Desktop: resizable 288–448px list, flexible thread, optional 320px context.
- Tablet: fixed list and thread; context is an overlay drawer.
- Mobile: list or thread, never squeezed columns. Thread provides a back action.
- The document does not scroll; list, timeline, context, and long overlays
  scroll independently.

## Accessibility

All actions use semantic controls and visible focus styles. Dialogs trap focus,
menus expose expanded state, status is not represented by colour alone, touch
targets are at least 40px, and motion respects `prefers-reduced-motion`.
Keyboard shortcuts are disabled while focus is in an editable control.

## Validation

Focused tests cover direct conversation selection, search/filter URL state,
duplicate-send prevention, draft restoration, status and assignment controls,
attachment staging, realtime activity handling, mobile navigation, dark-mode
classes, and keyboard navigation. Repository formatter, linter, type checker,
architecture tests, and relevant service tests remain the merge gate.
