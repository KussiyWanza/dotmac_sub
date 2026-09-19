# Identity / Email Decoupling

**Status:** Layers 1+2 implemented; bounded customer email login alias added 2026-09-14
**Date:** 2026-06-20
**Scope (this doc):** Core only — Layers 1+2 (relax global email uniqueness; keep credential usernames canonical while permitting a fail-closed customer email login alias). Reseller-identity-off-subscribers (Layer 3) and `+NNNN` data cleanup are noted as follow-ups, not implemented here.

---

## 1. Problem & corrected diagnosis

The system overloads **email** and the **subscriber record** to serve three unrelated business concepts:

1. **Customer contact information** — "how do I reach this customer?"
2. **Login identity** — "who is signing into the portal?"
3. *(claimed)* **Ownership** — "which reseller owns this customer?"

The presenting symptom: many customers under one reseller legitimately share a contact email
(`owner@abcnetworks.com`, `wanserverng@gmail.com`), but the system rejects the duplicate, so emails
get mangled into `wanserverng+8265@gmail.com` to satisfy uniqueness.

### What the code actually shows (corrections to the original framing)

- **✅ Ownership is already correct and is NOT derived from email.** A customer belongs to a reseller
  via `Subscriber.reseller_id` → `Reseller.id` (`app/models/subscriber.py:245`); the reseller customer
  list filters `Subscriber.reseller_id == <uuid>` (`app/services/reseller_portal.py:121`). No code
  infers ownership from an email address or a `+suffix`. **Layer 3 of the original concern is a
  non-issue.**

- **✅ A real `Reseller` org entity and a `reseller_users` link table already exist**
  (`app/models/subscriber.py:118`, `:615`).

- **⚠️ A reseller's *login identity* is still a `Subscriber` row.** `reseller_users.person_id` is a FK
  to `subscribers.id`, and the reseller credential (`UserCredential.subscriber_id`) points at that same
  subscriber. So a reseller login occupies a slot in the global email-unique space. (Addressed by
  Layer 3 — out of scope here.)

- **❗ The real blocker is one constraint:** `email: ... unique=True` on `subscribers`
  (`app/models/subscriber.py:204`), backstopped by validation that hard-rejects duplicates
  (`app/services/validation_api.py:69`; `app/services/web_customer_actions.py:1042,1142,1358`).

- **❗ The `+NNNN` workaround is NOT in the codebase.** No creation/import/CRM path generates suffixed
  emails — the app only *rejects* duplicates. The mangled addresses in prod are **data** (legacy BSS import
  or manual). There is no code to delete; un-mangling is a separate data cleanup (follow-up).

- **❗ Hidden coupling:** login resolution treats `Subscriber.email` as a login key
  (`app/services/auth_flow.py:604` and a second resolver at `app/services/web_reseller_auth.py:35`).
  Uniqueness cannot simply be dropped without first removing email-as-login, or shared emails make
  email login ambiguous.

---

## 2. Target model

Separate the three concepts cleanly:

| Concept | Stored as | Uniqueness |
|---|---|---|
| Customer contact info | `Subscriber.email` (+ `phone`) | **None** — plain, indexed, nullable contact field |
| Login identity | `UserCredential.username` (local) / RADIUS / `SystemUser.email` (admins); `Subscriber.email` is a read-only customer alias only when unambiguous | `username` unique per provider=local (already enforced, `app/models/auth.py:51`) |
| Ownership | `Subscriber.reseller_id` | already correct — no change |

Net effect: a reseller may onboard 500 customers all using `owner@abcnetworks.com` as contact email.
Each is a distinct `Subscriber` (distinct `id`, distinct `subscriber_number`, distinct login
credential where one exists). Email remains descriptive contact data.
`auth.customer_login_identity` may use it as a read-only login alias only when
it can select exactly one eligible customer and exactly one active local portal
credential without guessing.

The same rule applies across customer and reseller portal principals: an
existing `Subscriber.email` never blocks a reseller portal principal. A local
`UserCredential.username` remains globally unique, so the reseller onboarding
form collects a distinct username instead of deriving one from the contact
email. Reseller-detail access evidence lists the exact linked portal
principals and their local credential state; matching email alone is never
treated as an identity link.

---

## 3. Scope

**In scope (this PR):**
- **Layer 1** — Drop the global `UNIQUE(email)` on `subscribers`; relax duplicate-email rejection.
- **Layer 2** — Keep `username` as the canonical customer/reseller login key. Customer authentication
  may resolve `Subscriber.email` through the bounded `auth.customer_login_identity` policy. Audit and
  harden every other "find subscriber by email" call site.

**Out of scope (follow-ups, tracked but not built here):**
- **Layer 3** — Move reseller portal users off `Subscriber` rows onto their own principal (add a
  `reseller_user` principal to `UserCredential`, or model like `SystemUser`).
- **Data cleanup** — dry-run-first one-off to un-mangle existing `+NNNN` emails to real addresses and
  de-dupe.

---

## 4. Changes by layer

### Layer 1 — Email becomes non-unique contact info

1. **Model** `app/models/subscriber.py:204`
   `email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)`
   → drop `unique=True`; keep a **non-unique index** for lookup performance. (Keep `nullable=False`
   for now to avoid touching every writer; nullability can relax later.)

2. **Migration** (new revision off current alembic head — see §6)
   - Drop the unique constraint/index backing `subscribers.email` (name varies by environment; the
     migration must introspect and drop whichever of `subscribers_email_key` / unique index exists,
     guarded for both Postgres and SQLite test DB).
   - Create a plain `ix_subscribers_email` index.

3. **Validation** `app/services/validation_api.py:69` `validate_email_unique`
   - Email is no longer an identity → this check should no longer **block**. Make it return
     `(True, None)` (or remove it and its two callers at `:122`, `:165`). Decide whether to keep a
     soft, non-blocking "this email is already used by N other customers" hint for the admin UI.

4. **Creation flows** `app/services/web_customer_actions.py:1042, 1142, 1358`
   - Remove the `raise ValueError("A customer with email ... already exists")` guards. Duplicate
     contact emails are valid.

5. **Error mapping** `app/services/web_system_common.py:54-59`
   - The `people/email already exists` → "Email already exists" mapping becomes dead for the email
     path; keep the **username** mappings (`:52-53,57`) since username uniqueness still applies.

### Layer 2 — Email is a bounded customer-login alias

`UserCredential.username` remains the canonical identity. Customer email resolution is owned by the
typed, read-only `auth.customer_login_identity` service and follows these rules:

1. An exact stored credential username wins first. This preserves existing email-based usernames,
   customer-number usernames, and PPPoE fallback behavior.
2. Contact email matching is case-insensitive and never changes stored data.
3. Any contact email shared by multiple customer records is refused; the customer is asked to use
   their customer number.
4. A sole matching customer is selected only when the customer is active, is not disabled or
   canceled, and has exactly one active local credential with a portal password. Suspended customers
   retain their existing login eligibility.
5. A wrong portal password entered through the email alias participates in the existing credential
   lockout path and must not fall through to PPPoE authentication.
6. System-user email login and reseller username login remain unchanged.

No username, email, or password migration or backfill is required for this alias.

### Layer 2 risk surface — "find subscriber by email" audit

Once email is non-unique, every lookup-by-email can return multiple rows. Each site below must be
triaged: re-key on a stable id, restrict to credentialed accounts, or explicitly accept "first/most
recent" with a logged caveat.

| Site | Today | Required handling |
|---|---|---|
| `app/api/crm_webhooks.py` `_find_existing_customer` | CRM upsert matches by `crm_person_id` then falls back to email `.first()` | **DEFERRED — blocked:** the whole `receive_crm_customer` / `_find_existing_customer` feature is uncommitted parallel-session WIP (not in `main`/HEAD), so this fix cannot ship in this PR without absorbing that work. Required follow-up on the customer-webhook branch: make the email fallback adopt only a *single* legacy record with no CRM link (else shared emails merge distinct customers — would regress the prior 4,499-dup CRM merge). Patch + tests are drafted and ready to graft on. |
| `app/services/auth_flow.py:1415` | lookup by email (password reset / account) | Restrict to accounts that have a login credential; if still ambiguous, refuse rather than guess. |
| `app/services/auth_flow.py:1695` | email-change uniqueness check | Remove/relax — email change no longer needs to be globally unique. |
| `app/services/customer_identity_resolution.py:578` | resolve identity by email | Must tolerate multiple matches; prefer id/`subscriber_number`/credential. |
| `app/services/web_customer_actions.py:1038` | existing-customer lookup in wizard | Becomes advisory ("possible existing match"), not a hard block. |
| `app/services/web_admin_resellers.py:59` | admin reseller-customer lookup by email | Tolerate multiple; surface a chooser or scope by `reseller_id`. |
| `app/services/subscriber.py:729` | bulk lookup `email IN (...)` | Audit consumers for "expects 1 row" assumptions. |
| `app/services/network_subscriber_bridge.py:129,154,167` | inventory match by email | Confirm inventory emails are still distinct enough, or re-key. |

Deliverable for this PR: each row resolved (re-keyed or proven safe), with a one-off SQL audit showing
no two *credentialed* customers shared an email pre-change (logins are unaffected; only contact emails
collapse).

---

## 5. Why this is safe for customer logins

Customer portal auth continues to support customer-number and **RADIUS / PPPoE** identifiers. The
email alias resolves only to an existing active local portal credential, then uses that credential's
unchanged password, lockout, MFA, session, and rate-limit handling. Shared email, multiple active
credentials, disabled/canceled customer state, or inactive customer state fail closed. Admin
(`SystemUser`) email login is untouched.

---

## 6. Migration plan

- Branch off **current `alembic` head**, not this tree's local `159` — prod has advanced (mig 160/161
  merged elsewhere). Run `alembic current` **inside the app container** before authoring the revision
  (standing prod-drift / stamp-wedge gotcha).
- One revision: drop unique on `subscribers.email`, add non-unique `ix_subscribers_email`. Guard for
  both Postgres (prod) and SQLite (test) — introspect the existing constraint/index name rather than
  hard-coding it.
- Idempotent / re-runnable; pg-guarded `IF EXISTS`.

---

## 7. Risks & rollback

- **Ambiguous lookups** (the §4 table) — the main risk. Mitigated by the audit + re-keying.
- **CRM merge hazard** (`crm_webhooks.py`) — highest blast radius; must re-key on `crm_subscriber_id`
  before relaxing uniqueness, or shared-email customers get merged. Echoes the prior 4,499-duplicate
  CRM merge — do not regress it.
- **Ambiguous customer login** if an email is shared — fail closed and ask for the customer number.
- **Authentication fallback bypass** after a wrong email-alias password — prevented by stopping
  before PPPoE fallback while retaining the existing local-credential lockout counter.
- **Rollback:** the resolver/validation changes are pure code (revert PR). The migration's reverse adds
  the unique constraint back — which will **fail if duplicate emails now exist**, so document that the
  downgrade is best-effort and effectively one-way once shared emails are created.

---

## 8. Test plan

- A unique customer contact email logs in with the existing portal password; mixed case also works.
- Two subscribers with identical `email`, distinct `username` → both log in by their own username;
  shared-email login is refused and asks for the customer number.
- Wrong email-alias password updates the existing local lockout state and does not try PPPoE.
- Suspended customers may use the alias; disabled, canceled, and inactive customers may not.
- Existing PPPoE, customer-number, stored email-username, MFA, session, and rate-limit behavior remains.
- Admin `SystemUser` still logs in by email.
- CRM webhook with a shared email upserts the correct subscriber by `crm_subscriber_id` (no merge).
- Password reset for a shared email behaves deterministically (per §4 decision).
- Migration up/down on SQLite (test) and a Postgres check.

---

## 9. Open questions / follow-ups

1. **Layer 3** — give reseller portal users their own principal so a reseller login no longer needs a
   `Subscriber` row. Larger schema/auth change; separate design.
2. **Data cleanup** — dry-run-first one-off to un-mangle `+NNNN` → real email and de-dupe (follow the
   team's reconcile-CLI pattern: dry-run, `--execute`, run inside container).
3. **Password-reset-by-email policy** under shared emails — confirm desired behavior (refuse vs. send
   to all matching credentialed accounts).
4. Soft "email already used by N customers" hint in admin UI — keep or drop?
