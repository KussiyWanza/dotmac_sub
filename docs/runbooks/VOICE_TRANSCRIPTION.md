# Team Inbox voice transcription runbook

The runtime is installed but default-off. Do not enable it until the selected
processor has a completed privacy/security review and an OpenBao credential
reference. This runbook does not authorize production changes.

## Enablement gate

Record and approve all of the following before setting
`integration.voice_transcription_enabled=true`:

- processor legal name, service and endpoint host;
- processing region and subprocessors;
- confirmation that submitted audio is not used for training or human review;
- provider maximum transient retention and deletion/escalation procedure;
- data-processing agreement owner and review date;
- the approved transcription model;
- expected request volume and egress allowlist.

Store the credential in OpenBao. The setting and bootstrap value must contain
only its secret reference, never a literal key.

## Configuration

Configure the database-backed integration settings:

- `voice_transcription_enabled`
- `voice_transcription_base_url`
- `voice_transcription_model`
- `voice_transcription_api_key`
- `voice_transcription_timeout_seconds`
- `voice_transcription_max_retries`

The matching `.env.example` entries are empty bootstrap placeholders. The
endpoint must be HTTPS outside local development. Keep retries at zero to three
and timeout at 120 seconds or less.

## Verification

1. Confirm an agent without `support:ticket:update` is rejected.
2. Confirm empty, oversized, over-120-second, mismatched-signature and
   unknown-context uploads fail before provider egress.
3. Hold the control, record a short test phrase, release it, and verify the
   transcript is inserted without clearing existing composer text.
4. Verify no audio, transcript, prompt, contact identity, credential or raw
   provider response appears in application logs, audit rows, database rows,
   cache keys, object storage or tracing.
5. Verify the audit row contains metadata only: actor, context, byte count,
   declared duration, content type, provider/model/endpoint, outcome, retry
   count, elapsed time and request ID.
6. Confirm the transcript is not sent until the agent submits the normal Team
   Inbox reply command.

## Incident and deletion handling

- Disable `voice_transcription_enabled` first when processor privacy,
  credential, egress or retention guarantees are in doubt.
- Revoke/rotate the OpenBao credential for suspected exposure.
- The application keeps audio only in request/browser memory and has no audio
  object to delete. Escalate any provider-side exceptional retention through
  the approved processor deletion procedure recorded at enablement.
- Do not copy audio, transcripts, authorization headers or raw provider errors
  into an incident ticket.

There is no browser-vendor speech-recognition fallback. Adding one requires a
new processor review and explicit contract approval.
