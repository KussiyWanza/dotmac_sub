# OLT backup outcomes

Every selected active OLT must produce a usable configuration or a recorded
failure. Missing credentials, adapter failures, short/empty output and shell
prompt timeouts are errors, not skips. The actual fetch failure reason reaches
the existing backup notification path. No credential, network, host-key or
vendor-command policy is weakened by this change.

The task now returns completed/partial/failed/skipped together with target count,
unprocessed count and timeout evidence. No eligible targets is an explicit
skipped run; zero successes with failures is failed. Soft time limits propagate
out of the transport/fetch loop, preserve completed fetches for persistence and
skip retention cleanup. Failure to commit that persistence is raised, not
reported as a successful backup. Already-detached SSH targets and short database
persistence transactions remain intact.

Check per-device last successful backup age and verify saved configuration
content after approved deployment. This PR corrects reporting and failure
handling; it does not configure missing credentials or prove restore capability.
