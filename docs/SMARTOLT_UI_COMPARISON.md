# SmartOLT UI/UX Comparison Report

> **Status: comparative research, not a specification.** A SmartOLT difference
> is not automatically a Dotmac gap. Current information, action, table, and
> page-depth decisions follow `UI_INFORMATION_AND_ACTION_STANDARD.md` and the
> relevant domain source of truth.

**Date**: 2026-04-22
**Purpose**: Comprehensive analysis of SmartOLT's UI patterns vs. DotMac Sub implementation

---

## Executive Summary

SmartOLT is a mature FTTH management platform with well-established UX patterns optimized for NOC technicians. This report details every SmartOLT page/feature and compares it to our current implementation, identifying compatibility for form sharing and gaps requiring attention.

**Key Finding**: Our forms are **conceptually compatible** but use a different layout philosophy. SmartOLT uses compact modal dialogs for configuration changes, while we use collapsible sections on a unified config page. Both approaches are valid; ours provides more context but requires more scrolling.

---

## Page-by-Page Comparison

### 1. Dashboard

#### SmartOLT Dashboard
- **Hero Stats Row**: 4 large cards showing:
  - `0` Waiting authorization (blue with wand icon)
  - `310` Online (blue)
  - `431` Total offline (gray with X icon, breakdown: PwrFail: 37, LoS: 23, N/A: 371)
  - `34` Low signals (orange with warning icon, Warning: 27, Critical: 7)
- **Network Status Chart**: Daily network status area chart showing Online ONUs, Power fail, Signal loss, N/A over time
- **OLTs Sidebar**: List of OLTs with warning triangles, showing name, uptime, temperature (colored by severity)
- **Info Sidebar**: Auto config backup notifications with timestamps
- **ONU Authorizations per Day**: Bar chart of daily authorization activity
- **PON Outage Table**: Board/Port, ONUs, LOS, Power, Possible cause, Since columns

#### DotMac Implementation (`/admin/dashboard`)
- ✅ Stats cards (similar layout, 6 cards)
- ✅ OLT health indicators
- ❌ **Gap**: No daily network status chart
- ❌ **Gap**: No ONU authorizations per day chart
- ❌ **Gap**: No PON outage table
- ❌ **Gap**: No auto config backup notifications

**Compatibility**: ~40% - Dashboard structure differs significantly

---

### 2. OLT List

#### SmartOLT OLT List
- Table columns: Name, Location, Hardware version, Software version, System contact, IP, Temperature, Status
- Color-coded temperature (green/yellow/red)
- Sortable columns
- Link to OLT details

#### DotMac Implementation (`/admin/network/olts`)
- ✅ Table with name, location, IP, status
- ✅ Temperature display
- ❌ **Gap**: No hardware/software version columns in list view
- ✅ Links to OLT details

**Compatibility**: ~80% - Minor column differences

---

### 3. OLT Details

#### SmartOLT OLT Details
- **Tab Navigation**: OLT details | OLT cards | PON ports | Uplink | VLANs | ONU Mgmt IPs | Remote ACLs | VoIP Profiles | Advanced
- **Sub-tabs**: Edit OLT settings | See history | ↗CLI | Config backups
- **Settings Table**: Key-value pairs showing:
  - Name, OLT IP, Reachable via VPN, Telnet TCP port
  - OLT telnet username/password (masked)
  - SNMP communities (masked), SNMP UDP port
  - IPTV module, OLT hardware version, OLT software version
  - Supported PON types, TR069 Profile (dropdown with "Set profiles" button)
- **Sidebar**: OLT image, Uptime, Temperature badge

#### DotMac Implementation (`/admin/network/olts/{id}`)
- ✅ Similar information displayed
- ✅ Connection info, credentials
- ❌ **Gap**: No tabbed navigation (single page layout)
- ❌ **Gap**: No OLT cards sub-page
- ❌ **Gap**: No Config backups sub-page
- ❌ **Gap**: No CLI shortcut

**Compatibility**: ~60% - Same data, different navigation

---

### 4. OLT Cards

#### SmartOLT OLT Cards
- Table: Slot, Type, Real type, Ports, SW, Status, Role, Info updated
- Actions: "Refresh OLT cards info", "Detect new cards", "Reboot-card" per row
- Shows card hardware types (H803GPFD, H801MCUD1, H801MPWC)

#### DotMac Implementation
- ❌ **Not implemented** - No OLT card management page

**Compatibility**: 0% - Feature gap

---

### 5. PON Ports

#### SmartOLT PON Ports
- **Action Buttons**: Refresh PON ports info | Enable all PON ports | Disable AutoFind | Refresh ONUS
- Table: Port, Type, Status, OPMs, Manage status, ODB name, Range, To action
- Inline dropdown for ODB assignment
- Color-coded status (Enabled green, Down red)
- Links to view ONUs per port

#### DotMac Implementation (`/admin/network/olts/{id}/pon-ports`)
- ✅ PON port listing
- ✅ Status indicators
- ✅ Per-port Enable/Disable actions for supported Huawei OLTs, with confirmation,
  permissions, write-mode enforcement, audit logging, and device-result feedback
- ❌ **Gap**: No bulk Enable/Disable actions
- ❌ **Gap**: No AutoFind toggle
- ❌ **Gap**: No ODB (splitter) assignment

**Compatibility**: ~50% - Basic functionality present

---

### 6. OLT Uplinks

#### SmartOLT Uplinks
- Table: Uplink port, Description, Type, Admin state, Status, Negotiation, MTU, WaveL, Temp, PVID, Mode: tagged/untag, VLANs
- Color-coded status (Enabled green, Down red)
- Configure button per port

#### DotMac Implementation
- ❌ **Not implemented** - No uplink management page

**Compatibility**: 0% - Feature gap

---

### 7. VLANs (Per OLT)

#### SmartOLT VLANs Page
- **Add VLAN** button
- Table: VLAN ID, VLAN name, Purpose, Multicast, ONU IGMP snooping, ONU DHCP snooping, ONUs, Action
- Purpose labels: Internet, Management, VoIP, IPTV
- ONU count per VLAN
- Delete button per row

#### DotMac Implementation (`/admin/network/vlans`)
- ✅ VLAN listing
- ✅ Purpose field (enum: internet, management, voip, iptv)
- ❌ **Gap**: VLANs not scoped per-OLT in same way
- ❌ **Gap**: No ONU count column
- ❌ **Gap**: No IGMP/DHCP snooping flags

**Compatibility**: ~60% - Core concept present, missing per-OLT scoping

---

### 8. Unconfigured Devices (Autofind)

#### SmartOLT Unconfigured Devices
- **OLT Filter**: Dropdown to filter by OLT
- **Refresh** button
- **Auto actions**: Collapsible section with "Configure actions", "Task history", "Refresh", "Stop auto actions"
- **Authorization Presets**: Button to manage quick auth presets
- **Add ONU for later authorization**: Green button for pre-staging

#### DotMac Implementation (`/admin/network/onts?view=unconfigured`)
- ✅ OLT filter dropdown
- ✅ Scan Now button
- ✅ Authorize button per entry with Force checkbox
- ✅ Active/History view toggle
- ❌ **Gap**: No auto-authorization actions
- ❌ **Gap**: No authorization presets
- ❌ **Gap**: No "Add ONU for later authorization"

**Compatibility**: ~70% - Core workflow matches

---

### 9. Configured ONTs List

#### SmartOLT Configured ONTs
- **Multi-filter Bar**: OLT, Profile, PON type, PON port, Part, Zone, VLAN, Zone (hierarchical)
- Table: View (button), Status (online indicator), SN/MAC, Profile, ONU name, ONU External ID, OLT, Zone, VLAN, WIP, Type, Auth date
- Status pill with pulse animation for online
- Inline "View" button (blue)
- Hex serial shown below main serial

#### DotMac Implementation (`/admin/network/onts?view=list`)
- ✅ Multi-filter bar (OLT, status, signal, zone, vendor)
- ✅ Table with serial, OLT/PON, subscriber, online, signal, last seen, status
- ✅ Hex serial display
- ✅ Inline actions (Configure, Reboot, View)
- ✅ Bulk actions bar (select multiple, choose action, execute)
- ❌ **Gap**: No Profile column
- ❌ **Gap**: No VLAN column
- ❌ **Gap**: No WIP (work-in-progress) indicator

**Compatibility**: ~85% - Very similar, minor column differences

---

### 10. ONT Detail Page

#### SmartOLT ONT Detail
- **Header**: Zone badge, ODB, Name, Address, Contact, Authorization date, ONU external ID
- **Status Section**: Attached VLANs, ONU mode, TR069, Mgmt IP, WAN setup mode, PPPoE username/password
- **Action Buttons Row**: Get status, Show running-config, SW info, TR069 Stat, LIVE!
- **Collapsible Sections**:
  - General (Manufacturer, Model, SW/HW version, Provisioning code, Serial, CPU, RAM, Uptime, Pending provisions)
  - PPP Interface 2.1
  - Port Forward
  - IP Interface 1.1
  - LAN DHCP Server
  - LAN Ports
  - LAN Counters
  - Wireless LAN 1
  - WLAN Counters
  - Wifi 2.4GHz Site Survey
  - Hosts
  - Security
  - Voice Lines
  - Miscellaneous
  - Troubleshooting
  - Device Logs
  - File & Firmware management

#### DotMac Implementation (`/admin/network/onts/{id}`)
- ✅ Header with serial, OLT info, subscriber link
- ✅ Status badges (online, signal quality, authorization)
- ✅ Action buttons (Reboot, Refresh, Factory Reset)
- ✅ Collapsible sections (Management IP, Service Ports, WAN/PPPoE, WiFi, TR-069 Profile, LAN)
- ❌ **Gap**: No "LIVE!" real-time view
- ❌ **Gap**: No show running-config button
- ❌ **Gap**: Fewer diagnostic sections (no Voice Lines, Hosts, Security, Site Survey)
- ❌ **Gap**: No Device Logs section
- ❌ **Gap**: No File & Firmware management section

**Compatibility**: ~65% - Core structure similar, SmartOLT has more diagnostic depth

---

### 11. ONT Configuration Modals

#### SmartOLT "Update ONU mode" Modal
- **Fields**:
  - WAN VLAN-ID: Dropdown with purpose labels (e.g., "203 - Internet")
  - ONU mode: Radio buttons (Routing / Bridging)
  - WAN mode: Radio buttons (Route / PPPoE)
  - Config method: Radio buttons (OMCI / TR069)
  - IP Protocol: Radio buttons (IPv4 / IPv6 / Dual stack)
  - PPPoE username: Text input
  - PPPoE password: Text input with show/hide toggle
- **Buttons**: Close, Update

#### SmartOLT "Update Management and VoIP IP" Modal
- **Fields**:
  - TR069 Profile: Dropdown
  - Mgmt IP: Radio buttons (Disabled / DHCP / Static)
  - Service-port ID: Auto-populated
  - Allow remote access checkbox
  - Mgmt VLAN-ID: Dropdown with purpose labels (e.g., "201 - TR-069")
  - Management IP address: Dropdown of available IPs
  - VoIP service: Radio buttons (Disabled / Enabled)
- **Buttons**: Show Mgmt IP details, Close, Update

#### DotMac Implementation (`_unified_config.html`)
- ✅ Service Profile selector at top
- ✅ Management IP section with mode (DHCP/Static), VLAN dropdown, IP input
- ✅ WAN/PPPoE section with PPPoE username/password
- ✅ WiFi section with SSID/password
- ✅ TR-069 Profile section
- ✅ LAN/Ethernet section
- ❌ **Gap**: No ONU mode toggle (Routing/Bridging) - we handle this differently
- ❌ **Gap**: No Config method toggle (OMCI/TR069)
- ❌ **Gap**: No IP Protocol selector (IPv4/IPv6)
- ❌ **Gap**: No VoIP section in unified config
- ❌ **Gap**: VLAN dropdowns don't show purpose labels inline

**Compatibility**: ~75% - Forms have same fields, different layout (modal vs collapsible sections)

---

### 12. ONU Types (Settings)

#### SmartOLT ONU Types
- **Add ONU type** button
- Table: PON type, Channels, ONU type (model), Ethernet ports, WiFi, VoIP ports, CATV, Allow custom profiles, Capability, Action
- Shows device capabilities matrix
- Delete button per row

#### DotMac Implementation
- ❌ **Not implemented** - No ONU types management page
- We detect model from TR-069/OLT but don't have a capability registry

**Compatibility**: 0% - Feature gap (could use for better defaults)

---

### 13. Speed Profiles (Settings)

#### SmartOLT Speed Profiles
- **Tabs**: Download | Upload
- **Add speed profile** button
- Table: Name, For, Use prefix&suffix, Speed, Type, Default, ONUs, Action
- Shows ONU usage count per profile
- Speed displayed in Kbps with human-readable format

#### DotMac Implementation (`/admin/network/speed-profiles`)
- ✅ Speed profiles listing
- ✅ Download/Upload differentiation
- ✅ Name and speed display
- ❌ **Gap**: No "For" (which OLTs/types) scoping
- ❌ **Gap**: No prefix/suffix option
- ❌ **Gap**: No ONU usage count column
- ❌ **Gap**: No tabbed Download/Upload view

**Compatibility**: ~60% - Basic concept present

---

### 14. TR-069 Profiles (Settings)

#### SmartOLT TR-069 Profiles
- **Tabs**: VPN tunnels | TR069 Profiles
- **Info** collapsible section
- **Defined profiles** section:
  - Table: Profile name, CWMP ACS URL, Status, OLTs (multi-select dropdown), Action
  - "Set OLTs", "View", "Files", "Del" buttons
- **Add a new profile** button

#### DotMac Implementation (`/admin/network/tr069-profiles` or similar)
- ✅ TR-069 profile management
- ✅ ACS URL configuration
- ❌ **Gap**: No VPN tunnels integration
- ❌ **Gap**: No multi-OLT scoping per profile

**Compatibility**: ~70% - Core functionality present

---

### 15. VPN Tunnels (Settings)

#### SmartOLT VPN Tunnels
- **Info** collapsible section
- **Tunnel status** table: #, User/tunnel name, Status, Subnet, Connected subnets, Actions
- Status shows: Connected IP, Tunnel IP, bandwidth used, Since date
- Actions: Mikrotik VPN setup, Edit, Del
- **Create a new tunnel** button

#### DotMac Implementation
- ❌ **Not implemented** - No VPN tunnel management

**Compatibility**: 0% - Feature gap (may not be needed)

---

### 16. Zones (Settings)

#### SmartOLT Zones
- Hierarchical zone management
- Used for geographic/logical grouping
- Zone selector in ONT forms

#### DotMac Implementation (`/admin/network/zones`)
- ✅ Zone management exists
- ✅ Zone selector in forms
- Zone implementation appears equivalent

**Compatibility**: ~90%

---

### 17. ODBs / Splitters (Settings)

#### SmartOLT ODBs
- Optical Distribution Box management
- Links to PON ports
- Shows capacity/used ports

#### DotMac Implementation
- ❌ **Not implemented** - No ODB/splitter management

**Compatibility**: 0% - Feature gap (nice-to-have for plant management)

---

### 18. Users & Permissions (Settings)

#### SmartOLT Users
- **Tabs**: General | Users | API key | Billing
- **Actions**: Create a new user, Create a new group, View groups, Create a new restriction group, View restriction groups, View logs
- Table: Name, Email, 2F Auth, Group, Restriction group, Status, Last login, Action
- Groups: admins, tech_users, readonly_users
- Delete button per row

#### DotMac Implementation (`/admin/settings/users`)
- ✅ User management
- ✅ Role/permission system
- ✅ Groups/roles
- ❌ **Gap**: No 2FA column
- ❌ **Gap**: No restriction groups concept
- ❌ **Gap**: No audit log viewer

**Compatibility**: ~70%

---

### 19. API Keys (Settings)

#### SmartOLT API Keys
- **Generate API key** button
- Table: #, Api key type, Restriction group, Allowed IPs, Actions
- Shows Read & Write, Read only types
- Copy, Edit, Delete buttons

#### DotMac Implementation
- API tokens exist in auth system
- ❌ **Gap**: No dedicated API key management page

**Compatibility**: ~40%

---

### 20. Diagnostics View

#### SmartOLT Diagnostics (from search/filter)
- Table: Status, SN, Signal (with icon), RX/MAC, ONU name, Profile, OLT, Zone, VLAN, WIP, Type, Auth date
- Signal shown with warning/critical icons
- Status pill with pulse

#### DotMac Implementation (`/admin/network/onts?view=diagnostics`)
- ✅ Dedicated diagnostics view
- ✅ Signal quality display with color coding
- ✅ OLT Rx and ONU Rx columns
- ✅ Quality badge (Good/Warning/Critical)
- ✅ Distance column
- ✅ Signal-based sorting (worst first)
- ✅ Filter by Critical Only, Warning+

**Compatibility**: ~95% - Our diagnostics view is well-aligned

---

## Form Compatibility Matrix

| Form | SmartOLT | DotMac | Compatible? | Notes |
|------|----------|--------|-------------|-------|
| ONT Authorization | Modal with OLT, PON, SN, ONU type, mode, VLANs, speeds | Authorize button on unconfigured list | ⚠️ Partial | We authorize from list; SmartOLT has more options |
| ONU Mode | Modal: WAN VLAN, ONU mode, WAN mode, config method, IP protocol, PPPoE | Collapsible section | ⚠️ Partial | Same fields, different layout |
| Management IP | Modal: TR069 Profile, Mgmt IP mode, VLAN, IP, VoIP | Collapsible section | ✅ Yes | Very similar fields |
| PPPoE Credentials | In ONU mode modal | Separate WAN/PPPoE section | ✅ Yes | Same data, different placement |
| WiFi | Separate section/page | Collapsible section | ✅ Yes | Compatible |
| Speed Profiles | Separate admin page | Separate admin page | ✅ Yes | Compatible |
| VLAN Assignment | Dropdown with purpose labels | Dropdown (purpose in enum) | ⚠️ Partial | Need to show labels inline |

---

## UI Pattern Differences

### SmartOLT Patterns
1. **Modal-centric**: Configuration changes happen in modal dialogs
2. **Tabbed navigation**: Sub-pages organized by tabs
3. **Compact density**: More information per screen
4. **Inline actions**: Buttons in table rows
5. **Purpose labels in dropdowns**: VLANs show purpose (e.g., "203 - Internet")
6. **ONU counts**: Settings pages show how many ONUs use each setting
7. **Action buttons at top**: Get status, Show running-config, etc.

### DotMac Patterns
1. **Page-centric**: Configuration in collapsible sections on detail page
2. **View toggle**: List / Diagnostics / Unconfigured views
3. **Service Profile first**: Profile selector at top of config
4. **Dark mode**: Full dark mode support
5. **Bulk actions**: Select multiple, choose action, execute
6. **Modern aesthetics**: Rounded cards, gradients, animations

---

## Required Changes for Full Compatibility

### High Priority (Core UX)

1. **VLAN Dropdown Labels** (Phase 1 from plan)
   - Add purpose labels to VLAN dropdowns: "203 - Internet", "201 - Management"
   - Already have `Vlan.purpose` enum, just need to display it

2. **Speed Dropdown Labels**
   - Format speeds as "1G (1048064 Kbps)" or "10 Mbps"
   - Add `format_speed` filter

3. **ONU Usage Counts** (Phase 4 from plan)
   - Add ONU count column to Speed Profiles, Provisioning Profiles
   - Shows impact before deleting

### Medium Priority (Feature Parity)

4. **ONU Mode Toggle**
   - Add Routing/Bridging toggle to WAN section
   - Maps to `wan_mode` in our model

5. **IP Protocol Selector**
   - Add IPv4/IPv6/Dual stack selector
   - Already have `enable_ipv6_on_wan` task, need UI

6. **Config Method Toggle**
   - OMCI vs TR069 configuration method
   - Affects how we push config

7. **VoIP Section**
   - Add VoIP configuration to unified config
   - Model exists, UI missing

### Low Priority (Nice-to-Have)

8. **OLT Cards Page**
   - Show card inventory per OLT
   - Useful for hardware tracking

9. **OLT Uplinks Page**
   - Uplink port status and configuration
   - Useful for troubleshooting

10. **ONU Types Registry**
    - Device capability matrix
    - Enables smarter defaults

11. **Show Running Config Button**
    - Display current OLT config for ONT
    - Useful for debugging

12. **LIVE! Real-time View**
    - WebSocket-based real-time status
    - High effort, high value

---

## Recommended Implementation Order

Based on the plan already in `/root/.claude/plans/lively-leaping-llama.md`:

1. **Phase 1: Labeled Dropdowns** - VLAN and speed dropdowns with purpose labels
2. **Phase 2: Collapsible Sections** - Already implemented in `_unified_config.html`
3. **Phase 3: Profile Integration** - Service profile selector already present
4. **Phase 4: Usage Counts** - Add ONU counts to settings pages
5. **Phase 5: Quick Actions** - Already have Configure/Reboot in list view
6. **Phase 6: Modal Consistency** - Refine modal patterns

**Additional items from this analysis:**
- Add ONU mode (Routing/Bridging) toggle
- Add IP Protocol selector
- Add VoIP section
- Consider OLT Cards and Uplinks pages

---

## Conclusion

Our UI is **conceptually compatible** with SmartOLT's workflows. The main differences are:

1. **Layout philosophy**: We use a unified config page with collapsible sections; SmartOLT uses modals
2. **Feature depth**: SmartOLT has more diagnostic sections (Hosts, Security, Voice Lines, etc.)
3. **Labeling**: SmartOLT shows purpose labels inline in dropdowns
4. **Usage counts**: SmartOLT shows how many ONUs use each setting

The plan in `lively-leaping-llama.md` addresses the highest-impact items (Phases 1-4). This report identifies additional gaps that can be addressed in future iterations.

**Bottom line**: A NOC technician familiar with SmartOLT would find our interface navigable, but would miss the inline purpose labels and usage counts that help with quick decision-making.
