# NCC weekly complaints workbook delivery

Status: implemented; disabled until controlled cutover

## Outcome

Selfcare recreates the CRM weekly NCC complaints-report delivery as a native,
typed capability. The authoritative production cadence is **Tuesday**, not
Monday. The registered default is Tuesday at 08:00 in `Africa/Lagos`, with a
seven-day lookback. Celery polls every five minutes; it does not decide whether
a report is due.

## Ownership and flow

- `compliance.ncc_complaints_reporting` owns the typed complaint-report query
  over native Tickets, TicketComments and Subscribers.
- `communications.ncc_weekly_delivery` owns configuration, schedule
  interpretation, one-occurrence arbitration, the exact XLSX artifact, durable
  communication intent, audit evidence and retry outcome.
- `scheduler.registry` only triggers a five-minute admission poll while the
  feature is enabled.
- `communications.intents` owns the queued notification and its delivery state.

The owner converts the scheduler observation to the configured timezone. On
the configured weekday, once local time reaches the configured delivery time,
it locks an existing occurrence row, creates the workbook, stores its bytes and
SHA-256 digest, and queues a required XLSX attachment in one owner transaction.
For a first occurrence, the unique `(schedule_key, scheduled_local_date)`
constraint arbitrates concurrent attempts and prevents duplicate Tuesday runs.
The report window is anchored to the configured Tuesday time, not the poll's
arrival time, so a delayed poll or retry rebuilds the same bounded window.

## Configuration and provenance

The admin page at `/admin/reports/ncc-complaints` displays and, with
`notification:write`, updates the complete effective configuration: enabled,
To/CC/BCC, SMTP sender key, subject, body template, weekday, local time,
timezone and lookback. Every field is backed by the registered notification
setting specification. Defaults remain disabled and Tuesday-based.

The on-screen complaints table uses the canonical typed list contract and shows
20 rows by default, with 50- and 100-row options. Pagination applies only to the
screen: workbook exports and weekly delivery continue to use the complete
bounded report snapshot.

The complaints resolver excludes tickets carrying a source approved by the
support owner as internal operational work. It does not use missing customer or
classification data as an exclusion shortcut: incomplete customer complaints
remain visible and fail workbook validation. Nigerian customer phones in local,
`+234`, or `234` form are projected into the NCC-only `234XXXXXXXXXX` format;
the stored customer phone is not changed, and malformed values remain visible
as filing failures.

Configuration writes use a typed owner command, emit an event, and stage an
audit record. The CRM migration utility is dry-run by default and accepts an
operator-exported JSON document; it does not establish a new runtime CRM
dependency.

## Failure, retry and repair

A failed artifact or intent is recorded on the occurrence with a bounded
failure code. The savepoint removes any partial artifact/notification work,
while the owner transaction preserves durable failure evidence. The next
five-minute poll retries the same Tuesday occurrence. Once queued, subsequent
polls return `already_queued` and cannot replace the preserved artifact.

The exact queued workbook can be downloaded from the run history. Both email
attachment resolution and operator download verify the stored SHA-256 digest;
invalid, missing, over-size or out-of-scope artifacts fail closed.

## Cutover boundary

This implementation is not authority cutover by itself. Keep delivery disabled
until the runbook's configuration comparison, dry-run, staging acceptance and
CRM-job disablement gates are recorded. CRM must not remain an active parallel
sender after Selfcare is enabled.
