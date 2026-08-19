# WhatsApp phone-number-ID manifest adoption

## Production procedure

This is an operator procedure. Do not perform it from a local, staging, or
test environment.

1. Identify the exact enabled WhatsApp installation pinned to manifest `1.0.0`.
2. Preview the manifest adoption and record both the installed pin and the
   deployed WhatsApp `1.1.0` pin. Confirm the existing configuration and
   capability bindings are compatible.
3. Use the typed `integration.installations.manifest_adoption` command with the
   exact expected installed pin and the reviewed `1.1.0` target pin. This is an
   explicit, audited adoption; do not edit the version or digest directly.
4. Save the WhatsApp configuration with the real Meta `phone_number_id`.
   The save creates a new revision, scopes `messaging.send.v1`,
   `messaging.receive.v1`, and `messaging.templates.read.v1` to that ID, runs
   static and runtime validation, then enables the installation and bindings.
5. Verify that all three bindings are enabled and have the same
   `scope_json.phone_number_id`. Confirm inbound observations use the same
   provider account scope before enabling production traffic.

Secrets remain references (for example `bao://...`); never enter secret values
in the config form, event evidence, or this runbook.
