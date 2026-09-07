# Sub direct external-connector surface

Sub adopts the Governance-owned schema-9 ratchet from accepted ADR 0011 at
immutable canonical-main commit
`a19259b10568d29dc0a9617347498fea7f1e7a97`.

The ratchet freezes measured legacy connector surface while providers move
behind Dotmac Integrator. It is transitional defence in depth, not runtime
isolation. The permanent boundary is Integrator-only connector packages and
provider secrets, default-deny product egress, provider ingress terminating at
Integrator, and versioned inbox/outbox contracts between independent apps.

Sub declares no measurement roots and copies no detector. The Governance
engine derives scope from Git-tracked Python, proves test-only reachability
centrally, and reports every untracked Python source as an error.

## Accepted baseline

Measured on 2026-08-16 against current `origin/dev` with the accepted schema-9
engine: 2,984 tracked Python sources measured, 1,796 proven test-only sources
excluded, zero untracked Python, 15 conserved findings, and no syntax errors.

| Category | Baseline |
| --- | ---: |
| `outbound_transport` | 43 |
| `webhook_surface` | 4 |
| `provider_credential` | 3 |
| `connector_task` | 18 |
| `sync_checkpoint` | 11 |
| `delivery_retry` | 7 |

### `outbound_transport` — 43 files

`app/services/ai/client.py`, `app/services/ai/voice_transcription.py`,
`app/services/bandwidth_metrics_adapter.py`, `app/services/core_router_metrics.py`,
`app/services/crm_client.py`, `app/services/dotmac_erp/client.py`,
`app/services/email.py`, `app/services/genieacs_client.py`,
`app/services/geocode_reconciler.py`, `app/services/geocoding.py`,
`app/services/infrastructure_health.py`,
`app/services/integrations/connectors/http_webhook.py`,
`app/services/integrations/connectors/meta_social_runtime.py`,
`app/services/integrations/connectors/nextcloud_talk.py`,
`app/services/integrations/connectors/payment_gateway.py`,
`app/services/integrations/connectors/whatsapp_runtime.py`,
`app/services/meta_oauth.py`, `app/services/meta_pages.py`,
`app/services/metrics_store.py`, `app/services/monitoring_metrics.py`,
`app/services/nas/_mikrotik.py`, `app/services/nas/provisioner.py`,
`app/services/network/metrics_adapters.py`,
`app/services/network/olt_polling_metrics.py`,
`app/services/network/olt_rest_client.py`, `app/services/nextcloud_talk.py`,
`app/services/nin_service.py`, `app/services/notification_adapter.py`,
`app/services/push.py`, `app/services/router_management/connection.py`,
`app/services/secrets.py`, `app/services/sms.py`,
`app/services/team_inbox_media.py`, `app/services/uisp.py`,
`app/services/web_integrations.py`, `app/services/web_network_monitoring.py`,
`app/services/web_system_export_tool.py`, `app/tasks/tr069.py`,
`app/team_inbox_smtp.py`, `scripts/network/bulk_tr069_rebind.py`,
`scripts/network/setup_genieacs.py`,
`scripts/one_off/send_important_account_batch.py`, and
`scripts/testing/smoke_links.py`.

### `webhook_surface` — 4 files

`app/api/crm_webhooks.py`, `app/main.py`,
`app/services/integrations/payment_capability.py`, and
`app/web/admin/integrations.py`.

### `provider_credential` — 3 files

`app/api/meta_inbox_webhooks.py`, `app/config.py`, and
`app/services/object_storage.py`.

### `connector_task` — 18 files

`app/services/web_integration_syncs.py`, `app/tasks/crm_ticket_pull.py`,
`app/tasks/dotmac_erp_outbox.py`,
`app/tasks/forwarding_control_observations.py`, `app/tasks/gis.py`,
`app/tasks/infrastructure_polling.py`, `app/tasks/integration_delivery.py`,
`app/tasks/integrations.py`, `app/tasks/monitoring_cleanup.py`,
`app/tasks/profile_sync.py`, `app/tasks/radius.py`,
`app/tasks/radius_population.py`, `app/tasks/router_sync.py`,
`app/tasks/topology_lldp.py`, `app/tasks/topology_uisp.py`,
`app/tasks/tr069.py`, `app/web/admin/integrations.py`, and
`app/web/admin/system.py`.

### `sync_checkpoint` — 11 files

`app/models/erp_domain_sync.py`, `app/models/external.py`,
`app/models/field_material.py`, `app/models/integration_platform.py`,
`app/models/network_monitoring.py`, `app/models/quote_mirror.py`,
`app/schemas/external.py`, `app/services/external.py`,
`app/services/field/material_catalog.py`,
`app/services/integration_sync.py`, and
`app/services/team_inbox_audit_reconstruction.py`.

### `delivery_retry` — 7 files

`app/services/ai/client.py`, `app/services/ai/voice_transcription.py`,
`app/services/meta_pages.py`, `app/services/router_management/connection.py`,
`app/services/web_integrations.py`, `app/tasks/tr069.py`, and
`app/web/admin/integrations.py`.

## Conserved findings

These are connector-shaped symbols removed by the central test-only
reachability proof. Recording them does not suppress anything or claim the
files are harmless; it prevents the subtraction from changing silently.

| Path | Symbol | Category | Fingerprint |
| --- | --- | --- | --- |
| `tests/services/topology/test_coverage_metrics.py` | `<module>` | `outbound_transport` | `51fa94be3406bf06f9ae28167b6ac0160b8567ff79d95b4e9a88dded6fdc2b2e` |
| `tests/test_ai_gateway.py` | `<module>` | `delivery_retry` | `8592de9de918a715812dd3b241f2f00930b30bc617bdf2674fdfc16abb797399` |
| `tests/test_ai_gateway.py` | `<module>` | `outbound_transport` | `8592de9de918a715812dd3b241f2f00930b30bc617bdf2674fdfc16abb797399` |
| `tests/test_crm_client_resilience.py` | `<module>` | `outbound_transport` | `58bc472074bb7bec95639e2575065066c05d8043983fa49bb10f3858d29320b0` |
| `tests/test_crm_ticket_pull.py` | `test_latest_crm_updated_at_watermark` | `sync_checkpoint` | `03c45fad7c1a26e5c6ade4222d19ad0190ce37795403d89308d1c3fd23143aeb` |
| `tests/test_email_services.py` | `test_send_email_auth_failure_logs` | `outbound_transport` | `e21f75d7c74d8996bad3d29f4876bcce708c336102a88f3bd2474350f929b10e` |
| `tests/test_email_services.py` | `test_smtp_connection_auth_failure_logs` | `outbound_transport` | `e21f75d7c74d8996bad3d29f4876bcce708c336102a88f3bd2474350f929b10e` |
| `tests/test_genieacs_services.py` | `<module>` | `outbound_transport` | `cfc1341f06e824ad2bfc34a59d5adece457c40bea620b65d7badd43779956d99` |
| `tests/test_integration_meta_social.py` | `test_typed_facade_returns_sanitized_outcome` | `outbound_transport` | `fc7bb83c3ed15b6a49fa08d1d7991abaed40008d6704adb24afb268e64d96670` |
| `tests/test_meta_oauth.py` | `test_provider_rejection_records_only_sanitized_evidence` | `outbound_transport` | `d081abb25b5f09a8dc4658709a0103d25b9d866728c1dc80594621e32fb3a582` |
| `tests/test_nextcloud_talk_staff_notifications.py` | `<module>` | `delivery_retry` | `2ad03d19e91abbbd467037d4672ba61b4433b2533ead11b04955f1873b6017af` |
| `tests/test_router_management_connection.py` | `test_execute_honors_tunable_overrides` | `delivery_retry` | `c887e359adc3583a9c7332c37a3fb951afdad207057a5e96fb02cd136463d31e` |
| `tests/test_team_inbox_meta_social_webhook.py` | `<module>` | `provider_credential` | `2cfb33c493921333d14bb1a84b0426ad12e15e535a8f752d0add3ff9e9d33002` |
| `tests/test_team_inbox_smtp_runtime.py` | `test_readiness_uses_smtp_noop` | `outbound_transport` | `2df88d6ae0dba969bd2097a5459861379cf68d42b8542974efb0382c0a92e7f7` |
| `tests/test_team_inbox_whatsapp_webhook.py` | `<module>` | `provider_credential` | `6b5ec793e3c6236cda3fd4a2acac14846cc9c8970a068551ed3b3bb8bfb2c3fc` |

The 2026-09-05 review re-recorded `test_coverage_metrics.py` after its LLDP
wrapper assertions moved to the typed detached-poll contracts. The file remains
test-only, and its existing HTTP error fixture does not add a runtime transport.

## Review rule

A count rising fails. A count falling also fails until the profile and this
record are lowered in the same change. Every reduction must show deletion or a
cutover to a named connector distribution behind Dotmac Integrator.

The ratchet reaches its sunset only when all baselines and conserved findings
are zero and ADR 0011's runtime package, secret, egress, ingress, and contract
conditions hold simultaneously.
