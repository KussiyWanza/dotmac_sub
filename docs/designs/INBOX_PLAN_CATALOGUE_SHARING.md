# Inbox Plan Catalogue Sharing

## Outcome

An Inbox agent can open **Share catalogue**, choose a configured plan family,
and send that family's current approved PDF to any reply-capable conversation.
Commercial Operations publishes the PDFs in **Catalog Settings → Plan
Catalogues**. The Lead form action sits beside the conversation composer and is
shown only when the contact is not a customer or customer contact.

## Plain-language flow

1. Commercial Operations prepares and approves one PDF per plan family.
2. A staff member with `catalog:write` uploads the PDF in Catalog Settings.
3. The upload becomes the current version. The former version is superseded,
   but a link already sent to a prospect continues to work.
4. An Inbox agent selects a plan family. The Inbox outbound owner sends a short
   message containing the stable PDF download link.
5. A withdrawn or missing file is never served.

The first delivery contract is a versioned public link rather than a provider
attachment. It behaves consistently on email, WhatsApp, Messenger, Instagram,
and chat channels without pretending every provider supports PDF attachment
delivery. Native provider documents may be added later as transport
optimizations; the version selected by this owner remains the decision.

## Ownership and boundaries

- `service_intent.plan_family_catalogues` owns catalogue version, publication
  status, current-version selection, and public download eligibility.
- `control.settings_spec` owns the configured `plan_families` vocabulary.
- The catalog offer owner remains authoritative for actual commercial offer,
  price, speed, policy, and availability configuration. An uploaded brochure is
  an approved communication artifact, not an alternate offer database.
- `communications.team_inbox_outbound_intents` owns the message send and
  provider delivery lifecycle.
- `sales.lead_intake` owns whether the Lead form may be issued. Its manual
  eligibility rejects customer accounts, resolved identities, ambiguous
  identities, and WhatsApp/email contacts found in the canonical customer
  identity index.
- Settings, Inbox, and public download routes are adapters. Templates render
  typed options and eligibility; they do not choose current versions or infer
  customer status.

## Catalogue lifecycle

| State | Meaning | Public delivery |
| --- | --- | --- |
| `published` | Current approved PDF for one family | Allowed |
| `superseded` | Former approved version retained for sent links | Allowed |
| `withdrawn` | Explicitly removed from circulation | Denied |

The PDF is validated by extension, MIME allow-list, magic bytes, and a 20 MB
limit, then uploaded to private object storage before publication acquires its
plan-family database lock. Publication locks the plan-family versions and the
database permits only one `published` row per family. This keeps slow object
storage from holding a PostgreSQL transaction open. Its SHA-256 checksum
provides safe retry recognition and its object key is content-addressed.

## Page contracts

### Catalog Settings

- Screen: `/admin/catalog/settings/catalogues`.
- Audience/job: Commercial Operations sees every configured plan family,
  identifies missing PDFs, and publishes an approved replacement.
- Information owner: `service_intent.plan_family_catalogues.list_catalogue_options`.
- Mutation owner: `service_intent.plan_family_catalogues.publish_catalogue`.
- Primary state: Ready or Missing PDF, with current title, version, filename,
  and size.
- Empty state: configured families remain visible with Upload PDF, so missing
  work is explicit.

### Inbox conversation

- Screen: `/admin/inbox` conversation composer.
- Audience/job: authorized support staff share one approved family brochure;
  Sales-authorized staff may issue a Lead form to an eligible prospect.
- Catalogue button: visible for every selected conversation. Missing families
  and resolved-conversation restrictions appear inline and cannot submit.
- Lead form button: visible only with `crm:lead:write`, an unresolved supported
  conversation, and the owner-provided unknown-prospect decision.
- Server enforcement: both actions re-check their authoritative inputs. Hiding
  a button is never the permission or eligibility boundary.

## Migration and repair

Migration `495_plan_family_catalogues` creates the version ledger and one-current
partial unique index. There is no legacy file-path setting to backfill. A
missing plan-family row is an explicit Missing PDF work item; repair is to
publish the approved file through the owner. Stored objects are
content-addressed, and orphan-object cleanup remains the object-storage
reconciler's responsibility if a database transaction rolls back after upload.
The migration is additive and expected to create an empty, low-volume table;
operators use the deployment's standard migration lock and statement-time
budgets. Downgrade removes only this empty/native catalogue ledger and is safe
only before catalogue publication; after publication, forward-fix is the
required recovery path so approved versions and stable sent links are retained.

## Validation

- Owner tests cover configured-family validation, PDF validation, retry by
  checksum, version supersession, and public/withdrawn resolution.
- Inbox tests cover catalogue selection, missing PDF failure, outbound message
  construction, and Lead form customer/contact suppression.
- Architecture tests keep Settings and Inbox adapters thin and require the
  complete SOT contract.
