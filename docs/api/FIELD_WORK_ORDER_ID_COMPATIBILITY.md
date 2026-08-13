# Field API work-order identifier compatibility

`work_order_id` is the canonical public field-API name. During the mobile
migration, affected expense-request, material-request, and attachment inputs
also accept the deprecated `crm_work_order_id` name.

- Responses expose only `work_order_id`.
- Matching canonical and legacy values are accepted.
- Conflicting values fail with HTTP 422.
- Internal `crm_work_order_id` names remain provenance or persistence details;
  they are not the public contract.

## Retirement condition

Remove the input alias after all supported field-app releases send
`work_order_id`, queued offline records from the last legacy release have aged
past the supported synchronization window, and API telemetry shows no legacy
input during one complete release-support window. Removing the alias is a
coordinated breaking contract change and must include an OpenAPI manifest update.
