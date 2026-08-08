# Staff Login Identity Reconciliation

## Decision

`auth.staff_provisioning` is the sole writer for staff profile identity, local
login identity, staff activation state, and recovery preparation. For every
staff `SystemUser`, the normalized email and the username of its one local
`UserCredential` must be equal, including while either record is inactive.

The email remains the human-facing login identifier. Credential state is an
internal authentication record and cannot independently choose another staff
username.

## Previous failure mode

The administrative profile adapter updated only an active credential when an
email changed. A disabled credential therefore kept its old username. A later
activation or invitation could reactivate that stale, non-empty username, so
the profile card showed a new email while the credential still used the old
login value.

The old writers were the admin user-edit helper, self-profile adapters, and the
invite/reset helper. These are now thin adapters around typed commands owned by
`auth.staff_provisioning`.

## Command behavior

- `UpdateStaffIdentityCommand` locks the staff principal and its local
  credential, checks identity conflicts, updates both email and username in one
  transaction, and does so even when the credential is disabled.
- `SetStaffAccountActiveCommand` rechecks the invariant before activation and
  creates a missing placeholder credential or reconciles a stale username
  before enabling the credential.
- `PrepareStaffCredentialRecoveryCommand` is required before an invite or
  password-reset delivery. It creates a missing credential, when unambiguous,
  or reconciles and enables the existing credential.
- `ReconcileStaffLoginIdentityCommand` repairs reviewed missing, username, and
  activation drift only when the supplied email digest still matches the
  preview, preventing a stale repair from overwriting newer identity state.

Login-identity and administrative password changes revoke active sessions and
invalidate authentication caches after commit. Audit and event payloads
contain identifiers and an email digest, not an email address, password, reset
token, or other credential secret.

Identity commands acquire normalized-email advisory locks in sorted order
before locking the staff principal and local credential rows. If the email
changes between observation and lock acquisition, the command fails closed and
requires a fresh reviewed retry.

## Read model and operator behavior

The Login Credentials card reads local credentials whether active or inactive.
It distinguishes active, disabled, missing, multiple, and username-mismatch
states. A mismatch can be corrected by a normal reviewed profile save. Missing
or multiple credentials require the owner repair workflow rather than a direct
database edit.

### Login Credentials card contract

- Screen: administrative system-user detail; audience: authorized staff access
  administrators.
- Decision: whether the profile email, local login identity, and access state
  are aligned and whether login recovery can safely proceed.
- Read and status owner: the typed staff credential view composed from
  `auth.staff_provisioning` state; command and eligibility owner:
  `auth.staff_provisioning`; authorization owner: `auth.permission_gate`.
- Glance fields: local username, active/disabled/reconciliation status,
  password-change requirement, last password change, and a plain-language
  issue reason.
- Actions: profile save for an unambiguous mismatch, invitation, and password
  reset. Recovery actions are disabled with a reason for inactive, duplicate,
  or conflicting state and are rechecked by the command owner.
- Empty/error states distinguish missing, multiple, conflicting, disabled, and
  mismatched credentials. Color is accompanied by text, and the same facts are
  preserved when the card stacks on mobile.

The one-off reconciliation command is dry-run by default. Apply mode requires
an explicit actor, reason, idempotency prefix, and the exact preview
fingerprint. It automatically repairs unambiguous username mismatches, missing
credentials, and activation-only drift while preserving the authoritative
account activation state. Duplicate or conflicting credentials remain blocked
for individual review.

## Migration and cutover

1. Deploy the owner commands and adapter cutover together.
2. Run the reconciliation command in preview mode and review aggregate issue
   counts.
3. Apply the exact reviewed fingerprint for unambiguous username, missing, and
   activation drift.
4. Resolve duplicate or conflicting credentials individually, then repeat
   preview until drift is zero.
5. After zero drift is proven in production and rollback evidence is retained,
   consider a separate migration enforcing at most one local credential per
   system user at the database boundary.

Rollback restores the application release but does not reverse already aligned
usernames. Those values are canonical projections of staff email and remain
valid under the previous reader behavior.

## Follow-up identity stability

Email is currently the staff provisioning natural key. Moving ERP integration
to a stable employee identifier is a separate contract change: expand the
schema, backfill and verify identifiers, shadow matching, cut over the ERP
contract, and retire email-as-record-identity only after parity is proven.

## Verification

Focused owner tests cover inactive credential updates, activation,
recovery preparation, conflicts, stale repair evidence, drift discovery, and
session revocation, including proof that the old active login identifier no
longer resolves. Route/read-model tests cover the card and typed adapter calls.
Architecture tests prevent direct profile or credential identity writers from
returning outside `auth.staff_provisioning`.
