# AI intake classifier fact contract

The classifier prompt now includes the authoritative Pydantic fact schema,
including its enum definitions, plus explicit true/false/null instructions for
router power and restart facts. Unstated nullable booleans remain null, not false
or the string unknown. The schema is generated from the validator rather than a
second hand-maintained list and cached for the lifetime of the process.

Validation remains strict; no coercion of yes/no, quoted booleans or unsupported
enums is introduced. Invalid output still enters the existing bounded classifier
failure/exhaustion path for orchestration to hand off. No routing, permissions,
retry budget or human handoff authority changes. Synthetic response regressions
cover valid nullable booleans, schema equality and exhausted invalid output.
Provider behavior is not guaranteed by a prompt: monitor invalid-output rates
and validate exhausted-session human handoff after approved deployment.
