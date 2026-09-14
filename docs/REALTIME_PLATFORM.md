# Real-time Platform Contract

Status: active Sub-owned platform capability.

Owner: `runtime.realtime_projection` in `app.services.realtime_platform`.

## Boundary

Real-time delivery is a projection, not business state, an audit trail, or a
workflow engine. The relevant domain service commits authoritative state and
durable evidence first. It may then publish a hint that lets a client refetch
the canonical REST/read-model projection.

Routes, WebSocket handlers, SSE handlers, jobs, and notification adapters do
not decide domain transitions. The platform owner does not import HTTP types or
raise HTTP exceptions. WebSocket and SSE are interchangeable delivery adapters
over the same topic and event contract.

Bandwidth live views are a distinct source-owned sample stream: the bandwidth
owner produces observations and SSE frames them. Their existing sample payload
is intentionally not rewritten as a durable platform event.

## Delivery semantics

- Redis pub/sub provides cross-process fan-out and is at-most-once.
- There is no replay, ordering guarantee across topics, or delivery receipt.
- Every connection/reconnection is a refetch boundary.
- `Last-Event-ID` cannot be honored with Redis pub/sub. SSE emits
  `realtime.reset` with `refresh_required=true` instead of pretending to replay.
- Broker failure never rolls back an authoritative domain write.
- Consumers needing durable cross-team processing use the event store/outbox,
  not this platform.

## Version 1 envelope

Both transports carry:

```json
{
  "event_id": "uuid",
  "event": "workqueue_changed",
  "topic": "workqueue:audience:team:uuid",
  "data": {},
  "timestamp": "RFC3339 timestamp",
  "schema_version": 1,
  "refresh_required": true
}
```

SSE additionally maps `event_id` to the frame `id` and `event` to the frame
event name; its data field remains the complete envelope. WebSocket sends the
envelope directly. Existing top-level `event`, `data`, and `timestamp` fields
are retained for compatible clients.

## Authorization and topics

Topic authorization is performed before subscription and is object-scoped:

- `conversation:{id}`: a widget may subscribe only to its token-bound
  conversation; a subscriber only to a conversation bound to that subscriber;
  staff require `support:ticket:read`.
- `operation:{id}`: staff require the target resource's read permission.
- `workqueue:user:{id}`, `workqueue:audience:team:{id}`, and
  `workqueue:audience:org`: clients cannot select these. The workqueue scope
  owner derives them from the authenticated principal.
- `principal:{id}` and `audience:staff`: assigned by the server at connection
  registration and never accepted as client-selected topics.

Legacy clients may temporarily send a raw conversation or operation UUID; the
authorization owner resolves it to the explicit topic and applies the same
object check. This is a compatibility input, not an authorization bypass.

## Publishing and migration

Synchronous domain callers publish through the shared Redis client in
`app.services.realtime_platform`. They do not import `app.websocket`, start a
new event loop, open a private Redis client, or construct broker channels.

The original `inbox_ws:` channel family and its transport-specific service
bridge are retired. The workqueue, team inbox, network-operation notification,
and WebSocket notification publishers use `realtime:v1:`. Architecture tests
keep services independent of the WebSocket package and prevent the legacy
prefix from returning.

## Support ticket comment invalidations

`support.ticket_lifecycle` alone decides when a customer-visible Ticket comment
projection changed. After the owning comment create, edit, delete, or visibility
command commits, it may publish `support_ticket_comment_changed` to the
server-assigned `principal:{subscriber_id}` topic. Internal comments never
publish. A transition between internal and public does publish because the
authoritative customer projection must add or remove the comment. A bulk
comment command emits at most one coalesced invalidation.

The version 1 `data` value is the typed, identifier-only hint:

```json
{
  "ticket_id": "uuid",
  "change": "comment_created|comment_updated|comment_deleted|comment_visibility_changed",
  "comment_id": "uuid-or-null"
}
```

It never contains a comment body, attachment, author identity, or subscriber
identity. The subscriber identity is used only to derive the server-owned
principal topic. Delivery failure is isolated after commit and cannot reject or
roll back the saved comment.

Authenticated mobile clients connect to `/ws/inbox` with the existing
`dotmac-auth` WebSocket subprotocol and the current access token. They do not
send a subscription request for this event. A matching Ticket invalidation,
connection acknowledgement, reconnect, or app resume triggers one coalesced
read of the authoritative `/me/support/tickets/{id}/comments` resource. The
socket payload is never rendered as comment content. Manual refresh and
post-action reconciliation may additionally reload the Ticket header.

## First supported surfaces

- Team inbox and chat-widget conversation events over WebSocket.
- Network operation status hints over WebSocket.
- Workqueue invalidations over server-scoped WebSocket and SSE endpoints.
- Principal/staff notification hints over WebSocket.
- Customer-visible Support Ticket comment invalidations over the authenticated,
  server-scoped principal WebSocket topic.
- Bandwidth observations remain source-owned SSE with their established
  payload contract.
