# SmartOLT UI Implementation Plan

> **Status: historical comparative plan.** SmartOLT patterns are research input,
> not Dotmac requirements. Revalidate every proposed field, chart, action, and
> workflow against `UI_INFORMATION_AND_ACTION_STANDARD.md`, the current network
> SOT, and `PRODUCTION_UI_BRIEF.md` before implementation.

**Date**: 2026-04-22
**Last Updated**: 2026-04-22
**Status**: Planning
**Estimated Effort**: ~2.5 weeks

---

## Overview

This plan details the work required to bring DotMac Sub's UI to feature parity with SmartOLT's NOC-optimized patterns. It is organized by page/feature area with specific tasks, files to modify, and priority levels.

### Key Findings (Pre-Implementation Audit)

**Already Implemented**:
- `format_speed` filter exists in `app/web/templates.py`
- `vlan_label` filter exists in `app/web/templates.py`
- `voip_enabled` field exists on `OntUnit` and `OntProvisioningProfile` models
- VoIP VLAN purpose enum exists (`VlanPurpose.voip`)

**Actual File Paths** (corrected from original plan):
- Filters: `app/web/templates.py` (not `app/web/template_filters.py`)
- Speed profiles: `templates/admin/network/speed-profiles/`
- Provisioning profiles: `templates/admin/network/provisioning-profiles/`

**Dependencies**:
- "Show running-config" requires SSH connectivity to OLT (may timeout/fail)
- VoIP UI can use existing model fields, no migration needed
- LIVE! view requires WebSocket infrastructure not yet in place

---

## Priority Levels

- **P1 - High**: Major UX improvements, high NOC impact, implement first
- **P2 - Medium**: Feature parity, improves workflow efficiency
- **P3 - Low**: Polish, advanced features, can defer

> **Note**: No P0 items - nothing currently blocks NOC workflows entirely. VLAN/speed labels are UX polish (P2), not blockers.

---

## Page-by-Page Implementation

### 1. Dashboard (`/admin/dashboard`)

**Current State**: Basic stats cards and recent activity
**SmartOLT Features Missing**: Network status charts, PON outage table, auto-backup notifications

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add daily network status area chart | P2 | `templates/admin/dashboard/index.html`, `app/services/web_dashboard.py` | 1 day |
| Add ONU authorizations per day bar chart | P3 | Same as above | 0.5 day |
| Add PON outage table | P1 | Same + new partial | 1 day |
| Add OLT temperature sidebar | P2 | Same | 0.5 day |
| Add auto-backup notification feed | P3 | Same | 0.5 day |

**Total**: ~3.5 days

---

### 2. OLT List (`/admin/network/olts`)

**Current State**: Good coverage
**SmartOLT Features Missing**: Hardware/software version columns

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add hardware version column | P3 | `templates/admin/network/olts/index.html` | 0.25 day |
| Add software version column | P3 | Same | 0.25 day |

**Total**: 0.5 day

---

### 3. OLT Details (`/admin/network/olts/{id}`)

**Current State**: Single page with all info
**SmartOLT Features Missing**: Tabbed navigation, CLI shortcut, config backups

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add tabbed navigation (Details, Cards, PON Ports, Uplinks, VLANs) | P2 | `templates/admin/network/olts/detail.html` | 1 day |
| Add "CLI" shortcut button (SSH link or web terminal) | P3 | Same | 0.5 day |
| Add Config Backups sub-page | P3 | New template + endpoint | 1 day |

**Total**: 2.5 days

---

### 4. OLT Cards (`/admin/network/olts/{id}/cards`) - NEW PAGE

**Current State**: Not implemented
**SmartOLT Features**: Card inventory, reboot-card action

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Create OLT cards model (if not exists) | P3 | `app/models/olt_card.py` | 0.5 day |
| Create cards listing endpoint | P3 | `app/web/admin/network_olts.py` | 0.5 day |
| Create cards template | P3 | `templates/admin/network/olts/cards.html` | 0.5 day |
| Add "Refresh cards" action | P3 | Same | 0.25 day |
| Add "Reboot card" action | P3 | Same | 0.25 day |

**Total**: 2 days

---

### 5. PON Ports (`/admin/network/olts/{id}/pon-ports`)

**Current State**: Basic listing plus direct per-port Enable/Disable for supported
Huawei OLTs. The control is permission-gated, respects OLT write mode, records an
audit event, and updates local administrative state only after device acceptance.

**SmartOLT Features Missing**: Enable/Disable all, AutoFind toggle, ODB assignment

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add "Enable all PON ports" bulk action | P2 | `app/web/admin/network_olts.py`, template | 0.5 day |
| Add "Disable AutoFind" toggle | P2 | Same | 0.5 day |
| Add ODB (splitter) dropdown per port | P3 | Same + ODB model | 1 day |
| Add "Refresh ONUs" button | P2 | Same | 0.25 day |

**Total**: 2.25 days

---

### 6. OLT Uplinks (`/admin/network/olts/{id}/uplinks`) - NEW PAGE

**Current State**: Not implemented
**SmartOLT Features**: Uplink port status, VLAN tagging config

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Create uplinks listing endpoint | P3 | `app/web/admin/network_olts.py` | 0.5 day |
| Create uplinks template | P3 | `templates/admin/network/olts/uplinks.html` | 0.5 day |
| Add Configure button per uplink | P3 | Same | 0.5 day |
| Fetch uplink status from OLT | P3 | `app/services/network/olt_ssh.py` | 1 day |

**Total**: 2.5 days

---

### 7. VLANs (`/admin/network/vlans`)

**Current State**: Basic VLAN management
**SmartOLT Features Missing**: Per-OLT scoping, ONU counts, IGMP/DHCP snooping flags

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add ONU count column | P1 | `app/services/web_network_vlans.py`, template | 0.5 day |
| Add IGMP snooping toggle | P3 | Model + template | 0.5 day |
| Add DHCP snooping toggle | P3 | Same | 0.5 day |
| Add per-OLT VLAN scoping view | P2 | New endpoint + template | 1 day |

**Total**: 2.5 days

---

### 8. Unconfigured Devices (`/admin/network/onts?view=unconfigured`)

**Current State**: Good core workflow
**SmartOLT Features Missing**: Auto-authorization, presets, pre-staging

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add "Authorization Presets" management | P2 | New model + endpoints + templates | 1.5 days |
| Add "Auto actions" configuration | P2 | Same | 1 day |
| Add "Add ONU for later authorization" | P3 | `app/web/admin/network_onts.py` | 0.5 day |

**Total**: 3 days

---

### 9. Configured ONTs List (`/admin/network/onts?view=list`)

**Current State**: Excellent - bulk actions, filters, inline actions
**SmartOLT Features Missing**: Profile column, VLAN column

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add Profile column | P1 | `templates/admin/network/onts/index.html` | 0.25 day |
| Add VLAN column (primary service VLAN) | P2 | Same | 0.25 day |
| Add WIP (work-in-progress) indicator | P3 | Same + service order integration | 0.5 day |

**Total**: 1 day

---

### 10. ONT Detail (`/admin/network/onts/{id}`)

**Current State**: Good collapsible sections
**SmartOLT Features Missing**: LIVE! view, show running-config, more diagnostic sections

**Dependencies**:
- "Show running-config" requires SSH to OLT - **may fail if OLT unreachable**
- LIVE! requires WebSocket + reconnection + multi-stream handling

| Task | Priority | Files | Effort | Tests |
|------|----------|-------|--------|-------|
| Add "Show running-config" button | P2 | `app/web/admin/network_onts.py`, `_unified_config.html` | 1 day | `tests/test_web_network_ont_actions.py` |
| Add VoIP section | P2 | `templates/admin/network/onts/_config_voip.html` | 0.75 day | Manual + integration |
| Add Hosts (connected devices) section | P2 | New partial + TR-069 fetch | 1.5 days | `tests/test_web_network_onts.py` |
| Add Device Logs section | P3 | Same | 0.75 day | Manual |
| Add LIVE! real-time view (WebSocket) | P3 | New WebSocket handler, JS client, reconnection logic | **5 days** | New test file |
| Add File & Firmware management section | P2 | New partial | 1 day | Manual |

**Total**: 10 days (5 days without LIVE!)

---

### 11. ONT Configuration Forms

**Current State**: Unified config with collapsible sections
**SmartOLT Features Missing**: ONU mode toggle, IP protocol selector, VLAN labels in dropdowns

**Already Done**:
- `format_speed` filter exists in `app/web/templates.py`
- `vlan_label` filter exists in `app/web/templates.py`
- `voip_enabled` field exists on models

| Task | Priority | Files | Effort | Tests |
|------|----------|-------|--------|-------|
| **Use `vlan_label` filter in VLAN dropdowns** | P2 | `templates/admin/network/onts/_config_*.html` | 0.25 day | Manual |
| **Use `format_speed` in speed dropdowns** | P2 | Same + `_unified_config.html` | 0.25 day | Manual |
| Add ONU mode toggle (Routing/Bridging) | P1 | `templates/admin/network/onts/_config_wan.html` | 0.5 day | `tests/test_web_network_ont_actions.py` |
| Add IP Protocol selector (IPv4/IPv6/Dual) | P2 | Same | 0.5 day | Same |
| Add Config method toggle (OMCI/TR069) | P2 | Same | 0.5 day | Same |
| Add VoIP section to unified config | P2 | `templates/admin/network/onts/_config_voip.html` (new) | 0.75 day | `tests/test_web_network_ont_action_setters.py` |

**Total**: 2.75 days

---

### 12. ONU Types (`/admin/network/onu-types`) - NEW PAGE

**Current State**: Not implemented
**SmartOLT Features**: Device capability matrix

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Create ONU type model | P3 | `app/models/onu_type.py` | 0.5 day |
| Create CRUD service | P3 | `app/services/network/onu_types.py` | 0.5 day |
| Create admin endpoints | P3 | `app/web/admin/network_onu_types.py` | 0.5 day |
| Create list/form templates | P3 | `templates/admin/network/onu-types/` | 1 day |
| Auto-populate from TR-069 device info | P3 | Integration work | 1 day |

**Total**: 3.5 days

---

### 13. Speed Profiles (`/admin/network/speed-profiles`)

**Current State**: Basic CRUD
**SmartOLT Features Missing**: ONU counts, For (OLT scoping), prefix/suffix, tabbed view

| Task | Priority | Files | Effort | Tests |
|------|----------|-------|--------|-------|
| **Add ONU usage count column** | P1 | `app/services/network/speed_profiles.py`, `templates/admin/network/speed-profiles/index.html` | 0.5 day | `tests/test_speed_profiles.py` |
| Add Download/Upload tabs | P2 | Template restructure | 0.5 day | Manual |
| Add "For" OLT scoping field | P3 | Model + form + migration | 0.75 day | Model tests |
| Add prefix/suffix option | P3 | Model + form + migration | 0.5 day | Model tests |

**Total**: 2.25 days

---

### 14. Provisioning Profiles (`/admin/network/provisioning-profiles`)

**Current State**: Good CRUD
**SmartOLT Features Missing**: ONT counts

| Task | Priority | Files | Effort | Tests |
|------|----------|-------|--------|-------|
| **Add ONT usage count column** | P1 | `app/services/network/ont_provisioning_profiles.py`, `templates/admin/network/provisioning-profiles/index.html` | 0.5 day | `tests/test_ont_provisioning_profiles.py` |
| Add profile preview on hover/click | P2 | Template + HTMX partial | 0.5 day | Manual |

**Total**: 1 day

---

### 15. TR-069 Profiles (`/admin/network/tr069-profiles`)

**Current State**: Basic management
**SmartOLT Features Missing**: Multi-OLT scoping, status indicator

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Add multi-OLT assignment | P2 | Model update + form | 1 day |
| Add CWMP status indicator | P2 | Template + health check | 0.5 day |

**Total**: 1.5 days

---

### 16. Diagnostics View (`/admin/network/onts?view=diagnostics`)

**Current State**: Excellent - matches SmartOLT well
**SmartOLT Features Missing**: None significant

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| (Optional) Add sparkline signal history | P3 | Chart integration | 1 day |

**Total**: 1 day (optional)

---

### 17. Users & Permissions (`/admin/settings/users`)

**Current State**: Basic user management
**SmartOLT Features Missing**: 2FA indicator, restriction groups, audit logs

**Complexity Warning**: Restriction groups requires modifying permission checks across all routes.

| Task | Priority | Files | Effort | Tests |
|------|----------|-------|--------|-------|
| Add 2FA status column | P2 | Template | 0.25 day | Manual |
| Add restriction groups concept | P3 | Model + CRUD + **all route decorators** | **4 days** | `tests/test_auth*.py` |
| Add audit log viewer | P2 | New page + model + middleware | 2.5 days | New test file |

**Total**: 6.75 days

---

### 18. API Keys (`/admin/settings/api-keys`) - NEW PAGE

**Current State**: Not implemented as dedicated page
**SmartOLT Features**: Generate, restrict, manage API keys

| Task | Priority | Files | Effort |
|------|----------|-------|--------|
| Create API key management page | P2 | New endpoints + templates | 1.5 days |
| Add IP restriction option | P3 | Model update | 0.5 day |
| Add Read/Write type selection | P2 | Model + form | 0.5 day |

**Total**: 2.5 days

---

## Implementation Phases

### Phase 1: Core UX Improvements (P1 + P2) - Week 1

**Goal**: Use existing filters, add usage counts, essential form improvements

| Day | Tasks |
|-----|-------|
| 1 | Apply `vlan_label` filter to all VLAN dropdowns, apply `format_speed` to speed dropdowns |
| 2 | Add ONU usage count method to `speed_profiles.py`, update Speed Profiles template |
| 3 | Add ONT usage count method to `ont_provisioning_profiles.py`, update Provisioning Profiles template |
| 4 | Add ONU mode toggle (Routing/Bridging) to WAN section, add Profile column to ONT list |
| 5 | Testing, bug fixes, test coverage for new features |

**Deliverables**:
- VLAN dropdowns display "203 - Internet" format
- Speed dropdowns display "100 Mbps" format
- Usage counts visible on settings pages
- ONU mode toggle functional
- Test coverage added

**Test Files to Update**:
- `tests/test_web_network_ont_actions.py` - ONU mode toggle
- `tests/test_speed_profiles.py` - usage counts (create if needed)

---

### Phase 2: Feature Parity (P2) - Week 2

**Goal**: Fill major feature gaps, add diagnostics

| Day | Tasks |
|-----|-------|
| 1 | VoIP section in unified config (uses existing `voip_enabled` field) |
| 2 | IP Protocol selector, PON outage table on dashboard |
| 3 | Per-OLT VLAN view, VLAN ONU counts |
| 4 | "Show running-config" button (with error handling for SSH failures) |
| 5 | Testing, bug fixes, documentation |

**Deliverables**:
- VoIP configuration UI
- PON outage monitoring
- Per-OLT VLAN view
- Show running-config (graceful degradation if OLT unreachable)

**SSH Failure Fallback UX**: When "Show running-config" cannot reach the OLT, display an amber toast with "OLT unreachable - showing cached config from [timestamp]" and render the last-known config if available, or a clear error message if not.

**Test Files to Update**:
- `tests/test_web_network_ont_action_setters.py` - VoIP
- `tests/test_web_network_olts.py` - PON outage data

---

### Phase 3: Advanced Features (P3) - Future Sprints

**Goal**: Feature completeness, advanced diagnostics

| Feature | Effort | Dependencies |
|---------|--------|--------------|
| LIVE! real-time view | 5+ days | WebSocket infrastructure, reconnection logic, multi-stream |
| ONU types registry | 3.5 days | None |
| Authorization presets | 2.5 days | New model + endpoints + templates (from Section 8) |
| Restriction groups | 3+ days | RBAC refactor across all routes |
| Audit log viewer | 2.5 days | Audit logging infrastructure |
| Network status charts | 2 days | Chart library (Chart.js or similar) |
| Config backups page | 1.5 days | Backup storage mechanism |
| OLT cards page | 2 days | SSH card enumeration |
| OLT uplinks page | 2.5 days | SSH uplink status parsing |

**Note**: Restriction groups requires touching permission checks across the entire application - estimate of 2 days was too low.

---

## Quick Reference: File Changes by Phase

### Phase 1 Files

**Already Exists** (no changes needed):
```
app/web/templates.py                     # format_speed, vlan_label filters already present
```

**New Files**:
```
(None required - filters exist, macros are optional)
```

> **Decision**: Use `{{ vlan | vlan_label }}` and `{{ speed_kbps | format_speed }}` filters inline instead of creating reusable macro files. This is simpler and the filters already work.

**Modified Files**:
```
templates/admin/network/onts/_config_wan.html       # Add ONU mode toggle, use vlan_label filter
templates/admin/network/onts/index.html             # Add Profile column
```

> **Note**: Usage count methods and columns already exist:
> - `speed_profiles.count_by_profile()` + `profile_counts` in template
> - `ont_provisioning_profiles.count_onts_by_profile()` + `usage_counts` in template
> - `format_speed` filter already used in `_unified_config.html`

**Test Files** (add coverage):
```
tests/test_web_network_ont_actions.py    # Add ONU mode toggle tests
tests/test_speed_profiles.py             # Add usage count tests (if not exists)
```

### Phase 2 Files

**New Files**:
```
templates/admin/network/onts/_config_voip.html    # VoIP toggle + VLAN selector
templates/admin/dashboard/_pon_outage.html        # PON outage table partial
```

**Modified Files**:
```
app/web/admin/network_onts.py               # Show running-config endpoint
app/services/web_dashboard.py               # PON outage data aggregation
app/services/web_network_vlans.py           # Add ONU count method
templates/admin/dashboard/index.html        # Include PON outage partial
templates/admin/network/vlans/index.html    # Add ONU counts column
templates/admin/network/onts/_config_wan.html    # IP Protocol selector
templates/admin/network/onts/_unified_config.html  # Include VoIP section
```

**Test Files to Update**:
```
tests/test_web_network_ont_action_setters.py  # VoIP toggle tests
tests/test_web_network_olts.py                # PON outage data tests
tests/test_web_network_vlans.py               # ONU count tests
```

---

## Verification Checklist

### After Phase 1
- [x] Speed Profiles page shows ONU count per profile (already done)
- [x] Provisioning Profiles page shows ONT count per profile (already done)
- [x] Speed dropdowns show "1G" or "10 Mbps" format in profile selector (already done)
- [ ] VLAN dropdowns show "203 - Internet" format (use `vlan_label` filter)
- [ ] ONT list has Profile column
- [ ] ONU mode toggle works (Routing/Bridging)
- [ ] **Tests pass**: `pytest tests/test_web_network_ont_actions.py -v`

### After Phase 2
- [ ] VoIP section appears in ONT unified config (toggle + VLAN selector)
- [ ] IP Protocol selector works (IPv4/IPv6/Dual stack)
- [ ] Dashboard shows PON outage table
- [ ] "Show running-config" gracefully handles OLT unreachable
- [ ] VLANs page shows ONU count per VLAN
- [ ] **Tests pass**: `pytest tests/test_web_network_ont_action_setters.py -v`

### Manual Testing
- [ ] Dark mode works on all new components
- [ ] Mobile responsive layout
- [ ] HTMX interactions work without full page reload
- [ ] Forms validate correctly
- [ ] Error states display properly (especially SSH failures)
- [ ] Filter dropdowns maintain selection after form submit

### Automated Test Coverage
Each phase should maintain or improve coverage:
```bash
# Run after each phase
pytest tests/test_web_network*.py -v --tb=short
ruff check app/web/admin/network_onts.py app/services/web_network_onts.py
mypy app/web/admin/network_onts.py --ignore-missing-imports
```

---

## Notes

- **No migrations required** - `voip_enabled`, VoIP VLANs, all models already exist
- **Filters already exist** - `format_speed`, `vlan_label` in `app/web/templates.py`
- **SSH dependency** - "Show running-config" may fail if OLT unreachable; implement graceful error
- **WebSocket gap** - LIVE! view requires new infrastructure (5+ days, not 3)
- **RBAC scope** - Restriction groups touches permission checks app-wide (3+ days, not 2)
- Each phase is independently deployable
- Backwards compatible - existing workflows continue to work
