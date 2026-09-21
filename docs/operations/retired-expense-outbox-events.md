# Retired pre-approval expense envelopes

Delivery continues to refuse legacy submit/approve/reject expense envelopes,
including the existing default-submit interpretation. A refusal now records the
supported terminal dead status with reason retired_preapproval_expense_event,
then commits the outbox disposition. Payload, source identity, stable idempotency
key and attempt count are preserved. The source expense is not changed and no
ERP request is made. Pending sweeps stop selecting the same retired row.

This is a bounded lazy cleanup through the existing delivery owner, not a bulk
migration. A full batch of retired rows may consume one pass, but is removed
from the next pass so it cannot indefinitely starve valid work. Inspect these
terminal rows as retired protocol evidence, not transient delivery failures;
do not bulk-redrive them. Manually requeued legacy envelopes are still refused.
Current v2/v3 expense protocols and non-expense delivery retain their existing
ownership, dependency and idempotency guards.
