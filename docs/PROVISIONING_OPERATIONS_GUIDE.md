# DotMac Sub — Provisioning & Operations Guide

> Training guide for NOC/operations team. Covers adding OLTs, managing ONTs, provisioning subscribers, VPN setup, and verification tests.

---

## Table of Contents

1. [Prerequisites & First-Time Setup](#1-prerequisites--first-time-setup)
2. [Adding & Configuring an OLT](#2-adding--configuring-an-olt)
3. [Managing ONTs](#3-managing-onts)
4. [Provisioning a New Subscriber](#4-provisioning-a-new-subscriber)
5. [Remote ONT Operations](#5-remote-ont-operations)
6. [TR-069 / GenieACS Setup](#6-tr-069--genieacs-setup)
7. [VPN (WireGuard) Setup & Verification](#7-vpn-wireguard-setup--verification)
8. [NAS Device Configuration](#8-nas-device-configuration)
9. [Verification Tests & Health Checks](#9-verification-tests--health-checks)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites & First-Time Setup

### Access the Admin Portal

Navigate to `/admin` and log in with your admin credentials.

### Provisioning Flow Overview

Provisioning is intentionally staged. Do not authorize ONTs on a new OLT until the foundation objects, protocol access, and config-pack readiness checks are complete.

| Phase | Purpose | Operator Entry Points | Main Code Modules |
|-------|---------|-----------------------|-------------------|
| 1. Foundation setup | Create shared network primitives before touching live OLTs. | VLANs, speed profiles, ONU types, zones, IP pools, TR-069 ACS servers, optional WireGuard. | `app/web/admin/network_tr069.py`, `app/web/admin/wireguard.py`, `app/services/web_network_tr069.py`, `app/services/web_network_vlans.py`, `app/services/network/speed_profiles.py`, `app/services/network/onu_types.py`, `app/services/network/zones.py`, `app/services/wireguard.py` |
| 2. OLT onboarding | Create the OLT record and attach OLT-local resources. | OLT create/edit form, ACS assignment, VLAN/IP-pool scoping, imported profile/service-port state, backup settings. | `app/web/admin/network_olts_inventory.py`, `app/web/admin/network_olts_profiles.py`, `app/services/web_network_olts.py`, `app/services/network/olt_state_import.py`, `app/services/network/olt_profile_resolution.py` |
| 3. Connectivity validation | Prove the app can reach the OLT before any write operation. | Test SSH, NETCONF, running-config reads, and Zabbix host linkage for SNMP collection; REST only where the adapter supports it. | `app/services/network/olt_protocol_adapters.py`, `app/services/network/olt_ssh.py`, `app/services/network/olt_ssh_session.py`, `app/services/network/olt_ssh_pool.py`, `app/services/network/olt_netconf.py`, `app/services/network/olt_rest_client.py`, `app/services/network/olt_vendor_adapters.py` |
| 4. Imported state readiness | Verify OLT-local profiles, GEM mappings, ONT registrations, and service-port rows are imported. | Import Live State, Import Dump, Imported Profiles tab, mapping coverage report. | `app/services/network/olt_state_import.py`, `app/services/network/imported_service_ports.py`, `app/services/network/effective_ont_config.py`, `scripts/report_missing_olt_mappings.py` |
| 5. Inventory and topology sync | Populate operational state from the OLT. | ONT sync, autofind scan, PON repair, Zabbix-backed hardware discovery, monitoring links. | `app/services/network/olt_inventory.py`, `app/services/network/olt_hardware_discovery.py`, `app/services/network/olt_web_topology.py`, `app/web/admin/network_pon_interfaces.py`, `app/web/admin/network_olts_profiles.py`, `app/tasks/olt_hardware_discovery.py` |
| 6. Customer-service assignment | Bind the exact subscription and modeled PON through the assignment owner. Imported identifiers and MAC matches remain review evidence. | ONT assignment form or field work-order completion with explicit subscription and PON. | `app/services/network/ont_assignment_commands.py`, `app/services/web_network_ont_assignments.py`, `app/services/field_equipment.py` |
| 7. ONT authorization/provisioning | **Authorize & provision** requires an exact active assignment. Pre-assignment work uses the separate 24-hour, management-only commissioning intent. | Autofind **Commission ONT**, assigned ONT **Authorize & provision**, and ONT provisioning tab. | `app/services/network/ont_commissioning.py`, `app/services/network/ont_authorization.py`, `app/services/network/ont_provision_steps.py` |
| 8. Backup, config audit, and drift checks | Keep read-only evidence that live OLT state matches intended state. | Scheduled SSH running-config backups, backup audits, live config-pack audits, compensation retry watchdog. | `app/tasks/olt_config_backup.py`, `app/services/network/olt_config_audit.py`, `app/services/network/olt_config_pack_live_audit.py`, `app/tasks/provisioning.py` |

## Service-change execution reconciliation

Use `/admin/provisioning/service-change-reconciliation` when a paid relocation,
remote reprovision, or verified field change appears interrupted. The page is
restricted by `provisioning:service_change_reconcile`. Review the reported
structural evidence, supply a unique idempotency key and reason, and repair only
the displayed reviewed head. A stale head must be refreshed and reviewed again.
Non-repairable drift requires investigation; never infer settlement from invoice
memo text or provisioning from work-order status alone.

### Verify System Configuration

Before any network operations, confirm these are configured:

| Setting | Location | What to Check |
|---------|----------|---------------|
| Company Info | `/admin/system/company-info` | Company name, address, currency |
| TR-069 Settings | `/admin/system/config/tr069` | Default ACS server ID |
| Network Settings | `/admin/system/config/network` | Default region, SNMP community |
| RADIUS Settings | `/admin/system/config/radius` | RADIUS server address, ports |
| Credential Encryption | Server env var | `CREDENTIAL_ENCRYPTION_KEY` is set |

### Verify Speed Profiles Exist

Go to `/admin/network/speed-profiles`. You need at least download and upload profiles before provisioning.

### Verify VLANs Exist

Go to `/admin/network/vlans`. You need VLANs for:
- **Internet** — subscriber data (e.g., VLAN 203)
- **Management** — ONT management (e.g., VLAN 450)
- **TR-069** — ACS communication (e.g., VLAN 455)

### Verify TR-069 ACS Server Exists

Go to `/admin/network/tr069`. Create or confirm an active ACS server with:
- CWMP URL reachable by ONTs over the management/TR-069 path
- GenieACS NBI URL reachable by the app
- Connection request credentials, if the ACS will push changes after inform

### Verify Optional WireGuard Access

If the app reaches OLTs over VPN, go to `/admin/vpn` and confirm the WireGuard server and peer are active before testing OLT SSH or SNMP.

---

## 2. Adding & Configuring an OLT

### Step 1: Create the OLT

1. Go to `/admin/network/olts`
2. Click **"Add OLT"**
3. Fill in:
   - **Name** — descriptive name (e.g., "Garki MA5608T")
   - **Vendor** — Huawei
   - **Model** — MA5608T / MA5800-X2 / etc.
   - **Management IP** — OLT management address
   - **SSH Username/Password** — for CLI access
   - **SSH Port** — usually 22
   - **SNMP Community** — read-only community string
   - **SNMP Version** — v2c (most common)
4. Save

### Step 2: Test Connectivity

On the OLT detail page (`/admin/network/olts/{olt_id}`):

1. Click **"Test SSH"** — should show "Connection successful"
2. Confirm the linked Zabbix host has recent SNMP items
3. If NETCONF is available, click **"Test NETCONF"**

> **Troubleshooting:** If SSH fails, check that the OLT management IP is reachable from the app server. If using WireGuard, verify the tunnel is up first (see Section 7).

### Step 3: Assign VLANs to OLT

On the OLT detail page, go to the **VLANs** tab:
1. Click **"Assign VLAN"**
2. Select the internet, management, and TR-069 VLANs
3. Save

### Step 4: Assign IP Pool and TR-069 ACS Server

On the OLT detail page:
1. Assign the management IP pool used for ONT ACS reachability
2. Find the **TR-069** section
3. Select your GenieACS server from the dropdown
4. Save

### Step 5: Import OLT State

On the OLT detail page, import the actual OLT state before authorizing or provisioning ONTs:

1. Click **"Import Live State"** to read profiles, ONT registrations, GEM mappings, and service-ports over SSH.
2. If a recent audit dump is available on the app server, click **"Import Dump"** to import from `/root/olt_audit_20260506`.
3. Open the **Imported Profiles** tab and confirm:
   - Line profiles and service profiles are present.
   - Equipment profile mappings exist for the ONT models on that OLT.
   - GEM mappings exist for the internet and management VLANs.
   - Service-port count is non-zero and `last_imported_at` is current.

Provisioning treats imported DB rows as source of truth. If imported service-port state is missing, provisioning, cleanup, clone, and VLAN enforcement paths fail fast instead of falling back to guessed GEM/VLAN defaults.

### Step 6: Verify Profile and ACS Readiness

On the OLT detail page:

1. Open the **TR-069 Profiles** tab.
2. Create or confirm the DotMac ACS TR-069 profile on the OLT.
3. Confirm the imported equipment mappings point to imported line/service profile IDs.
4. Run `python scripts/report_missing_olt_mappings.py --all --fail-on-missing` after fleet imports; it must report `0 missing mapping(s)`.

### Step 7: Sync Inventory and Topology

1. Click **"Sync ONTs"** — triggers OLT ONT discovery
2. Click **"Discover Hardware"** — reads shelves/cards/ports/SFPs from Zabbix-collected SNMP Entity MIB data
3. Run **"Repair PON Ports"** if ONTs exist but canonical PON ports are missing
4. Confirm the topology and PON interface views show the expected OLT structure
5. Re-run **Import Live State** after large manual OLT changes so imported service-port and GEM rows stay current.

PON assignment fails closed when multiple active rows on one OLT claim the same
frame/slot/port identity. A reviewed deactivation preserves the retired row for
history while removing it from current identity competition; the reporting-only
**Repair PON Ports** action does not deactivate or merge rows.

### Step 8: Run Autofind

For unregistered ONTs:
1. Click **"Autofind Scan"**
2. Review discovered serial numbers
3. Confirm config-pack readiness is green
4. For a customer-ready device, create the exact subscription/PON assignment,
   then click **"Authorize & provision"**.
5. If the device needs ACS management before customer assignment, click
   **"Commission ONT"**, enter the operational reason and optional work-order or
   ticket reference, and confirm the exact OLT/F/S/P/serial.
6. Commissioning configures only ONT registration, management VLAN/IPHOST, and
   TR-069. It does **not** configure internet, PPPoE, WAN, LAN, or Wi-Fi.
7. Commissioning expires after 24 hours. Assign a management-ready ONT before
   expiry; otherwise the reconciler returns it to inventory. A cleanup failure
   stays visible and blocks assignment until reviewed.
8. Commissioning validates only dependencies it can write: authorization
   line/service mappings, referenced DBA profiles, management configuration,
   and the OLT TR-069 profile. Internet traffic tables and WAN profiles remain
   required by the normal assigned **Authorize & provision** workflow.

Do not retry a spinning or failed unassigned authorization through the raw
authorization endpoint. Refresh live autofind and use **Commission ONT**. Normal
authorization intentionally fails closed when the exact active assignment,
modeled PON, OLT, or F/S/P is missing or disagrees.

There is no operator or API switch for “authorize only.” The assigned workflow
always applies the full OLT service baseline, while **Commission ONT** owns the
separate management-only lifecycle. **Re-authorize** is also assignment-gated;
if it reports a missing assignment, correct the assignment or use commissioning
instead of attempting a direct OLT registration.

---

## 3. Managing ONTs

### Viewing ONTs

Go to `/admin/network/onts` to see all ONTs with:
- Online/offline status (color-coded)
- Signal levels (Rx power in dBm)
- Assigned subscriber
- Zone and OLT

Use the search bar and filters to find specific ONTs by serial number, zone, or status.

### Creating an ONT Manually

1. Go to `/admin/network/onts/new`
2. Fill in:
   - **Serial Number** — exact match from OLT (e.g., "48575443A1B2C3D4")
   - **OLT** — select the parent OLT
   - **Board/Port** — FSP location (e.g., board "0/2", port "1")
   - **External ID** — ONT-ID on the OLT (e.g., "5")
   - **ONU Type** — select the hardware model
   - **Zone** — geographic zone
3. Save

### ONT Detail Page

Click any ONT to see its detail page (`/admin/network/onts/{ont_id}`) with tabs:

| Tab | What It Shows |
|-----|---------------|
| **Summary** | Serial, model, signal, online status, subscriber link |
| **TR-069** | ACS status, last inform time, connection request URL |
| **Service Ports** | VLAN/GEM mappings on the OLT |
| **Config** | Running configuration from the device |
| **Charts** | Signal and bandwidth trends |
| **Provisioning** | Profile assignment and provisioning preview |
| **IPHOST** | Management IP configuration |

### Assigning an ONT to a Subscriber

1. On the ONT detail page, click **"Assign"**
2. Search for and select the subscriber
3. Select the subscription (service plan)
4. Select the PON port
5. Save

---

## 4. Provisioning a New Subscriber

This is the end-to-end workflow for activating a new subscriber.

### Step 1: Ensure Prerequisites

- [ ] OLT added and tested (SSH + SNMP working)
- [ ] ONT discovered or manually added
- [ ] ONT assigned to subscriber
- [ ] Speed profiles configured
- [ ] VLANs created
- [ ] Management IP pool assigned to OLT
- [ ] TR-069 ACS server assigned to OLT
- [ ] TR-069 OLT profile exists and its profile ID is in the OLT config pack
- [ ] OLT config-pack validation has no blocking errors
- [ ] Provisioning profile created

### Step 2: Create or Select a Provisioning Profile

Go to `/admin/network/provisioning-profiles`:

1. Click **"Create Profile"**
2. Configure:
   - **Name** — e.g., "Residential 100Mbps"
   - **Config Method** — OMCI or TR-069
   - **ONU Mode** — Routing or Bridging
   - **Management VLAN** — tag number (e.g., 450)
   - **Management IP Mode** — DHCP (recommended)
   - **Internet Config IP Index** — 0 (default, activates TCP stack)
   - **WAN Config Profile ID** — 0 (default, sets route+NAT)
   - **PPPoE OMCI VLAN** — set to internet VLAN tag if using PPPoE via OMCI (e.g., 203); leave empty to skip
   - **CR Username/Password** — connection request credentials (defaults to "acs"/"acs")
   - **WiFi** — enabled, SSID template, security mode
   - **Speed Profiles** — download and upload
3. Add **WAN Services**:
   - Service type: Internet
   - VLAN: your internet S-VLAN (e.g., 203)
   - Connection type: PPPoE
   - GEM port: 1
   - PPPoE username template: `{subscriber_code}`
   - PPPoE password mode: from_credential or generate
4. Save

### Step 3: Preview Provisioning Commands (Dry Run)

1. Go to the ONT detail page
2. Click the **Provisioning** tab
3. Select the provisioning profile
4. Enter the TR-069 OLT profile ID (e.g., 2)
5. Click **"Preview"** (dry run)
6. Review the generated OLT CLI commands:
   - Service-port creation commands
   - IPHOST management IP command
   - Internet-config command
   - WAN-config command
   - TR-069 binding command
7. Verify VLANs, GEM ports, and tag-transforms look correct

### Step 4: Execute Provisioning

1. Click **"Provision"** (or use async mode for background execution)
2. Monitor the 13-step progress:

| Step | What Happens | Expected |
|------|-------------|----------|
| 1. Resolve Context | Loads ONT, OLT, subscriber | "ONT XXXX on OLT-Name 0/2/1 ONT-ID 5" |
| 2. Generate Commands | Creates CLI command set | "Generated N commands in M steps" |
| 3. Dry Run Check | Skipped in execute mode | — |
| 4. Create Service Ports | SSH to OLT, creates GEM→VLAN mappings | "Created 1, failed 0" |
| 5. Configure Mgmt IP | Sets DHCP on management VLAN | "Management IP configured (dhcp on VLAN 450)" |
| 6. Internet Config | Activates TCP stack on ONT | "Internet config activated (ip-index 0)" |
| 7. WAN Config | Sets route+NAT mode | "WAN route+NAT mode set" |
| 8. TR-069 Binding | Binds ACS profile + resets ONT | "TR-069 profile 2 bound (reset triggered)" |
| 9. TR-069 Bootstrap | Waits for ONT to register in GenieACS | "Device registered in ACS" (up to 120s) |
| 10. CR Credentials | Sets connection request auth | "Connection request credentials set" |
| 11. PPPoE OMCI | Configures PPPoE via OLT (if enabled) | "Configured 1, failed 0" |
| 12. PPPoE TR-069 | Pushes PPPoE username/password | "PPPoE credentials pushed" |
| 13. Finalize | Marks ONT as provisioned | "ONT marked as provisioned" |

### Step 5: Verify

After provisioning completes:

1. **Check ONT status** — should show "online" within 60 seconds
2. **Check TR-069 tab** — should show recent inform timestamp
3. **Check service ports** — verify GEM/VLAN mappings on the OLT
4. **Test subscriber connection** — customer should get PPPoE session and internet access

---

## 5. Remote ONT Operations

Available from the ONT detail page action buttons:

| Action | Button | What It Does |
|--------|--------|-------------|
| **Reboot** | "Reboot" | Restarts ONT via TR-069 |
| **Factory Reset** | "Factory Reset" | Wipes all config (use with caution) |
| **Refresh Status** | "Refresh" | Pulls latest data from ACS |
| **Set WiFi SSID** | WiFi section | Changes wireless network name |
| **Set WiFi Password** | WiFi section | Changes wireless password |
| **Toggle LAN Port** | LAN section | Enables/disables individual LAN ports |
| **Set PPPoE** | Network section | Pushes PPPoE credentials |
| **Run Ping** | Diagnostics section | Pings from the ONT itself |
| **Run Traceroute** | Diagnostics section | Traces route from ONT |
| **View Config** | "Config" tab | Fetches full running configuration |
| **Reboot via OMCI** | Advanced | Resets ONT through OLT (works even if TR-069 is down) |
| **Configure IPHOST** | IPHOST tab | Sets management IP via OLT SSH |
| **Bind TR-069** | TR-069 tab | Binds/rebinds ACS profile via OLT SSH |

---

## 6. TR-069 / GenieACS Setup

### Step 1: Add ACS Server

1. Go to `/admin/network/tr069`
2. Click **"Add ACS Server"**
3. Fill in:
   - **Name** — e.g., "GenieACS Production"
   - **CWMP URL** — the ACS URL that ONTs connect to (e.g., `http://10.10.41.1:7547`)
   - **CWMP Username/Password** — credentials ONTs use to authenticate
   - **Connection Request Username/Password** — credentials ACS uses to connect back to ONTs
   - **GenieACS NBI URL** — the management API URL (e.g., `http://10.10.41.1:7557`)
4. Save

### Step 2: Link ACS to OLT

1. Go to the OLT detail page
2. Set the TR-069 ACS server dropdown to your GenieACS server
3. Save

### Step 3: Create TR-069 Profile on OLT

1. On the OLT detail page, go to **TR-069 Profiles** tab
2. Click **"Create Profile"**
3. Enter:
   - **Profile Name** — e.g., "DotMac-GenieACS"
   - **ACS URL** — same as CWMP URL above
   - **Username** — CWMP username
   - **Password** — CWMP password
4. Note the profile ID assigned by the OLT

### Step 4: Bulk Rebind ONTs (Migration)

To move ONTs from SmartOLT's ACS profile to GenieACS:

1. On the OLT detail page, go to **TR-069 Profiles** tab
2. Select the ONTs to rebind (or "Select All")
3. Choose the new profile ID
4. Click **"Rebind Selected"**
5. Each ONT will be reset and should register with GenieACS within 120 seconds

### Step 5: Verify Registration

1. Go to the ONT detail page
2. Check the **TR-069** tab
3. Should show:
   - Last inform time (recent)
   - Connection request URL
   - Device parameters populated

---

## 7. VPN (WireGuard) Setup & Verification

The WireGuard VPN connects the app server to the OLT management network.

### Step 1: Create WireGuard Server

1. Go to `/admin/network/vpn`
2. Click **"Add Server"**
3. Configure:
   - **Name** — e.g., "OLT Management VPN"
   - **Listen Port** — e.g., 51820
   - **VPN Address** — server-side IP (e.g., `10.10.41.1/24`)
   - **Interface Name** — e.g., `wg0`
   - **Public Host** — public IP or hostname of the VPN server
   - **DNS** — DNS server for VPN clients
   - **Auto-deploy** — enable for automatic interface management
4. Save

### Step 2: Add Peers

1. Click **"Add Peer"** on the server page
2. Configure:
   - **Name** — e.g., "GenieACS Server" or "OLT-Garki"
   - **Peer Address** — peer's VPN IP (e.g., `10.10.41.2/32`)
   - **Known Subnets** — networks behind the peer (e.g., `192.168.1.0/24` for OLT mgmt)
   - **Persistent Keepalive** — 25 seconds (recommended for NAT traversal)
3. Save
4. Share the peer config (public key + endpoint) with the remote site

### Step 3: Deploy Interface

1. Click **"Deploy"** on the server page
2. The WireGuard interface will be brought up

### Step 4: Test VPN Connectivity

1. On the server page, click **"Health Scan"**
2. Each peer should show as "connected" with recent handshake
3. Alternatively, use the OLT **"Test SSH"** button — if SSH works through the VPN, the tunnel is working

### MikroTik Router Integration

If the remote site uses MikroTik:
1. On the server page, enter MikroTik router details (API host, port, credentials)
2. Click **"Test Router"** to verify API connectivity
3. WireGuard configs can be deployed directly to MikroTik via API

---

## 8. NAS Device Configuration

### Adding a NAS (MikroTik Router)

1. Go to `/admin/network/nas`
2. Click **"Add Device"**
3. Fill in:
   - **Name** — e.g., "Garki-NAS-01"
   - **Vendor** — MikroTik
   - **Model** — CCR1036 / RB4011 / etc.
   - **IP Address** — management IP
   - **SSH Credentials** — username, password, port
   - **MikroTik API** — enable, port (default 8728)
   - **RADIUS Secret** — shared secret for RADIUS auth
   - **NAS Identifier** — identifier sent in RADIUS requests
4. Save

### Testing NAS Connectivity

On the NAS detail page:
1. Click **"Test API"** — verifies MikroTik API connection
2. Click **"Ping"** — updates last-seen timestamp

### Setting Up Backups

1. On the NAS detail page, go to **Backups** tab
2. Click **"Trigger Backup"** to run manually
3. Or configure scheduled backups in the device settings

---

## 9. Verification Tests & Health Checks

Run these checks to confirm the system is ready for production.

### Test 1: OLT Connectivity

For each OLT:
1. Go to OLT detail page
2. Click **"Test SSH"** — expect "Connection successful"
3. Confirm the OLT is linked to a Zabbix host and recent SNMP items are visible
4. Click **"Sync ONTs"** — expect ONT count to match reality

**Pass criteria:** All 8 OLTs show SSH successful and current Zabbix SNMP ingestion.

### Test 2: ONT Signal Monitoring

1. Go to `/admin/network/onts`
2. Filter by online status
3. Verify signal levels are populated (Rx dBm values)
4. Check that offline ONTs show correct offline reason

**Pass criteria:** Online ONTs have signal readings updated within last 10 minutes.

### Test 3: TR-069 ACS Connectivity

1. Go to `/admin/network/tr069`
2. Click **"Sync"** on your ACS server
3. Verify device count matches registered ONTs

Then for a specific ONT:
1. Go to ONT detail → **TR-069** tab
2. Verify last inform time is recent
3. Click **"Refresh Status"** — should complete without error
4. Click **"Config" tab** — should show device parameters

**Pass criteria:** ACS sync returns devices; ONT refresh works.

### Test 4: Provisioning Dry Run

1. Pick a test ONT (not in production)
2. Go to ONT detail → **Provisioning** tab
3. Select a profile and click **"Preview"**
4. Verify commands look correct:
   - Service-port has correct VLAN and GEM
   - IPHOST has correct management VLAN
   - TR-069 binding has correct profile ID

**Pass criteria:** Commands match expected OLT CLI syntax.

### Test 5: Full Provisioning (Test ONT)

1. Pick a fresh/unused ONT
2. Run full provisioning (not dry run)
3. Verify all 13 steps succeed
4. Check ONT comes online within 2 minutes
5. Verify subscriber gets PPPoE session
6. Test internet connectivity from subscriber

**Pass criteria:** All 13 steps green; subscriber has internet.

### Test 6: VPN Health

1. Go to `/admin/network/vpn`
2. Run **"Health Scan"**
3. All peers should show recent handshake

**Pass criteria:** All VPN peers connected.

### Test 7: NAS Connectivity

For each NAS device:
1. Go to NAS detail page
2. Click **"Test API"** — expect success
3. Run a manual backup — expect backup file created

**Pass criteria:** All NAS devices respond to API and backup.

### Test 8: Remote ONT Operations

Pick an online ONT and test:
1. **Reboot** — ONT should go offline briefly, then come back online
2. **Set WiFi SSID** — change to a test SSID, verify on ONT's WiFi
3. **Run Ping** — ping 8.8.8.8 from the ONT, expect success
4. **View Config** — should return full device parameters

**Pass criteria:** All 4 operations complete successfully.

### Test 9: RADIUS Authentication

1. Go to `/admin/network/radius`
2. Check **"Sessions"** tab for active PPPoE sessions
3. Verify subscriber usernames appear in session list
4. Check **"Errors"** tab for any authentication failures

**Pass criteria:** Active sessions visible; no unexpected auth failures.

---

## 10. Troubleshooting

### ONT Not Coming Online After Provisioning

1. Check OLT SSH: is the ONT registered? (`display ont info`)
2. Check service-ports: do they exist? (`display service-port all`)
3. Check management VLAN: is internet-config active?
4. Check TR-069: did the ONT register in GenieACS?
5. Try OMCI reboot from the ONT detail page (works without TR-069)

Huawei serial lookup normalizes compatible display serials such as
`HWTC1234ABCD` to the full OLT hex form before issuing
`display ont info by-sn`. Only an explicit ONT-absent response proves that a
registration does not exist. A parameter error, rejected command, empty
response, or unrecognized detail block is an unavailable observation and must
fail closed. Do not force reauthorization or delete the suspected registration
until a successful lookup or registered-ONT inventory read identifies its
current F/S/P and ONT-ID; investigate physical/offline state separately.

On MA5608T, `display ont autofind <F/S/P>` is not a supported command. Sub reads
`display ont autofind all` and filters the parsed result to the exact requested
OLT/F/S/P/serial before commissioning. If the cached row and live location
disagree, refresh Autofind and review the new port; never authorize from the
stale row.

There is no operator, API, task, or service switch for registration-only
authorization. Assigned authorization requires an exact typed ONT/OLT/F/S/P/
serial command and rechecks the active assignment immediately before the OLT
write. An assignment-free candidate must use **Commission ONT**; reauthorization
uses the same assigned-command gate.

### TR-069 Bootstrap Timeout (Step 9 Fails)

1. Verify the ACS URL is reachable from the ONT's management VLAN
2. Check that the TR-069 profile on the OLT has the correct URL
3. Verify the management VLAN is trunked to the OLT's uplink
4. Try manually resetting the ONT from the OLT detail page
5. Check GenieACS logs for connection attempts

### SSH Connection Failed

1. Verify the OLT management IP is reachable (ping from app server)
2. If using VPN, check WireGuard tunnel status
3. Verify SSH credentials are correct
4. Check if OLT has SSH enabled and allows connections from app server IP
5. Check for SSH key exchange algorithm compatibility

### WiFi/PPPoE Push Fails

1. Check the TR-069 tab — is the device registered in ACS?
2. Check GenieACS faults — are there pending tasks that failed?
3. The device may use a different data model (TR-098 vs TR-181). Check `tr069_data_model` field on the ONT.
4. Try **"Refresh Status"** first, then retry the operation

### Service-Port Creation Fails

1. Check that the VLAN exists on the OLT (SSH: `display vlan all`)
2. Check that the GEM port index is valid for the line profile
3. Check OLT capacity — service-port limit may be reached
4. Review the error message in the step result for OLT CLI output

---

## Quick Reference: Key URLs

| Page | URL |
|------|-----|
| Dashboard | `/admin/dashboard` |
| OLT List | `/admin/network/olts` |
| ONT List | `/admin/network/onts` |
| Provisioning Profiles | `/admin/network/provisioning-profiles` |
| TR-069 ACS Servers | `/admin/network/tr069` |
| Speed Profiles | `/admin/network/speed-profiles` |
| VLANs | `/admin/network/vlans` |
| Zones | `/admin/network/zones` |
| ONU Types | `/admin/network/onu-types` |
| VPN Management | `/admin/network/vpn` |
| NAS Devices | `/admin/network/nas` |
| RADIUS | `/admin/network/radius` |
| System Settings | `/admin/system/settings-hub` |
| Company Info | `/admin/system/company-info` |
| Users & Roles | `/admin/system/users` |
| Scheduler | `/admin/system/scheduler` |
| Audit Log | `/admin/system/audit` |
