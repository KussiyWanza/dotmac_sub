# OLT / ONT / ACS Architecture

This document maps the relationships between OLT, ONT, and ACS services in the DotMac Sub codebase.

## Operational Sequence

Provisioning is staged so live OLT writes only happen after the shared foundation and OLT-specific readiness gates are complete.

| Phase | Responsibility | Main modules |
|-------|----------------|--------------|
| 1. Foundation setup | Configure VLANs, speed profiles, ONU types, zones, IP pools, TR-069 ACS servers, and optional WireGuard access before touching live OLTs. | `app/web/admin/network_tr069.py`, `app/web/admin/wireguard.py`, `app/services/web_network_tr069.py`, `app/services/web_network_vlans.py`, `app/services/network/speed_profiles.py`, `app/services/network/onu_types.py`, `app/services/network/zones.py`, `app/services/wireguard.py` |
| 2. OLT onboarding | Create the OLT record with vendor/model, management IP, credentials, ACS assignment, VLAN/IP-pool scope, config-pack defaults, and backup settings. | `app/web/admin/network_olts_inventory.py`, `app/services/web_network_olts.py`, `app/services/network/olt.py`, `app/services/network/olt_web_forms.py`, `app/services/network/olt_config_pack.py` |
| 3. Connectivity and protocol validation | Test and operate against OLTs via SSH, NETCONF, and REST where supported. SNMP collection is owned by Zabbix. Huawei SSH CLI remains the primary write path, with protocol adapters selecting the backend. | `app/services/network/olt_protocol_adapters.py`, `app/services/network/olt_ssh.py`, `app/services/network/olt_ssh_session.py`, `app/services/network/olt_ssh_pool.py`, `app/services/network/olt_netconf.py`, `app/services/network/olt_rest_client.py`, `app/services/network/olt_vendor_adapters.py` |
| 4. Config-pack readiness | Validate that the OLT has authorization profiles, internet and management VLANs, a management IP pool, ACS assignment, and an OLT-local TR-069 profile ID. | `app/services/network/olt_config_pack.py`, `app/services/network/olt_readiness_validator.py`, `app/services/network/acs_reachability.py`, `app/services/network/olt_profile_resolution.py` |
| 5. Inventory and topology sync | Model shelves, cards, ports, PON interfaces, SFPs, power units, hardware inventory, linked monitoring devices, and topology views. Hardware inventory reads SNMP Entity MIB data collected by Zabbix. | `app/services/network/olt_inventory.py`, `app/services/network/olt_hardware_discovery.py`, `app/services/network/olt_web_topology.py`, `app/web/admin/network_pon_interfaces.py`, `app/web/admin/network_olts_profiles.py`, `app/tasks/olt_hardware_discovery.py` |
| 6. Customer-service assignment | Bind one exact subscription and modeled PON to an ONT through the normal assignment command owner. UISP, RADIUS, ACS, authorization, and topology imports provide observations or candidates; they never infer this customer decision. | `app/services/network/ont_assignment_commands.py`, `app/services/web_network_ont_assignments.py`, `app/services/field_equipment.py` |
| 7. ONT authorization and provisioning | Assigned **Authorize & provision** applies customer service only for an exact active assignment. Assignment-free work is a separate expiring management-only commissioning intent. | `app/services/network/ont_commissioning.py`, `app/services/network/ont_authorization.py`, `app/services/network/ont_provision_steps.py` |
| 8. Backup, config audit, and drift checks | Capture OLT running-config backups over SSH, audit backups and live config-pack assumptions against intended state, and retry failed compensation entries. Drift checks are read-only guardrails by default. | `app/tasks/olt_config_backup.py`, `app/services/network/olt_config_audit.py`, `app/services/network/olt_config_pack_live_audit.py`, `app/tasks/provisioning.py` |

## Polling, Monitoring, and Status

Zabbix owns OLT/ONT SNMP collection. DotMac does not run direct OLT SNMP polling for status or hardware inventory; it ingests Zabbix walk items and combines them with ACS runtime refresh, optical metrics, signal thresholds, and stale-device cleanup.

Main modules: `app/services/network/olt_polling.py`, `app/services/network/olt_polling_metrics.py`, `app/services/network/ont_metrics.py`, `app/services/network/ont_status.py`, `app/services/network/signal_thresholds.py`, `app/tasks/zabbix_ingestion.py`, `app/tasks/zabbix_sync.py`.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                    ENTRY POINTS                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐                   │
│  │  Web Routes      │  │  API Routes      │  │  Celery Tasks    │                   │
│  │  (admin/network) │  │  (api/v1)        │  │  (tasks/)        │                   │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘                   │
└───────────┼─────────────────────┼─────────────────────┼─────────────────────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              EXECUTION LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    ont_authorization.py                                         │ │
│  │         authorize_autofind_ont_and_provision_network_audited()                 │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    ont_authorization.py                                         │ │
│  │    authorize_autofind_ont_and_provision_network_audited()                       │ │
│  │    ├─ Resolve authorization line/service profiles                               │ │
│  │    ├─ Delete existing registration (if force_reauthorize)                       │ │
│  │    ├─ Authorize via protocol adapter                                            │ │
│  │    ├─ Create or update ONT inventory state                                      │ │
│  │    ├─ Record verified device/PON state and allocate management IP               │ │
│  │    ├─ Never infer or create the customer-service assignment                     │ │
│  │    └─ Apply ACS foundation and wait for ACS bootstrap when TR-069 is configured │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
└───────────────────────────────────┼─────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
│  OLT PROTOCOL     │   │  PROVISIONING         │   │  ACS/TR-069           │
│  ADAPTERS         │   │  COORDINATOR          │   │  LAYER                │
└───────────────────┘   └───────────────────────┘   └───────────────────────┘
```

## OLT Protocol Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              OLT PROTOCOL LAYER                                      │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │              olt_protocol_adapters.py (1,991 lines)                             │ │
│  │  OltProtocol: SSH | NETCONF | REST | AUTO                                       │ │
│  │  Auto-selects protocol based on OLT capabilities                                │ │
│  │  Falls back to next protocol on failure                                         │ │
│  └─────────┬──────────────────────┬──────────────────────┬────────────────────────┘ │
│            │                      │                      │                          │
│            ▼                      ▼                      ▼                          │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐                   │
│  │    SSH          │   │   NETCONF       │   │    REST         │                   │
│  │  (Primary)      │   │  (GPON YANG)    │   │   (API)         │                   │
│  └────────┬────────┘   └─────────────────┘   └─────────────────┘                   │
│           │                                                                          │
└───────────┼──────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              OLT SSH LAYER                                           │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    olt_ssh_pool.py (Connection Pool)                            │ │
│  │  PooledConnection: transport, channel, policy, OLT metadata                     │ │
│  │  SshConnectionPool: Thread-safe, per-OLT max 2, TTL 5min, 100 reuses           │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    olt_ssh.py (1,567 lines)                                     │ │
│  │  Low-level SSH: Paramiko, CLI parsing, TextFSM                                  │ │
│  │  _run_huawei_cmd(), _read_until_prompt(), _open_shell()                         │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    olt_ssh_ont/ (Subpackage)                                    │ │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐    │ │
│  │  │lifecycle.py│ │ status.py  │ │ iphost.py  │ │omci_config │ │  tr069.py  │    │ │
│  │  │authorize   │ │get_status  │ │configure_ip│ │wan/pppoe   │ │bind_profile│    │ │
│  │  │deauthorize │ │find_serial │ │clear_ip    │ │wifi/lan    │ │unbind      │    │ │
│  │  │reboot      │ │            │ │            │ │            │ │            │    │ │
│  │  │factory_rst │ │            │ │            │ │            │ │            │    │ │
│  │  └────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘    │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Provisioning Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           PROVISIONING LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │              provisioning_coordinator.py (1,041 lines)                          │ │
│  │  Phases: olt_registration → service_port → mgmt_ip → tr069_bind →              │ │
│  │          acs_discovery → acs_config_push → verification                         │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │              ont_provisioning/executor.py (875 lines)                           │ │
│  │  Execute delta steps with compensation-based rollback                           │ │
│  │  Single SSH session for all commands                                            │ │
│  │  CompensationEntry: undo commands registered BEFORE execution                   │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## ACS / TR-069 Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           ACS / TR-069 LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    genieacs_client.py (Concrete GenieACS client)                          │ │
│  │  AcsClient Protocol: Structural interface for ACS backends                      │ │
│  │  create_genieacs_client(): Factory for server-specific client                        │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    genieacs.py (HTTP Client)                                    │ │
│  │  GenieACSClient: REST API for GenieACS NBI                                      │ │
│  │  ├─ Devices: list, get, delete, count                                           │ │
│  │  ├─ Tasks: create, list, delete, wait_for_completion                            │ │
│  │  ├─ Parameters: get, set, set_and_wait, refresh_object                          │ │
│  │  ├─ Device ops: reboot, factory_reset, download                                 │ │
│  │  ├─ Presets/Provisions: CRUD                                                    │ │
│  │  └─ Faults: list, delete, retry                                                 │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    tr069.py (2,373 lines)                                       │ │
│  │  Tr069AcsServers: ACS endpoint CRUD                                             │ │
│  │  Tr069CpeDevices: Device registration                                           │ │
│  │  Tr069Jobs: Task queue                                                          │ │
│  │  Tr069Sessions: Communication sessions                                          │ │
│  │  Tr069Parameters: Parameter cache                                               │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐│
│  │  Supporting Services                                                             ││
│  │  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐    ││
│  │  │olt_tr069_admin │ │tr069_profile_  │ │tr069_parameter_│ │ ont_tr069.py   │    ││
│  │  │resolve ACS     │ │matching.py     │ │adapter.py      │ │param aggregator│    ││
│  │  │apply defaults  │ │match profiles  │ │type inference  │ │fetch from ACS  │    ││
│  │  └────────────────┘ └────────────────┘ └────────────────┘ └────────────────┘    ││
│  └─────────────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────────────┘
```

The TR-069 Inform handler owns each CPE's exact GenieACS device identifier.
Reconciliation reads that persisted identifier before any serial search, so a
Huawei serial rendered as `HWTC...` by the OLT still targets the same ACS
document when the ONT reports the equivalent `48575443...` hexadecimal serial.
Serial search is only a discovery or stale-identity fallback; it never
overrides a conflicting persisted identity or authorizes a guessed device.

## Adapter Registry

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           ADAPTER REGISTRY                                           │
│                                                                                      │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐                     │
│  │OltActionAdapter  │ │OltDetailAdapter  │ │OltProfileAdapter │                     │
│  │UI operational    │ │Dashboard summary │ │Live profile data │                     │
│  │actions           │ │                  │ │                  │                     │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘                     │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐                     │
│  │GenieAcsService  │ │AcsServiceIntent  │ │SubscriberOnt    │                     │
│  │Build config      │ │Adapter           │ │Adapter          │                     │
│  │payloads          │ │Intent→ACS tasks  │ │ONT→Customer link│                     │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Data Layer (ORM Models)

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           DATA LAYER (ORM Models)                                    │
│                                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐               │
│  │  OLTDevice   │ │  PonPort     │ │  OntUnit     │ │OntAssignment │               │
│  │  id, name    │ │  fsp, olt_id │ │  serial, fsp │ │  ont→pon     │               │
│  │  ssh creds   │ │  capacity    │ │  olt_ont_id  │ │  customer    │               │
│  │  acs_server  │ │              │ │  status      │ │              │               │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘               │
│                                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐               │
│  │Tr069AcsServer│ │Tr069CpeDevice│ │  Tr069Job    │ │Tr069Parameter│               │
│  │  base_url    │ │  device_id   │ │  command     │ │  path, value │               │
│  │  cwmp_url    │ │  serial      │ │  status      │ │  last_update │               │
│  │  credentials │ │  acs_server  │ │  result      │ │              │               │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘               │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Key Data Flows

### 1. Assigned ONT Authorization Flow

```
User clicks "Authorize & provision"
    → RequestAssignedOntAuthorization carries CommandContext + exact typed target
    → ont_provisioning_commands requires the exact active assignment and PON
    → durable authorization operation/dispatch
    → worker reconstructs ExecuteAssignedOntAuthorization
    → execution owner rechecks the exact assignment immediately before device I/O
    → ont_authorization.authorize_and_provision_ont(typed command)
        → Validate OLT authorization readiness
        → Resolve line/service/TR-069 profile and VLAN defaults
        → olt_protocol_adapters.authorize_ont()
            → olt_ssh_ont/lifecycle.authorize_ont() [via SSH]
        → Create or update OntUnit inventory and observed topology in DB
        → Apply internet service-port and management/ACS baseline from assignment
        → Verify ACS and apply saved customer service intent
```

### 1a. Assignment-free ONT Commissioning Flow

```
User with network:ont:commission clicks "Commission ONT"
    → network.ont_commissioning stores exact candidate + reason + 24h expiry
    → durable commission operation/dispatch
    → worker re-reads model-supported live OLT autofind and filters exact F/S/P
    → management-only dependency audit (line/service + DBA + TR-069)
    → ont_authorization.register_ont_for_commissioning(RegisterCommissioningOnt)
    → restricted management batch:
         management VLAN service-port + IPHOST + TR-069 profile only
         no internet-config, WAN, PPPoE, LAN, or Wi-Fi
    → bounded ACS observation / Inform marks management_ready
    → assignment converts ownership, or expiry stages safe inventory cleanup
```

MA5608T uses the supported global `display ont autofind all` observation and
filters it in application code to the exact requested F/S/P and canonical
serial. The unsupported per-port command is never attempted. Normal assigned
authorization retains the full live dependency audit, including customer
traffic-table and WAN-profile inventories.

The two authorization capabilities are separate named interfaces. There is no
public `provision` switch: only the assigned command executor may call
`authorize_and_provision_ont`, and only the commissioning owner may call
`register_ont_for_commissioning`. Reauthorization returns through the assigned
command owner and therefore cannot bypass assignment admission. OLT, ONT,
operation, and intent identities remain `UUID`; F/S/P and serial are validated
value objects; actor/correlation evidence remains `CommandContext` until the
explicit persistence or transport serialization boundary.

### 2. ACS Configuration Push Flow

```
ONT authorized + provisioning enabled
    → provisioning_coordinator (phase: acs_config_push)
    → genieacs_service_intent.push_service_intent_to_acs()
    → genieacs_service  [WiFi, WAN, LAN params]
    → genieacs.set_parameter_values_and_wait()
    → Poll until complete or timeout
```

### 3. OLT → ACS Relationship

```
OLTDevice
    └─ tr069_acs_server_id → Tr069AcsServer
                                └─ base_url (GenieACS API)
                                └─ cwmp_url (device connects here)
                                └─ periodic_inform_interval ← settings.tr069_periodic_inform_interval

OntUnit
    └─ tr069_acs_server_id → Tr069AcsServer (can override OLT's server)
    └─ Tr069CpeDevice (GenieACS device record)
        └─ genieacs_device_id
```

## File Summary

| Layer | Key Files | Lines | Purpose |
|-------|-----------|-------|---------|
| **Authorization** | `ont_authorization.py` | ~2,000 | Readiness-gated ONT authorization |
| **Assignment owner** | `ont_assignment_commands.py` | — | Exact subscription/PON assignment, release, and verified move projection |
| **Protocol** | `olt_protocol_adapters.py` | 1,991 | SSH/NETCONF/REST |
| **SSH Core** | `olt_ssh.py` | 1,567 | Low-level CLI |
| **SSH Pool** | `olt_ssh_pool.py` | ~300 | Connection reuse |
| **SSH ONT** | `olt_ssh_ont/*.py` | ~2,000 | ONT operations |
| **Provisioning** | `provisioning_coordinator.py` | 1,041 | Multi-step orchestration |
| **Executor** | `ont_provisioning/executor.py` | 875 | Delta execution + rollback |
| **TR-069** | `tr069.py` | 2,373 | ACS lifecycle |
| **GenieACS** | `genieacs.py` | ~1,200 | HTTP client |
| **OLT CRUD** | `olt.py` | 1,450 | Database operations |

## Service Descriptions

### OLT Services

| Service | File | Purpose |
|---------|------|---------|
| `OLTDevices` | `olt.py` | CRUD for OLT devices, credential management |
| `PonPorts` | `olt.py` | PON port infrastructure, capacity tracking |
| `OntUnits` | `olt.py` | ONT inventory with advanced filtering |
| `OntAssignmentCommands` | `ont_assignment_commands.py` | Canonical exact ONT-to-subscription assignment, release, and PON-move projection |
| `OntAssignments` | `ont_assignment_crud.py` | Compatibility reads and non-identity metadata updates; identity writes delegate or reject |
| `olt_ssh` | `olt_ssh.py` | Low-level SSH CLI execution |
| `olt_ssh_pool` | `olt_ssh_pool.py` | Connection pooling (TTL 5min, max 100 reuses) |
| `olt_operations` | `olt_operations.py` | High-level ops (backup, firmware, diagnostics) |
| `olt_protocol_adapters` | `olt_protocol_adapters.py` | Multi-protocol abstraction |

### ONT SSH Operations (`olt_ssh_ont/`)

| Module | Purpose |
|--------|---------|
| `lifecycle.py` | authorize, deauthorize, reboot, factory_reset |
| `status.py` | get_ont_status, find_ont_by_serial |
| `iphost.py` | configure/clear IP host settings |
| `omci_config.py` | WAN, WiFi, LAN config via OMCI |
| `tr069.py` | bind/unbind TR-069 server profile |
| `diagnostics.py` | service port diagnostics, remote ping |

### ACS/TR-069 Services

| Service | File | Purpose |
|---------|------|---------|
| `Tr069AcsServers` | `tr069.py` | ACS endpoint CRUD |
| `Tr069CpeDevices` | `tr069.py` | Device registration |
| `Tr069Jobs` | `tr069.py` | Task queue management |
| `GenieACSClient` | `genieacs.py` | HTTP client for GenieACS NBI |
| `GenieAcsService` | `genieacs_service.py` | Config payload building |
| `olt_tr069_admin` | `olt_tr069_admin.py` | ACS resolution for OLT flows |

### Provisioning Services

| Service | File | Purpose |
|---------|------|---------|
| `provisioning_coordinator` | `provisioning_coordinator.py` | Multi-phase orchestration |
| `executor` | `ont_provisioning/executor.py` | Delta execution with rollback |
| `context` | `ont_provisioning/context.py` | ONT→OLT context resolution |

## Adapter Pattern

All adapters register with `adapter_registry` and follow this pattern:

```python
class ExampleAdapter:
    name = "example"

example_adapter = ExampleAdapter()
adapter_registry.register(example_adapter)
```

**Registered Adapters:**
- `OltActionAdapter` - UI operational actions
- `OltDetailAdapter` - Dashboard summary data
- `OltProfileAdapter` - Live OLT profile data
- `OltObservedStateAdapter` - Real-time OLT state
- `SubscriberOntAdapter` - ONT-to-customer linking
- `GenieAcsServiceIntent` - Service intent to ACS tasks
- `GenieAcsService` - Config payload building
- `AcsStateAdapter` - ACS device state tracking

## Critical Architecture Notes

### Transaction Management
- Adapters create and close sessions. A registered public command owner enters
  `execute_owner_command` once on a transaction-free session and owns the
  atomic commit or rollback.
- Nested service helpers use `db.flush()` when they need generated IDs and do
  not commit independently.
- Legacy service methods in this architecture that still call `db.commit()`
  are migration debt; they are not the contract for new or modified owner
  boundaries.

### SSH Pool Efficiency
- Reuses connections for 5 minutes (configurable TTL)
- Max 100 reuses per connection before recycling
- Eliminates 2-3 second connection overhead per operation
- Thread-safe with automatic cleanup

### Compensation-Based Rollback
- Provisioning executor registers undo commands BEFORE execution
- On failure, compensation actions run in REVERSE order
- Single SSH session for all commands

### Multi-Protocol Support
- Protocol adapter auto-selects SSH, NETCONF, or REST based on OLT capabilities
- Falls back to next available protocol on operation failure
- Unified result type across all protocols

### ACS Polling vs Async
- `set_parameter_values()` returns immediately (async on ACS)
- `set_parameter_values_and_wait()` polls until completion (timeout configurable)
- `wait_for_task_completion()` polls task status with exponential backoff
- The periodic GenieACS inventory reconciliation loads one active-ONT serial
  index per ACS pass. Serial ambiguity still fails closed, but device count no
  longer multiplies complete ONT-table reads. Batch commits preserve bounded
  progress, and a Celery soft timeout aborts the pass and disposes its database
  session rather than continuing on an interrupted connection.

### Credential Encryption
- ACS passwords encrypted at rest using Fernet (from `credential_crypto`)
- Format: `enc:<encrypted>` for encrypted, `plain:<value>` for plaintext

## Configuration

### TR-069 Periodic Inform Interval

Single source of truth: `settings.tr069_periodic_inform_interval`

Set via environment variable:
```bash
TR069_PERIODIC_INFORM_INTERVAL=300  # seconds, default 5 minutes
```

## File Paths

### Core OLT/ONT/ACS Services
```
app/services/network/olt.py                    # CRUD infrastructure
app/services/network/olt_ssh.py                # Low-level CLI
app/services/network/olt_ssh_pool.py           # Connection pooling
app/services/network/olt_operations.py         # Operational tasks
app/services/network/ont_authorization.py           # Main auth flow
app/services/network/olt_protocol_adapters.py  # Multi-protocol abstraction
app/services/network/olt_ssh_ont/              # ONT operations via SSH
    lifecycle.py
    status.py
    iphost.py
    omci_config.py
    tr069.py
    diagnostics.py
app/services/network/ont_provisioning/         # Orchestration
    executor.py
    context.py
    profiles.py
    state.py
```

### TR-069/ACS Services
```
app/services/tr069.py                          # Complete lifecycle
app/services/genieacs.py                       # HTTP client
app/services/genieacs_client.py                     # Concrete GenieACS HTTP client
app/services/genieacs_service.py             # Application-facing GenieACS service
app/services/network/ont_tr069.py              # Parameter aggregation
app/services/network/olt_tr069_admin.py        # OLT TR-069 admin
app/services/network/tr069_profile_matching.py # Profile matching
app/services/network/tr069_parameter_adapter.py # Type inference
app/services/network/tr069_paths.py            # Path resolution
```

### Adapters & Coordination
```
app/services/olt_action_adapter.py
app/services/olt_detail_adapter.py
app/services/olt_profile_adapter.py
app/services/genieacs_service_intent.py
app/services/network/provisioning_coordinator.py
```
