# Meta Social Inbox Cutover

Status: implemented; connection testing and production cutover require an
explicitly named target and operator acceptance.

Owner: `integration.installations` for configuration and capability bindings;
`integration.runtime` for provider transport; `integration.inbox` for verified
receipt evidence; Team Inbox owners for local conversation consequences.

## Boundary

One `meta.social` installation carries one shared Meta application/webhook
identity and two non-interchangeable account credentials:

- Facebook Page ID with a Page access token using `graph.facebook.com`.
- Instagram professional account ID with an Instagram Login token using
  `graph.instagram.com`.

WhatsApp remains a separate installation. Expired CRM OAuth rows are not a
credential source or rollback mechanism. Historical CRM conversations are not
part of this credential cutover.

## Secret bindings

Provision replacement values directly in an approved OpenBao path and enter
only `bao://...#field` references in Sub:

- `facebook_page_access_token`
- `instagram_login_access_token`
- `webhook_signing_secret` (the Meta app secret)
- `webhook_verify_token`

Never paste material values into Sub forms, shell history, logs, reports, tests,
commits, or tickets. Previously exposed tokens must be revoked before cutover.

## Configuration

Open `/admin/crm/meta` and record:

- the Meta App ID;
- the Facebook Page ID;
- the Instagram professional account ID;
- the configured Graph API version;
- the future Sub webhook URL ending in `/api/v1/webhooks/meta`;
- the four OpenBao references.

Saving creates a disabled, version-pinned installation and immutable config
revision. It does not contact Meta or activate traffic.

## Validation gates

1. On a non-production development or staging target, open Installed
   Integrations and enable the Meta Social Inbox installation. Connection
   validation must identify both configured account IDs using their respective
   credentials. Any account mismatch or rejected credential fails closed.
2. Confirm Facebook has `pages_messaging` and the Page webhook-subscription
   permissions needed by the configured fields.
3. Confirm Instagram Login has `instagram_business_basic` and
   `instagram_business_manage_messages`. Do not use the Facebook-linked
   `instagram_basic` or `instagram_manage_messages` scope contract for this
   token.
4. Send one controlled inbound message to each channel and confirm one
   `integration.inbox` receipt and one Team Inbox message per provider event.
5. Reply once on each channel and confirm one provider message ID is attached
   to the local delivery/message evidence.
6. Verify no token, app secret, verify token, raw provider response, or message
   body appears in logs or integration operation evidence.

## Production cutover

After the required feature-branch to `dev`, immutable image, staging, and
acceptance sequence passes:

1. Name the production target explicitly.
2. Deploy the accepted immutable image with the disabled installation already
   configured through OpenBao references.
3. Enable the installation and preserve the successful connection-validation
   evidence.
4. Change the Meta application callback to Sub's `/api/v1/webhooks/meta` URL and
   complete the verification challenge with the bound verify token.
5. Subscribe the Facebook Page and Instagram professional account to the
   required messaging webhook fields.
6. Perform one inbound and outbound acceptance message per channel.
7. Stop CRM Meta outbound use and observe Sub receipt, delivery, retry, and
   duplicate metrics for the agreed window.

## Rollback

Before CRM retirement, rollback is an operator transport action:

1. Disable the Sub `meta.social` installation to stop new sends.
2. Restore the prior CRM webhook callback only if the CRM credential set is
   still valid and the rollback window remains open.
3. Reconcile any verified Sub receipts whose consequence or reply outcome is
   incomplete before resuming agent work.

Do not run both outbound transports for the same account concurrently. After
the observation window closes, revoke the superseded CRM credentials and
retire the remaining settings/OAuth fallback paths in a separate contraction
change.
