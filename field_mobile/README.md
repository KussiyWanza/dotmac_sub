# DotMac Field

Technician/vendor field app for DotMac ISP operations.

This app was moved from `dotmac_crm/mobile` during the CRM-to-sub migration. Its
default API base URL is `https://selfcare.dotmac.io`; local and CI builds can still
override it with `--dart-define=API_BASE_URL=...`.

Field service is work-order execution only. The old CRM field-sales/customer
lookup module was intentionally not carried forward.

## Offline storage: encrypted and scoped to one principal

Everything this app keeps on disk belongs to exactly one signed-in principal on
exactly one deployment, and none of it is readable from a copy of the files.

- **One store per scope.** The scope is `(deployment, principal)`, derived from
  the API base URL and the access token's subject. Each scope gets its own
  directory under `scopes/<hash>/`, its own SQLCipher database and its own key
  material in the platform keystore. Every row also carries a `scope_key`
  column, part of the primary key wherever a table declares one, so the
  isolation does not depend on the file layout alone.
- **The database is encrypted whole**, not column by column: a copied file must
  not reveal schema, row counts or timestamps either. The store checks
  `PRAGMA cipher_version` at open and refuses to run on a plain sqlite3 build,
  because plain sqlite accepts `PRAGMA key` and then writes a readable file.
- **Evidence is sealed on top of that.** Photos, signatures, drafts, queued
  mutations and the location queue are AES-256-GCM envelopes bound to the scope,
  so nothing survives being copied out of one technician's storage into
  another's — and nothing is readable if the database layer is ever bypassed.
- **Tables declare their own policy.** `RebuildableProjection` marks read models
  the server can send again; `PendingOutbound` marks evidence it has never seen.
  A wipe destroys both; the plaintext migration carries only the second.
- **One wipe.** Explicit sign-out, an authoritative token revocation and a
  different technician signing in all call the same journalled wipe. It destroys
  the scope's keys before its files, so an interruption can only leave
  unopenable bytes, and the next launch finishes the job from the journal. The
  wipe first refuses new offline work and waits for work already admitted before
  closing the database, so a stale task can never try to reopen that retired
  connection.
- **Upgrading from the unencrypted store** re-encrypts and carries queued
  mutations, un-uploaded photos and signatures, saved drafts and unsent location
  pings, and destroys everything the server can resend. The plaintext source is
  not touched until the journal says the carry completed, so an interruption
  either restarts safely or finishes the deletion.

On-shift location fixes that cannot be delivered immediately are retained in
the scope's app-private, encrypted `location_queue.bin`. The queue holds at most
200 typed fixes, restores on process restart, and is cleared only after the
field location API accepts the batch. It is independent of the business-mutation
outbox so location transport failures cannot block job transitions or requests.
Malformed queue data — including an envelope belonging to key material this
device no longer holds — is deleted rather than retried or retained with private
coordinates; a payload-free `.corrupt` marker records when recovery occurred.
After upload, the server retains detailed GPS ping history for 30 days based on
its receipt timestamp; current presence and work-order evidence have separate
lifecycles.

Vendor mode uses the same sub-native work-order execution tabs as technicians,
with backend scoping by vendor assignment. Do not re-add CRM project/quote
routes; vendor work must come back as sub-native work orders.

## Useful Commands

```sh
flutter pub get
dart run build_runner build --delete-conflicting-outputs
flutter test
```
