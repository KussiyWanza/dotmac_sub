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

## Evidence

Archive and restore stage typed audit and domain-event evidence in the same
owner-managed transaction as the authoritative state. The events are
`network_device.archived` and `network_device.restored`. Permanent deletion is
not part of this contract.
