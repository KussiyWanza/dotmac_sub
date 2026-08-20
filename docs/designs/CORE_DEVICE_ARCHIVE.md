# Core device archive lifecycle

Status: implemented contract

Owner: `network.core_device_archive`

## Decision

Core-device archive is a reversible administrative retirement. It does not
delete `NetworkDevice`, monitoring history, interfaces, metrics, audit records,
or its rebuildable projection. The three lifecycle states are:

- `active`: admitted to monitoring;
- `inactive`: retained in current inventory but not admitted to monitoring;
- `archived`: hidden from current inventory and available from the archived
  cohort for review and restoration.

`archived` is the stable persistence, command, event, and query vocabulary.
Operator-facing network-device surfaces call this state **Decommissioned** and
call the archive action **Decommission Device**. This is a presentation label,
not a second lifecycle state or a destructive deletion contract. It must not be
confused with the separately owned permanent ONT decommission workflow.

Restore clears the archive tombstone and returns the device as `inactive`.
Re-admission is a separate operator decision so restore cannot assert that an
unverified device is working.

While the tombstone exists, the archive owner rejects edits, provisioning
credential changes, interface-monitoring changes, graph configuration, backup
configuration or triggers, ping, and reboot actions. The existing adapters call
one typed mutation-eligibility query, so a manually submitted legacy URL cannot
bypass the read-only archived state. Historical detail, graphs, and backups
remain readable; live interface collection is not attempted for an archived
device. The generic monitoring API applies the same guard to device edits,
deactivation, and interface mutations.

## Eligibility and stale evidence

The archive preview is authoritative. It fails closed when customer impact
cannot be calculated and blocks archive while the exact device has active child
devices, reviewed forwarding declarations, an active linked NAS/router record,
or active customers in its failure domain. Confirmation locks the device and
recomputes the preview fingerprint so changed dependencies cannot be archived
from stale evidence.

## Projection and repair

`network.device_projection` continues to project archived rows. Default device
queries exclude `archived`; the explicit archived cohort reads them and offers
restore. Reconciliation derives the archive marker from `NetworkDevice`, forces
its operational result to `not_working`, and cannot reactivate it. External
inventory synchronization may update observations but only the restore command
may clear the archive tombstone.

The unified network-device worklist keeps `operational_status` and
`lifecycle_state` as separate authoritative inputs.
`ui.network_device_status_presentation` applies this display precedence without
rewriting either input:

1. `archived` lifecycle -> **Decommissioned**;
2. `inactive` lifecycle -> **Inactive**;
3. active plus `working` -> **Online**;
4. active plus `not_working` -> **Offline**.

The Decommissioned presentation uses a neutral tone and archive icon. A
decommissioned row therefore cannot present as an ordinary Offline outage even
though its separately retained binary operational result remains
`not_working`. Default inventory continues to exclude decommissioned rows; the
explicit Decommissioned Devices cohort supports review and restoration.

## Unified device page contract

- Screen: `/admin/network/devices`, list/work surface for NOC and network
  administrators.
- Read owners: `network.device_projection` for projected lifecycle and
  operation, sourced from `network.device_state`,
  `network.monitoring_inventory`, and `network.core_device_archive`;
  `ui.network_device_status_presentation` owns the combined label, tone, and
  icon.
- Command and eligibility owner: `network.core_device_archive` for core devices
  only. Other device types retain their own lifecycle owners and do not inherit
  this action.
- First-viewport state: identity, type, lifecycle-aware status, management IP,
  relevant relationship, last observation, and eligible actions.
- Action: **Decommission Device** is permission-gated, opens the existing
  authoritative impact preview, requires a reason and confirmation, and is
  also available as an explicit secondary action on current core-device detail.
- Restoration: **Restore Device** returns the record to inactive inventory and
  never claims the device is Online.
- Unauthorized actions remain hidden. Blocked confirmation renders the exact
  owner-provided blockers. Internal route names, permission keys, event types,
  and `lifecycle=archived` query values remain stable compatibility contracts.

## Evidence

Archive and restore stage typed audit and domain-event evidence in the same
owner-managed transaction as the authoritative state. The events are
`network_device.archived` and `network_device.restored`. Permanent deletion is
not part of this contract.
