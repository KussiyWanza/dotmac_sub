# Workforce Attendance Integration

Status: implemented; production rollout is controlled by the version-pinned
connector installation and its two explicit capability bindings.

Dotmac ERP is the sole owner of attendance identity resolution, daily state,
shift and timezone policy, geofence decisions, lateness, early exit, working
hours, and persistence. Selfcare is an authenticated capture and transport
surface only; it stores no attendance ledger or pending/offline punch.

The provider-neutral Selfcare contracts are
`workforce.attendance.read.v1` and `workforce.attendance.punch.v1`. The current
version-pinned `dotmac.erp` connector implements them using server-side
credentials.
Browser requests terminate at CSRF-protected `/admin/dashboard/attendance/*`
routes. Native field-app requests terminate at bearer-authenticated
`/api/v1/field/attendance/*` routes. Neither client receives the ERP credential.

Identity is fixed by the authenticated Selfcare `SystemUser.id`. ERP resolves
that subject to exactly one active, Selfcare-enabled employee through
`Employee.dotmac_sub_account_id` inside the service principal's organization.
Email, browser-supplied employee IDs, and browser-supplied organization IDs are
not fallback identities.

Each punch captures fresh browser or native-device latitude, longitude,
accuracy, and observation time. Selfcare forwards those untrusted observations unchanged after schema
validation. ERP server time is authoritative, and ERP alone accepts or rejects
the geofence result. Mutations use ERP platform idempotency and the dashboard
reads ERP after an ambiguous timeout rather than inferring success.

The field app offers `Check In`, `On shift`, and `Check Out` in its location
card. After a fresh ERP observation confirms `checked_in`, the field app
immediately requests location sharing with presence status `on_shift`; the
field location-sharing adapter repeats that attendance check server-side before
it accepts enablement. If that follow-up request fails, the confirmed attendance
state remains visible and `On shift` provides an explicit retry rather than
implying that sharing started. Attendance punches are online-only and are never
written to the mobile offline queue. After ERP confirms checkout, the client
immediately stops local tracking and requests the server presence projection be
switched to `off_shift`.

Attendance is synchronous request/response capability traffic, not a webhook.
ERP material-status and staff-access webhooks are separate contracts and do not
change attendance state.

Production enablement requires all of the following at the same time:

- an enabled `dotmac.erp` installation whose pinned manifest declares
  `workforce.attendance.read.v1` and `workforce.attendance.punch.v1`;
- enabled bindings for the attendance capabilities;
- an ERP service principal with explicit `sub:attendance:read` and
  `sub:attendance:write` scopes; and
- exactly one active ERP employee whose `dotmac_sub_account_id` is the
  authenticated Selfcare `SystemUser.id` and whose Selfcare access is enabled.

ERP's allowlisted employee eligibility codes cross the connector boundary without
provider messages or response bodies. Its user-scoped browser cache retries
transient unavailability after five minutes, but caches a permanent mapping,
inactive-employee, disabled-access, or authorization refusal for twelve hours.
This prevents an ineligible account from polling ERP every ten minutes while
still allowing a later operator correction to take effect without a deployment.

## Operator remediation workflow

Treat mapping and access failures as employee-data corrections, not connector
or deployment failures:

1. For `employee_not_linked`, set exactly one active ERP employee's
   `dotmac_sub_account_id` to the affected Selfcare `SystemUser.id`.
2. For `employee_mapping_ambiguous`, remove duplicate subject mappings until
   exactly one active employee remains.
3. For `employee_inactive` or `attendance_disabled`, confirm that the employee
   is meant to use Selfcare attendance before activating the employee or
   enabling `dotmac_sub_access_enabled`. Otherwise remove the local
   `attendance:self:use` permission.
4. For `authorization_failed`, verify the installed connector binding and the
   service principal's explicit attendance scope before rotating credentials.
5. Recheck the corrected subject through the attendance dashboard. Confirm a
   successful today response and confirm that the previous stable error code no
   longer recurs in logs.

Employee mapping and access corrections take effect independently of an
application deployment. Application changes follow the repository release
workflow: merge the source PR with one version-impact label, let automation own
the version bump, promote the resulting immutable image digest to staging, and
complete staging acceptance before production promotion.

The compact dashboard action control is a user-specific lazy partial beside the
Add Customer action, outside the shared dashboard cache. ERP failure degrades
only that control. While checked in, the browser may render a `HH:MM:SS` timer
from ERP's confirmed `check_in_at`; after checkout it freezes using ERP's
confirmed `check_out_at`. This is display-only and never becomes attendance
evidence or a local work-hours calculation.

Overnight shifts remain excluded from v1 because ERP's existing next-morning
checkout lookup requires a separate domain fix. Selfcare must render the ERP
pilot-exclusion state without offering a punch.
