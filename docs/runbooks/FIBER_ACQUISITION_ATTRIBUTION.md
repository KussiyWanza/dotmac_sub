# Fiber acquisition attribution deployment

The implementation is deployed through the normal immutable-digest release
sequence. Do not configure WordPress until the staging migration and signed
smoke test have passed.

## Required secrets

- Create a random HMAC secret in the approved secret manager and bind it as
  `webhook_signing_secret` on the `fiber.inquiry.http` `1.1.0` installation.
  Never put
  the value in connector config, logs, commits, screenshots, or tickets.
- Set `CONVERSION_INGEST_API_KEY` from the approved secret manager for stable
  pseudonymous subject-key derivation.
- Bind the same marketing API key to the outbound `webhook.http` `1.1.0`
  installation as secret `authorization`; configure `authorization_scheme` as
  `Bearer`.

## Capability bindings

Create and enable `communications.fiber_inquiry.receive.v1` with:

```json
{
  "site_id": "fiber.dotmac.ng"
}
```

The omitted values intentionally use these defaults:

- signature header: `X-Dotmac-Fiber-Signature`
- delivery header: `X-Dotmac-Fiber-Delivery`
- signature prefix: `sha256=`

Create and enable an `events.deliver.v1` `webhook.http` binding with URL
`${MARKETING_BASE_URL}/api/v1/conversions/events`, method `POST`, and
`authorization_scheme: Bearer`. Subscribe it to
`marketing.conversion_ready` with payload policy
`{"projection":"event_payload.v1"}`.

## Staging acceptance

1. Upgrade the staging database to Alembic head and confirm revision
   `616_fiber_acquisition_attribution`.
2. Submit one redacted, signed `fiber-coverage-v1` request with a unique
   delivery ID. Confirm a single IntegrationInbox receipt, provider observation,
   Lead, immutable origin, customer reference, and safe coverage response.
3. Replay the identical delivery and confirm the same reference, coverage,
   conversation, and message with no new records.
4. Confirm invalid signatures create no durable receipt or business data.
5. Inspect the marketing delivery payload and confirm it contains none of:
   customer name, email, phone number, or street address.
6. Submit an address without coordinates and confirm a Lead is created while
   `coverage` is null.

Record only the base URL, binding UUID, header names/prefix, migration revision,
test results, and a redacted response. Hand the website administrator a secret
manager reference or approved one-time secure exchange; never return the secret
itself.
