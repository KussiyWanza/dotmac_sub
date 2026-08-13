# Workforce Attendance Integration

Status: implemented locally; production rollout disabled.

Dotmac ERP is the sole owner of attendance identity resolution, daily state,
shift and timezone policy, geofence decisions, lateness, early exit, working
hours, and persistence. Selfcare is an authenticated capture and transport
surface only; it stores no attendance ledger or pending/offline punch.

The provider-neutral Selfcare contracts are
`workforce.attendance.read.v1` and `workforce.attendance.punch.v1`. The current
`dotmac.erp` 1.1.0 connector implements them using server-side credentials.
Browser requests terminate at CSRF-protected `/admin/dashboard/attendance/*`
routes and never receive the ERP credential.

Identity is fixed by the authenticated Selfcare `SystemUser.id`. ERP resolves
that subject to exactly one active, Selfcare-enabled employee through
`Employee.dotmac_sub_account_id` inside the service principal's organization.
Email, browser-supplied employee IDs, and browser-supplied organization IDs are
not fallback identities.

Each punch captures fresh browser latitude, longitude, accuracy, and observation
time. Selfcare forwards those untrusted observations unchanged after schema
validation. ERP server time is authoritative, and ERP alone accepts or rejects
the geofence result. Mutations use ERP platform idempotency and the dashboard
reads ERP after an ambiguous timeout rather than inferring success.

The compact dashboard action control is a user-specific lazy partial beside the
Add Customer action, outside the shared dashboard cache. ERP failure degrades
only that control. While checked in, the browser may render a `HH:MM:SS` timer
from ERP's confirmed `check_in_at`; after checkout it freezes using ERP's
confirmed `check_out_at`. This is display-only and never becomes attendance
evidence or a local work-hours calculation.

Overnight shifts remain excluded from v1 because ERP's existing next-morning
checkout lookup requires a separate domain fix. Selfcare must render the ERP
pilot-exclusion state without offering a punch.
