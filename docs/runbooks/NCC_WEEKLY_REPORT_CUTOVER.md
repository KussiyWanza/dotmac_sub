# NCC weekly report cutover

Use this runbook to move the Tuesday NCC complaints workbook email from CRM to
Selfcare without duplicate or missed deliveries.

## Preconditions

- The release containing migration `533_ncc_weekly_report_delivery` is deployed
  to the named staging target through the repository promotion process.
- Selfcare `ncc_report_email_enabled` is false.
- An operator has exported the current CRM configuration to a protected local
  JSON file with keys `enabled`, `to`, `cc`, `bcc`, `sender_key`, `subject`,
  `body_template`, `local_time`, `timezone`, `send_day`, and `lookback_days`.
- The export says `send_day: "tuesday"`; any disagreement stops the cutover.

## Compare and stage

1. Run the importer without `--apply`. Confirm Tuesday, local time, timezone,
   recipient counts and body-template digest. The output intentionally does not
   print recipient addresses.
2. Run it with `--apply` against staging. This writes through
   `communications.ncc_weekly_delivery` and records audit/event evidence.
3. Keep `enabled` false while validating the admin page and a manually generated
   NCC complaints export.
4. In staging only, enable delivery with a controlled test recipient and observe
   one Tuesday-equivalent run. Verify notification delivery, workbook content,
   filename, To/CC/BCC, sender, subject/body, run-history row, and exact-artifact
   download. Disable it after acceptance if production promotion is not immediate.

Commands:

```bash
python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json
python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json --apply
```

## Production authority switch

1. Michael names the production target and explicitly authorizes production
   work under the normal immutable-digest promotion process.
2. After the final CRM Tuesday delivery is accounted for, disable the CRM NCC
   scheduled sender and capture its job/configuration evidence.
3. Apply the already reviewed configuration to Selfcare with `enabled` false.
4. Compare all effective fields in the admin page, then enable Selfcare.
5. Confirm the five-minute admission poll is registered and the next due
   Tuesday creates exactly one run and one queued notification.
6. Record delivery success and download/hash evidence. Begin the CRM route/job
   zero-traffic observation window required by the retirement ledger.

Never leave CRM and Selfcare enabled together.

## Rollback

If Selfcare fails before a Tuesday intent is queued, disable it, record the run
failure code, and re-enable CRM only with explicit operator approval. If a
Selfcare occurrence is already queued, do not re-enable CRM for that local date:
doing so would duplicate the regulatory email. Repair or redeliver using the
preserved Selfcare artifact under an approved operator action.

Rollback does not delete run evidence or the queued artifact.
