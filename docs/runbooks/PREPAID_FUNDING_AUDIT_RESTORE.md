# Runbook: prepaid funding audit restore

**Purpose.** Obtain the per-account blocker reason codes for prepaid accounts
that have no reviewed opening baseline, so their funding can be reconstructed
and they can re-enter money-based enforcement.

**Owner.** `app/services/prepaid_funding_reconstruction.py` owns every baseline
write. Nothing in this runbook writes one.

---

## Why a restore is needed at all

`scripts/one_off/export_prepaid_funding_snapshot.py` refuses to run unless

```python
os.getenv("BILLING_AUDIT_EPHEMERAL") == "1"
and current_database().endswith("_audit")
```

That is deliberate. The export replays every financial fact in the system, and
must never do so against the live database. Until this runbook existed there
was no way to satisfy the guard, which made the entire funding-baseline repair
path unreachable in practice.

## What the quarantined cohort is

An account is funding-quarantined when it existed **before** the authority
cutover and has no active `PrepaidFundingBaseline`. Its balance cannot be
derived, so `prepaid_balance_sweep` excludes it from enforcement entirely:

```python
enforceable_ids = set(account_ids) - quarantined_ids
```

No warning, no suspension, no restoration. That is the correct response to an
unverifiable balance — and it means each quarantined account is a live customer
outside money-based enforcement until its baseline is repaired.

As of 2026-07-26 production carried **80** such accounts out of 3,919 funding
candidates, all active subscribers, holding 84 active subscriptions between
them, none under a prepaid enforcement lock.

## Safety model

The audit stack is provisioned beside production, never inside it:

| property | value |
|---|---|
| network | dedicated, `--internal` — no route off the host, none to the app stack |
| ports | none published |
| storage | dedicated volume, destroyed on teardown |
| auth | `trust`, safe because of the two rows above, and avoids minting a credential for a database that lives for minutes |
| live DB | never connected to; the script reads a **dump file** only |

The script refuses to name the live database container, and refuses any
`AUDIT_DB` not ending in `_audit`.

`--allow-primary` is required when running the exporter here, and is correct:
the restore is its own primary. It cannot be abused to reach production,
because the `_audit` database-name check has already rejected that case.

---

## Procedure

Run on the host holding the backups. Prerequisites: a deploy-time dump in
`/var/backups/dotmac_sub/`, and **≥ 60 GB free** — a 2.4 GB gzip dump expands
to roughly 25 GB with indexes. The script preflights both.

### 1. Provision

```bash
scripts/one_off/prepaid_funding_audit_restore.sh provision
```

Uses the newest dump unless given `--dump PATH`. The restore takes a while;
it is verified by counting `subscribers` rows, not by trusting `psql`'s exit
status (a plain-format prod dump reliably emits benign errors for extensions
and roles absent from a bare image).

### 2. Export

```bash
scripts/one_off/prepaid_funding_audit_restore.sh export \
  --snapshot-at 2026-07-26T00:00:00+00:00 \
  --source funding-gap-survey-2026-07-26
```

**Exit 2 is the expected outcome** while accounts remain quarantined: the
cohort is not fully reconstructable, so nothing is sealed. The blockers file
is written *before* that point, and is the artifact you came for:

```
/var/backups/dotmac_sub/funding_audit/blockers_<stamp>.json
```

It contains account UUIDs and reason codes only — no customer identity,
credentials, free text, or delivery coordinates.

Pass `--allow-quarantined-subset` through only when you intend to seal
baselines for the reconstructable accounts and bind the excluded cohort into
the signed blocker manifest. That path additionally needs `--signing-key-ref`
pointing at the OpenBao Ed25519 private key reference.

### 3. Destroy

```bash
scripts/one_off/prepaid_funding_audit_restore.sh destroy
```

Removes the container, the volume (and with it the restored copy of production
data), and the network. Exported artifacts are kept.

**Do not leave the audit stack running** — it is a full copy of production.

---

## After the export: the repair pipeline

1. `export_prepaid_funding_snapshot.py` — this runbook. Produces blockers.
2. `adjudicate_prepaid_funding_gaps.py` — binds one reviewed decision to each
   blocker. Cannot write money: no `SessionLocal`, no `--apply`. Dispositions
   are `source_evidence_required`, `canonical_payment_required`, `quarantine`,
   `no_paid_through_due_immediately`.
3. **Correct the owning source, then re-replay.** A blocker clears only when
   the independent replay stops reporting it — never by editing the manifest.
4. `materialize_prepaid_funding_reconstruction.py` — verifies the Ed25519
   signature against the public key at billing setting
   `prepaid_reconstruction_attestation_public_key_ref`, then applies. Dry-run
   by default; apply requires `--reviewed-sha256`, `--evidence-ref`,
   `--approved-by`, and `--confirm-final-cutover`.

### Three things that surprise people

**Manifests are cohort-complete, never partial.** The materializer passes
`expected_account_ids = candidate_prepaid_funding_account_ids(db)` — the whole
cohort — and preview blocks on any `missing_reconstruction_account` or
`unexpected_reconstruction_account`. You cannot repair 80 accounts in
isolation; every manifest names all of them, each either materialized or
explicitly quarantined.

**A repair batch is not the cutover.** The authority cutover already exists, so
a new batch gets `is_authority_cutover = False`, and its `position_at` must be
strictly newer than the existing baselines or preview blocks with
`reconstruction_position_not_newer`.

**Bank-statement evidence is not self-authorising.** It authorises a canonical
payment only with attested definitive customer attribution and a non-secret
evidence reference. Amount/date coincidence is explicitly insufficient.

## Monitoring

`billing_prepaid_funding_quarantined_accounts` tracks the size of this backlog;
the `prepaid_funding_quarantined` anomaly logs at `warning`, not `error`,
because quarantine is the correct outcome for an un-baselined account rather
than a defect to page on. Watch it trend to zero as repairs land.
