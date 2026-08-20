# Team Inbox User Guide

**Audience:** Customer-service agents, team leads, managers, and Inbox administrators
**Applies to:** Dotmac Selfcare Team Inbox at `/admin/inbox`

## Purpose

The Team Inbox is Dotmac's staff workspace for receiving, organizing, and
responding to customer and prospect conversations. It brings supported email,
WhatsApp, Meta social, website, chat-widget, and field-job communication into
one operational queue.

Use the Inbox to manage the conversation. Use the Support workspace to manage a
Support ticket after a conversation requires formal investigation or tracked
resolution. A linked conversation and ticket remain separate records.

## Supported channels

The Inbox can contain:

- Email
- WhatsApp
- Facebook Messenger
- Instagram direct messages
- Facebook and Instagram comments
- Customer chat-widget conversations
- Fiber-website inquiries
- Customer-to-technician field-job chat
- Private internal notes

Available actions depend on the channel, the conversation state, the provider's
rules, and the staff member's permissions. Fiber-website inquiries are currently
inbound-only and cannot be answered from the Inbox.

## The workspace

The standard Inbox screen has three working areas:

1. **Conversation queue:** Search, filter, select, and bulk-manage conversations.
2. **Conversation thread:** Read messages, review recent activity, and reply or
   add a private note.
3. **Contact details:** Review customer identity, previous conversations,
   tickets, leads, projects, tasks, labels, and other permission-scoped context.

On smaller screens, the queue and conversation appear as separate views. Use
the back control to return to the queue. On desktop, the queue width can be
dragged and is remembered by the browser.

## Recommended daily agent workflow

### 1. Set your availability

Set your Inbox presence to the correct state:

- **Online:** Eligible for automatic assignment.
- **Away:** Temporarily unavailable.
- **On break:** Not available for new automatic assignments.
- **Offline:** Not working the Inbox queue.

Presence affects routing. Keep it accurate so new conversations are not sent to
an unavailable agent.

### 2. Start with work requiring attention

Use the queue shortcuts and filters to find:

- **Needs response:** The customer is waiting for an eligible agent reply.
- **Needs attention:** The conversation has another operational reason to be
  reviewed.
- **Unread:** You have not read the latest activity.
- **Unassigned:** No agent currently owns the conversation.
- **My conversations / My teams:** Work assigned to you or routed to your teams.

The **All** status view includes every conversation status, including resolved
history. Use the **Active** shortcut for operational work that is not resolved,
or choose a specific status to narrow the queue.

### 3. Open and review the conversation

Before replying:

- Read the latest customer message and recent thread.
- Check the channel and delivery/reply-window notices.
- Check the current assignee, team, status, and priority.
- Open **Contact details** when identity, billing, network, ticket, or previous
  conversation context may affect the answer.
- In **Contact details → Conversations**, review previous active and resolved
  threads. Personal endpoints appear together only when they have an exact
  Subscriber or reviewed Party/Reseller relationship; otherwise history stays
  limited to the exact endpoint.
- Confirm that the conversation is linked to the correct customer when the
  identity is ambiguous or unmatched.

Opening a conversation updates your personal read state. It does not mark the
conversation as read for other agents.

### 4. Claim or assign the conversation

Use **Assign to me** when you are taking ownership. Managers or authorized
agents can assign the conversation to another eligible team member, transfer it
to another team, or request automatic assignment.

Direct assignment to another person requires that person to be an active member
of the selected team. If nobody has capacity, the conversation may enter the
team's FIFO queue until an eligible agent becomes available.

### 5. Reply to the customer

Use **Reply** for customer-visible communication. The composer supports:

- Plain text
- Attachments
- Replying to a specific message
- Macros and reusable templates
- AI drafting and AI polish where enabled
- Voice transcription where enabled
- Scheduled sending
- Approved WhatsApp templates when required

After Send, watch the message status:

- **Queued:** Saved and waiting for the delivery worker.
- **Sent/accepted:** Accepted by the channel provider.
- **Delivered:** The provider reports delivery to the recipient.
- **Read:** The provider reports that the recipient read it.
- **Failed:** Delivery was rejected or exhausted its retry policy.

Do not repeatedly click Send while a reply is processing. The Inbox uses an
idempotency key to protect against duplicates, but the existing message status
is the authoritative result.

### 6. Set the correct next state

- **Open:** Active work that should remain in the operating queue.
- **Pending:** Work is waiting on another step or response.
- **Snoozed:** Hide the conversation until a duration, exact time, or customer
  reply condition is reached.
- **Resolved:** The conversation is complete and moves to Done history.

Use **Snooze until reply** when no action is needed until the customer answers.
Use a specific wake time when the team must follow up regardless of whether the
customer replies.

## Searching, filtering, and saved views

The queue supports filters for:

- Search text
- Status and channel
- One or more service teams
- Assigned agent
- Needs response or needs attention
- Contact-resolution state
- Priority
- Muted or snoozed state
- Open, unassigned, or unread work
- Meta reply-window state
- AI-handled conversations
- Conversations with or without a linked ticket
- Activity date range
- Sort direction and pagination

Advanced team conditions can combine AND/OR rules. Invalid advanced filters are
rejected rather than interpreted loosely.

Save commonly used combinations as a **Saved view**. A view can be personal or
shared, subject to permissions. Only its owner can delete it.

Bulk actions can update selected conversations, including supported status,
priority, label, team, and assignment operations. Review the selection before
submitting a bulk action.

## Channel-specific guidance

### WhatsApp

Free-form WhatsApp replies are allowed only while the provider reply window is
open. The Inbox calculates this from the latest qualifying inbound customer
message. Notes, failed sends, staff replies, and delivery receipts do not reopen
the window.

When the window is expired, select an approved WhatsApp template or wait for the
customer to send another qualifying message. Business-initiated WhatsApp
conversations must begin with an approved template.

Template forms can request numeric header/body values, media URLs, and dynamic
URL-button values. Confirm the preview and recipient before sending.

WhatsApp delivery can take longer than an Inbox screen update because a
dedicated notifications worker must contact Meta. Attachments may require a
media upload and separate provider calls. A temporary Meta or network problem
can place the message into a retry schedule.

### Email

New email conversations support recipient, subject, message, attachments, CC,
and BCC. Separate multiple CC/BCC addresses with commas, semicolons, or line
breaks. Any invalid address blocks the send.

CC and BCC are available when starting a conversation; they are not currently
shown in the existing-conversation reply composer. BCC recipients are never
placed in the visible email headers.

### Facebook and Instagram direct messages

Free-form replies follow the same calculated provider reply-window principle as
WhatsApp. Use the reply-window notice in the conversation rather than assuming
that an open workflow status permits a provider send.

### Facebook and Instagram comments

Use the dedicated social-comments workspace to review parent comments and post
public replies. Remember that a public reply is visible on the social platform.
Do not include private customer information.

### Chat widget

Native chat conversations update in real time. An agent may insert an
introduction manually. When configured, the first pickup can automatically send
that agent's introduction once for chat-widget conversations only.

Customer satisfaction can be submitted only after an eligible native chat
conversation is resolved.

## Collaboration tools

### Private notes and mentions

Switch the composer to **Note** to add information that must remain internal.
Type `@` to mention a colleague. A private note never reaches the customer and
does not count as an agent response to the customer.

### Team comments

Comments can be attached to a conversation or a specific message and later
resolved. Use them for internal discussion that should remain distinct from the
customer-visible chronology.

### Labels

Create, apply, and remove labels to categorize work. Use labels consistently so
saved views, automation, and team reporting remain useful.

### Macros and reply templates

- A **macro** can provide reply text and may also apply supported workflow
  actions such as changing status or adding a label.
- A **reply template** provides reusable channel-aware content and may preserve
  provider-template metadata.

Running a macro is different from inserting similar text: running it records the
macro identity and applies its configured actions.

## Customer, lead, and ticket context

The Contact Details drawer may show, according to permission:

- Canonical customer or prospect identity
- Subscriber or reseller details
- Previous conversations, including conversations handled by other agents
- Active and recent Support tickets
- Lead information and lead-intake actions
- Projects and tasks
- Network and connection details
- Billing or arrears information

Restricted information is shown as restricted rather than replaced with guessed
or placeholder values.

If a conversation is unmatched or ambiguous, review the evidence before linking
or merging it with a customer. The Inbox intentionally avoids guessing when a
phone number or email belongs to multiple records.

### Creating a Lead

Authorized staff can create or attach a Lead from an unmatched conversation and
can issue or revoke a lead-intake invitation. The Inbox records the relationship
but does not become the owner of the Lead's sales lifecycle.

### Issuing a Support ticket

Use the ticket action when the conversation requires formal Support tracking.
Provide a meaningful title, description, priority, and reason.

Creating a ticket does not automatically resolve the conversation. Continue to
manage the conversation in Inbox and the ticket in Support according to their
separate states.

## Advanced features

### AI draft

AI Draft uses a bounded selection of the current conversation to suggest a
reply. Review and edit the suggestion before sending. It is never sent
automatically.

### AI polish

AI Polish rewrites the current unsent draft using the configured business voice
and channel guidance. It is designed to preserve meaning and protected facts.
The agent must explicitly accept the result.

### Voice transcription

Where enabled, record a short voice input and transcribe it into the composer.
Review the text before sending. Audio is size- and duration-limited and is not
kept as part of the normal Inbox message history.

### Catalogue sharing

Use **Share catalogue** to send an approved, versioned public plan-family
catalogue link. Select the correct catalogue for the customer's enquiry.

### Conversation transcript

Authorized agents can email the current conversation transcript to a specified
recipient. Confirm the address because the transcript may contain customer or
internal operational information.

### Real-time updates

The Inbox uses a WebSocket connection for new-message, unread, typing, and
delivery-status notifications. Real-time events prompt the screen to reload
authoritative data; they are not the permanent record. If the connection drops,
the Inbox reconnects and periodic refresh remains a fallback.

### Activity and history

The message thread includes customer messages and a limited selection of recent
system events, such as status, assignment, and open/read activity. The Contact
Details drawer provides recent previous conversations for the same customer.

The backend retains broader routing, queue, status, assignment, delivery, and
audit evidence. The current Inbox UI does not expose the complete audit record
as one full activity-history screen.

## Manager workflow

Managers can use the Manager Dashboard to review:

- Agent presence
- Current workload and capacity
- Conversation status counts
- Channel workload
- Team assignment options

Managers should regularly check unassigned work, needs-response and
needs-attention cohorts, FIFO queues, failed outbound messages, and conversations
that have remained pending or snoozed longer than expected.

Manager AI, where separately permitted, can answer questions using bounded
Inbox management context. It cannot independently assign, resolve, or send a
customer reply.

## Administrator features

Inbox settings include:

- Email-address-to-team routes
- Channel/provider/account-to-team routes
- AI intent-to-team routes and confidence thresholds
- Agent introduction templates
- AI intake draft, validation, activation, and disable controls
- AI polish business voice and channel guidance
- Outbound email sender selection

AI intake policy changes use a controlled lifecycle: save a draft, validate it,
then explicitly activate it. Editing a draft does not silently change the active
runtime policy.

Automation rules can respond to conversation creation or inbound messages and
perform supported assignment, auto-assignment, or label actions.

## Troubleshooting

### A reply remains queued

Queued means the reply is stored but the delivery worker has not completed the
provider call. Do not submit a duplicate immediately.

Check:

- Whether the status changes after a short wait
- Whether other channels are also delayed
- Whether the dedicated notifications worker is healthy
- Whether the provider is timing out or rate-limiting requests
- Whether a scheduled send time was selected

WhatsApp messages may take longer because Meta must accept the request and media
may require additional calls.

### A WhatsApp reply is blocked

Check the reply-window notice. If expired, use an approved template. If the
template list is unavailable, report the provider/configuration problem rather
than attempting a free-form workaround.

### A message failed

Review the safe failure state and use the message retry action when appropriate.
Authorized users can also retry a bounded batch of failed messages and view the
failed-outbox report. Permanent validation or configuration failures require
correction before retrying.

### Customer details are missing or ambiguous

Open Contact Details and review the resolution status. Search and link the
correct record only when the evidence is clear. Escalate ambiguous identity
instead of guessing.

### The thread does not show a new event

Use **Refresh thread**. The Inbox protects unsent composer text during background
refreshes and displays a notice when newer messages are available.

## Permissions

What a user sees and can change depends on assigned permissions. Important
capabilities include:

- Viewing the Inbox and customer context
- Updating conversations and sending replies
- Assigning a conversation to yourself
- Assigning other agents or teams
- Creating or linking Leads
- Viewing manager AI
- Viewing billing, network, or other restricted customer sections

The absence of a control may therefore reflect permissions, conversation state,
or channel policy rather than a UI error.

## Current limitations

- Fiber-website inquiries are inbound-only.
- Email CC/BCC are limited to starting a conversation.
- WhatsApp template fields support the implemented numeric-variable contract;
  named-variable support is not established.
- Meta free-form replies require a qualifying inbound message within the
  provider window.
- The complete lifecycle and provider-attempt audit is not available as one
  user-facing Activity screen.
- The Inbox and Support ticket lifecycle remain separate by design.
- AI suggestions require human review and do not auto-send ordinary replies.
- Ambiguous customer identities are not automatically linked.

## Related technical documentation

- `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`
- `docs/designs/ADMIN_INBOX_WORKSPACE.md`
- `docs/designs/INBOX_CUSTOMER_CONTEXT_AND_LEAD_ACTIONS.md`
- `docs/designs/INBOX_PLAN_CATALOGUE_SHARING.md`
- `docs/runbooks/team-inbox-reconciliation.md`
