# Voice transcription data-protection contract

Status: **accepted by Michael, 2026-07-30**. Runtime remains default-off until
the selected processor passes the operational enablement gate below.

This contract governs voice input in the admin Team Inbox. It complements
`docs/designs/AI_SOT.md` and keeps transcription advisory: it can fill an
unsent composer but cannot send or mutate a conversation.

## Purpose and boundary

The feature lets an authenticated inbox agent deliberately record a
short message, send it to an approved transcription provider, and insert the
returned text into an unsent reply composer.

The ownership boundary is:

1. The browser captures audio only while the agent is visibly recording.
2. The admin route is an authenticated, CSRF-protected adapter. It validates
   the upload and calls the voice-transcription transport.
3. The use case applies the policy in this contract and calls an approved
   provider transport.
4. The transport resolves its server-side credential through
   `secrets.reference_store`/OpenBao and returns text plus safe provider
   metadata.
5. The adapter returns the transcript to the browser. It does not send an
   inbox message.
6. The agent reviews or edits the text. A later send must still go through the
   Team Inbox outbound-intent and provider-receipt owners.

The transcription provider is a transport, not a source of truth. Voice
transcription must not create or update conversations, messages, contacts,
participants, outbound attempts, or AI insights.

## Consent and recording

- Recording requires an authenticated agent with the existing permission to
  reply in the selected inbox.
- Recording starts only from an explicit press-and-hold action on the
  microphone control. It must never start on page load, focus, navigation, or
  another background event.
- The control and an accessible live region must clearly announce recording,
  transcription, success, cancellation, and failure.
- Releasing the control stops capture. Capture also stops after 120 seconds,
  when the page loses its usable recording context, or when the agent cancels.
- The browser must show a persistent recording indicator while the microphone
  is active and must release the media stream immediately after stop,
  cancellation, error, or navigation.
- Microphone permission is requested at the time of the deliberate action.
  Denial must leave the composer unchanged.
- Operational policy must tell agents not to record another person's speech
  without that person's knowledge and permission. The product must not imply
  that browser permission alone supplies the speaker's consent.
- Background recording, passive listening, continuous capture, and automatic
  activation are prohibited.

## Temporary audio handling

- Accepted content types are limited to the approved browser formats:
  `audio/webm`, `audio/mp4`, `audio/mpeg`, and `audio/ogg`, including the
  approved Opus codec variants.
- Empty audio is rejected.
- The hard upload limit is 25 MiB. The browser stops capture at 120 seconds,
  submits its measured duration, and the server rejects a declared duration
  above 120 seconds before provider egress.
  These are safety limits, not operator-expandable defaults.
- Audio may exist only in browser memory, request-stream memory, or a
  request-scoped temporary file required by the web framework/provider client.
- Audio must never be written to an application model, object storage,
  outbound event, durable queue payload, cache, insight, audit record, log,
  exception message, tracing attribute, analytics payload, or support dump.
- Transport uses TLS. Provider URLs must use HTTPS outside an explicitly
  approved local development environment.
- The provider request contains only the audio, configured model, and required
  protocol fields. Conversation history, contact details, agent identity, and
  inbox message identifiers must not be added to the provider request.
- There is no browser speech-recognition fallback in the first implementation.
  A browser fallback would disclose audio or speech to a browser-vendor service
  outside this server-side policy and requires separate approval.

## Retention and deletion

- Application retention for audio is zero.
- Browser audio blobs are released immediately after upload completion,
  cancellation, or failure.
- Server-side temporary bytes are deleted in a `finally` path immediately
  after the provider response, timeout, cancellation, or error.
- A startup/periodic cleanup must remove abandoned request-scoped voice files
  older than 15 minutes. Its directory must be dedicated to this feature so it
  cannot delete unrelated temporary files.
- Provider-side storage, model training, human review, and secondary use must
  be disabled by contract and provider configuration. A provider that cannot
  make those guarantees is not eligible.
- The provider's documented maximum transient retention and deletion process
  must be recorded in an operator runbook before enablement.
- Because the application retains no audio, a data-subject deletion request
  has no stored audio object to delete. Provider incidents or exceptional
  retention are handled through the provider's documented deletion process.
- The returned transcript exists only in the agent's unsent browser composer.
  It becomes authoritative inbox content only if the agent sends it through
  the normal Team Inbox command.

## Provider access and configuration

- Provider credentials are server-side only. They must never appear in HTML,
  JavaScript, browser requests, logs, audit metadata, or error responses.
- The credential setting contains an OpenBao secret reference. Runtime resolves
  it through the repository's secrets service; a missing or unresolved
  reference fails closed.
- No direct environment-first credential lookup is allowed. Environment
  placeholders are bootstrap inputs for stored settings only.
- The base URL, transcription model, timeout, retry count, and secret reference
  are allowlisted configuration. Arbitrary URLs or models supplied by a browser
  request are prohibited.
- Egress is limited to the approved provider host and transcription endpoint.
- Missing configuration, a disabled control, an unapproved provider, or an
  unavailable secret must return a safe unavailable result without sending
  audio anywhere.
- There is no automatic fallback to the primary text-generation provider.
  Fallback would disclose audio to an additional processor and requires its own
  approved provider entry.
- Retries are bounded and allowed only for connection timeouts and HTTP 408,
  409, 425, 429, or 5xx responses. The default proposal is one retry with
  short exponential backoff. Validation, authentication, permission, and other
  permanent failures are not retried.

The bootstrap placeholders are:

```text
VOICE_TRANSCRIPTION_BASE_URL=
VOICE_TRANSCRIPTION_MODEL=
VOICE_TRANSCRIPTION_API_KEY=
VOICE_TRANSCRIPTION_TIMEOUT_SECONDS=
VOICE_TRANSCRIPTION_MAX_RETRIES=
```

`VOICE_TRANSCRIPTION_API_KEY` must be seeded with an OpenBao reference, not a
literal secret. The runtime consumes the corresponding database-backed
integration settings and stays disabled by default.

## Audit and logging

A successful or failed attempt records only:

- acting system-user ID;
- approved context name;
- audio byte count and validated content type;
- configured provider label, model, and endpoint identifier;
- outcome category, retry count, elapsed time, and request correlation ID.

Audit and logs must never contain:

- audio bytes or a link to audio;
- transcript or composer text;
- prompt or conversation contents;
- provider credentials or authorization headers;
- raw provider response bodies;
- raw exceptions that may contain request content;
- contact names, email addresses, phone numbers, or other unnecessary customer
  identity data.

The conversation ID may be recorded only if review determines it is necessary
for scoped incident investigation. It must not be sent to the provider.

## Failure and abuse controls

- Authentication, reply permission, CSRF, context allowlist, content-type,
  size, declared duration, provider readiness, and rate-limit checks fail
  closed before provider egress.
- The endpoint is rate-limited per authenticated agent and per installation.
- Concurrent transcription requests per agent are bounded.
- Client-supplied filenames are ignored or replaced with a generated safe name.
- Media contents must be checked against the allowed type instead of trusting
  only the filename or browser header.
- Provider errors are mapped to a short safe message. They never replace the
  page and never clear existing composer text.
- A transcript is untrusted generated text. It is displayed as text, never
  rendered as HTML, and the agent must review it before sending.
- Transcription cannot trigger tools, commands, sends, contact changes, or any
  other side effect.

## Approval and operational enablement gate

Michael approved the application-side contract and implementation on
2026-07-30, including:

1. this consent and recording behavior;
2. zero application audio retention and the 15-minute crash-cleanup bound;
3. requiring a named provider review covering processing, training, retention,
   deletion, region, and subprocessors before that provider is enabled;
4. the metadata-only audit fields;
5. the server-side OpenBao credential and egress model;
6. omission of browser-vendor speech-recognition fallback;
7. the 25 MiB, 120-second, rate, concurrency, timeout, and retry limits.

The implementation is recorded in `app/services/ai/voice_transcription.py`,
the `ai.voice_transcription` registry contract, focused tests, and
`docs/runbooks/VOICE_TRANSCRIPTION.md`.

No production processor is approved merely by this design approval. Before
enablement, operators must record the selected provider's retention, training,
region, subprocessors, deletion path and data-processing approval in the
runbook, configure an OpenBao reference and allowlisted HTTPS endpoint, then
complete the runbook verification.
