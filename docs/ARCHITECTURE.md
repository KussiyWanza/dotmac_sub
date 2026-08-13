# DotMac Sub - Architecture Documentation

This document provides a comprehensive overview of the DotMac Sub system architecture, a multi-tenant subscription management system for ISPs and fiber network operators.

---

## Table of Contents

1. [Overall Architecture & Project Structure](#1-overall-architecture--project-structure)
2. [Key Models & Relationships](#2-key-models--relationships)
3. [Service Layer Patterns & Business Logic Flows](#3-service-layer-patterns--business-logic-flows)
4. [API & Web Route Organization](#4-api--web-route-organization)
5. [Authentication Flows](#5-authentication-flows-for-different-portals)
6. [Billing & Payment Flows](#6-billing--payment-flows)
7. [Network Provisioning Flows](#7-network-provisioning-flows)
8. [Background Task Processing (Celery)](#8-background-task-processing-celery)
9. [Key Integrations & External Dependencies](#9-key-integrations--external-dependencies)
10. [Summary & Key Architectural Patterns](#10-summary--key-architectural-patterns)

---

## 1. Overall Architecture & Project Structure

### Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI (Python 3.11+) |
| Database | PostgreSQL with PostGIS support |
| ORM | SQLAlchemy 2.0 with declarative models |
| Templates | Jinja2 |
| Frontend | HTMX + Alpine.js + Tailwind CSS v4 |
| Task Queue | Celery + Redis |
| Real-time projection | Redis pub/sub + WebSocket/SSE (`docs/REALTIME_PLATFORM.md`) |
| Migrations | Alembic (119 migration files) |
| Testing | pytest, Playwright (E2E) |

### Directory Structure

```
/root/projects/dotmac_sub/
├── app/
│   ├── main.py                 # FastAPI app initialization, middleware, routers
│   ├── config.py               # Environment configuration (pydantic)
│   ├── db.py                   # Database session management, SessionLocal
│   ├── celery_app.py           # Celery configuration
│   ├── csrf.py                 # Double-submit cookie CSRF protection
│   ├── errors.py               # Global error handlers
│   ├── logging.py              # Logging configuration
│   │
│   ├── api/                    # REST API endpoints (JSON, also at /api/v1)
│   │   ├── subscribers.py      # Subscriber/Organization/Reseller CRUD
│   │   ├── billing.py          # Invoices, payments, credit notes
│   │   ├── catalog.py          # CatalogOffer, Subscription management
│   │   ├── auth.py             # User credential, MFA, API key management
│   │   ├── auth_flow.py        # Login/logout/token endpoints
│   │   ├── provisioning.py     # Service orders, appointments, tasks
│   │   ├── radius.py           # RADIUS servers, clients, sync jobs
│   │   ├── network.py          # Network devices, CPE, OLT, fiber plant
│   │   ├── domains.py          # Multi-tenancy domain routing
│   │   ├── search.py           # Full-text search across entities
│   │   ├── imports.py          # CSV/bulk import handlers
│   │   ├── webhooks.py         # Webhook endpoints and management
│   │   ├── notifications.py    # Notification templates and delivery
│   │   ├── integrations.py     # External system integrations
│   │   ├── scheduler.py        # Task scheduling
│   │   ├── bandwidth.py        # Bandwidth monitoring and QoS
│   │   └── [20+ more specialized endpoints]
│   │
│   ├── web/                    # Web portal routes (HTML via Jinja2)
│   │   ├── admin/              # Admin portal (/admin/*)
│   │   │   ├── dashboard.py
│   │   │   ├── subscribers.py
│   │   │   ├── billing.py
│   │   │   ├── catalog.py
│   │   │   ├── network.py
│   │   │   ├── usage.py
│   │   │   ├── reports.py
│   │   │   ├── notifications.py
│   │   │   ├── integrations.py
│   │   │   └── system.py
│   │   ├── customer/           # Customer portal (/portal/*)
│   │   │   └── routes.py
│   │   ├── reseller/           # Reseller portal (/reseller/*)
│   │   │   └── routes.py
│   │   ├── auth/               # Authentication routes
│   │   │   ├── routes.py
│   │   │   └── dependencies.py
│   │   └── public/             # Public pages
│   │
│   ├── models/                 # SQLAlchemy ORM models (40+ files)
│   │   ├── subscriber.py       # Unified Subscriber, Organization, Reseller
│   │   ├── catalog.py          # CatalogOffer, Subscription, Pricing
│   │   ├── billing.py          # Invoice, Payment, CreditNote, Ledger
│   │   ├── provisioning.py     # ServiceOrder, Tasks, Workflows
│   │   ├── network.py          # CPE, OLT, ONT, Fiber plant, VLAN, IP pools
│   │   ├── radius.py           # RADIUS servers, clients, users
│   │   ├── auth.py             # UserCredential, MFAMethod, Session, ApiKey
│   │   ├── usage.py            # QuotaBucket, RadiusAccountingSession, UsageRecord
│   │   ├── collections.py      # Dunning cases/actions and access-consequence evidence
│   │   ├── domain_settings.py  # Multi-tenant configuration
│   │   ├── event_store.py      # Event store for event sourcing
│   │   ├── rbac.py             # Roles, Permissions, SubscriberRole
│   │   ├── lifecycle.py        # SubscriptionLifecycleEvent
│   │   ├── network_monitoring.py # Alerts, monitoring rules
│   │   ├── payment_arrangement.py # Payment plans
│   │   ├── subscription_change.py # Change requests
│   │   ├── notification.py     # Templates, delivery logs
│   │   ├── webhook.py          # Webhook endpoints, subscriptions
│   │   └── [15+ more models]
│   │
│   ├── services/               # Business logic (manager/service classes)
│   │   ├── subscriber.py       # Organizations, Resellers, Subscribers
│   │   ├── subscription_engine.py # Subscription engine configuration
│   │   ├── billing/            # Billing subdomain
│   │   │   ├── invoices.py     # Invoice creation and management
│   │   │   ├── payments.py     # Payment processing
│   │   │   ├── credit_notes.py # Credit note handling
│   │   │   ├── ledger.py       # Ledger entries and balancing
│   │   │   ├── runs.py         # Billing run orchestration
│   │   │   ├── providers.py    # Payment provider integrations
│   │   │   ├── _common.py      # Shared billing utilities
│   │   │   └── reporting.py    # Billing analytics
│   │   ├── catalog/            # Catalog/pricing subdomain
│   │   │   ├── subscriptions.py
│   │   │   ├── credentials.py
│   │   │   └── catalog.py
│   │   ├── collections/        # Dunning/collections
│   │   │   ├── _core.py
│   │   │   └── collections.py
│   │   ├── network/            # Network services
│   │   │   ├── ip.py           # IP pool management
│   │   │   ├── cpe.py          # CPE/ONT device management
│   │   │   └── network.py
│   │   ├── events/             # Event system
│   │   │   ├── dispatcher.py   # Central event dispatcher
│   │   │   ├── types.py        # Event type enums (~40 event types)
│   │   │   └── handlers/       # Event handlers
│   │   │       ├── enforcement.py # Throttle/suspend logic
│   │   │       ├── lifecycle.py   # Subscription lifecycle
│   │   │       ├── notification.py # Notifications
│   │   │       ├── provisioning.py # Provisioning workflow
│   │   │       └── webhook.py     # Webhook delivery
│   │   ├── auth.py             # User credentials, MFA, API keys
│   │   ├── auth_flow.py        # Login, logout, JWT, RADIUS auth
│   │   ├── auth_dependencies.py # Dependency injection for auth
│   │   ├── provisioning.py     # Service orders, appointments, tasks
│   │   ├── radius.py           # RADIUS server/client sync
│   │   ├── enforcement.py      # Service enforcement (throttle, block)
│   │   ├── network.py          # Network device/fiber management
│   │   ├── usage.py            # Usage recording and rating
│   │   ├── bandwidth.py        # Bandwidth monitoring
│   │   ├── notification.py     # Notification dispatch
│   │   ├── billing_automation.py # Automated billing tasks
│   │   ├── subscription_changes.py # Upgrade/downgrade handling
│   │   └── [30+ more services]
│   │
│   ├── schemas/                # Pydantic request/response models
│   │   ├── subscriber.py
│   │   ├── catalog.py
│   │   ├── billing.py
│   │   ├── provisioning.py
│   │   ├── auth_flow.py
│   │   ├── network.py
│   │   ├── usage.py
│   │   └── [10+ more]
│   │
│   ├── validators/             # Input validation utilities
│   │   ├── catalog.py
│   │   ├── network.py
│   │   ├── provisioning.py
│   │   └── subscriber.py
│   │
│   ├── tasks/                  # Celery background tasks (26 files)
│   │   ├── bandwidth.py        # Bandwidth monitoring tasks
│   │   ├── billing.py          # Invoice/payment processing
│   │   ├── catalog.py          # Catalog sync tasks
│   │   ├── collections.py      # Dunning/collections
│   │   ├── events.py           # Event handler retry
│   │   ├── gis.py              # GIS data sync
│   │   ├── integrations.py     # External system sync
│   │   ├── nas.py              # NAS device provisioning
│   │   ├── notifications.py    # Notification delivery
│   │   ├── oauth.py            # OAuth token refresh
│   │   ├── radius.py           # RADIUS sync
│   │   ├── snmp.py             # SNMP polling
│   │   ├── usage.py            # Usage rating/charging
│   │   ├── webhooks.py         # Webhook delivery retry
│   │   ├── wireguard.py        # WireGuard config
│   │   └── [more]
│   │
│   ├── websocket/              # WebSocket support
│   │   └── manager.py
│   │
│   └── poller/                 # Background polling
│
├── templates/                  # Jinja2 templates
│   ├── layouts/
│   │   ├── admin.html          # Admin base layout with sidebar
│   │   ├── customer.html       # Customer portal layout
│   │   └── reseller.html
│   ├── admin/                  # Admin UI templates
│   ├── customer/               # Customer portal templates
│   ├── auth/                   # Login, MFA, registration
│   ├── errors/                 # Error pages (404, 500, etc)
│   └── components/             # Reusable template components
│
├── static/                     # CSS, JS, images
├── tests/                      # pytest test files
├── alembic/                    # Database migrations (66 versions)
├── scripts/                    # Utility scripts
├── docs/                       # Documentation
├── docker-compose.yml          # Local dev environment
└── pyproject.toml              # Poetry dependencies
```

---

## 2. Key Models & Relationships

### Core Entity: Unified Subscriber Model

The **Subscriber** model (~400 lines) is the central entity combining identity, account, and billing information:

```
Subscriber
├── Identity Fields
│   ├── first_name, last_name, display_name
│   ├── email (unique), phone
│   ├── date_of_birth, gender
│   ├── preferred_contact_method, locale, timezone
│   └── address_line1-2, city, region, postal_code, country_code
│
├── Account Fields
│   ├── subscriber_number (unique)
│   ├── account_number
│   ├── account_start_date
│   ├── status (active, suspended, canceled, delinquent)
│   ├── is_active, marketing_opt_in
│   └── notes, metadata
│
├── Billing Fields
│   ├── tax_rate_id (FK)
│   ├── billing_enabled
│   ├── billing_name, billing_address_*
│   ├── payment_method
│   ├── deposit, billing_day, payment_due_days, grace_period_days
│   ├── min_balance, prepaid_low_balance_at, prepaid_deactivation_at
│   └── [timestamps: created_at, updated_at]
│
├── Organization Relationships
│   ├── organization_id → Organization
│   ├── reseller_id → Reseller
│   └── tax_rate_id → TaxRate
│
├── Service Relationships
│   ├── subscriptions → Subscription[]
│   ├── service_orders → ServiceOrder[]
│   ├── cpe_devices → CPEDevice[]
│   ├── ip_assignments → IPAssignment[]
│   ├── ont_assignments → OntAssignment[]
│   ├── access_credentials → AccessCredential[]
│   ├── dunning_cases → DunningCase[]
│   ├── addresses → Address[]
│   ├── custom_fields → SubscriberCustomField[]
│   └── channels → SubscriberChannel[]
│
└── Auth Relationships
    └── credentials → UserCredential[]
```

**Supporting Models**:
- **Organization**: B2B subscriber, has many Subscribers
- **Reseller**: ISP reseller/partner, manages Subscribers
- **Address**: Multiple service/billing addresses per Subscriber
- **SubscriberCustomField**: Extensible custom attributes
- **SubscriberChannel**: Contact preferences (email, phone, SMS, push)

### Subscription & Catalog Models

```
CatalogOffer (Service Plan)
├── name, code, description
├── service_type (residential, business)
├── access_type (fiber, fixed_wireless, dsl, cable)
├── price_basis (flat, usage, tiered, hybrid)
├── billing_cycle (daily, weekly, monthly, annual)
├── billing_mode (prepaid, postpaid)
├── contract_term (month_to_month, 12-month, 24-month)
│
├── Pricing & Product
│   ├── region_zone_id → RegionZone
│   ├── usage_allowance_id → UsageAllowance
│   ├── sla_profile_id → SlaProfile
│   ├── policy_set_id → PolicySet
│   ├── speeds (download_mbps, upload_mbps, guaranteed_speed)
│   ├── with_vat, vat_percent
│   └── add_ons → OfferAddOn[]
│
└── Provisioning
    ├── available_for_services
    ├── show_on_customer_portal
    └── status (active, inactive, archived)

The former "External Integration" block (`splynx_tariff_id`,
`splynx_service_name`, `splynx_tax_id`) is gone from the model, schema, and
admin UI. Nothing ever read those three columns — the migration populated them
and the offer form wrote them back, but no behaviour consulted them, and the
system they referenced is retired. The columns remain unmapped in the database
and should be dropped as their own change once they have been observed doing
nothing across a release.

Subscription (Active Service)
├── subscriber_id → Subscriber
├── offer_id → CatalogOffer
├── status (pending, active, suspended, canceled, expired)
├── billing_period_start, billing_period_end
├── active_from, active_until
├── contract_start_date, contract_end_date
├── monthly_charge, setup_charge, activation_charge
├── is_active, auto_renew
│
├── Usage & Billing
│   ├── quota_buckets → QuotaBucket[]
│   ├── dunning_cases → DunningCase[]
│   ├── subscription_addons → SubscriptionAddOn[]
│   └── billing references
│
├── Network Resources
│   ├── cpe_devices → CPEDevice[]
│   ├── ip_assignments → IPAssignment[]
│   ├── ont_assignments → OntAssignment[]
│   └── access_credentials → AccessCredential[]
│
└── Lifecycle
    ├── service_orders → ServiceOrder[]
    ├── state_transitions → SubscriptionLifecycleEvent[]
    └── change_requests → SubscriptionChangeRequest[]

PolicySet (Subscription Policies)
├── proration_policy, downgrade_policy, refund_policy
├── trial_days, grace_days, refund_window_days
├── suspension_action (legacy compatibility data; not an execution input)
└── dunning_steps → PolicyDunningStep[]
    ├── day_offset (days after effective grace ends)
    └── action (notify, throttle, suspend, reject)
```

### Billing Models

```
Invoice
├── account_id → Subscriber
├── invoice_number, currency (NGN, USD, etc)
├── status (draft, issued, partially_paid, paid, void, overdue, written_off)
├── subtotal, tax_total, total, balance_due
├── billing_period_start, billing_period_end
├── issued_at, due_at, paid_at
├── memo
│
├── lines → InvoiceLine[]
├── payments → Payment[]
├── payment_allocations → PaymentAllocation[]
├── ledger_entries → LedgerEntry[]
└── credit_note_applications → CreditNoteApplication[]

Payment
├── account_id → Subscriber
├── invoice_id → Invoice (optional)
├── payment_number
├── status (pending, succeeded, failed, refunded, partially_refunded)
├── amount, currency
├── payment_method_id → PaymentMethod
├── payment_provider_id → PaymentProvider
├── reference (external transaction ID)
├── processed_at, failed_at
└── payment_allocations → PaymentAllocation[]

LedgerEntry (Double-Entry Accounting)
├── account_id → Subscriber
├── invoice_id (nullable)
├── entry_type (debit, credit)
├── source (invoice, payment, adjustment, refund, credit_note)
├── amount
├── is_active
└── [basis for account balance calculation]

CreditNote
├── account_id → Subscriber
├── invoice_id (optional)
├── credit_number
├── status (draft, issued, partially_applied, applied, void)
├── total, applied_total
├── lines → CreditNoteLine[]
└── applications → CreditNoteApplication[]

PaymentArrangement (Payment Plan)
├── account_id → Subscriber
├── principal_amount, frequency, num_installments
├── status (active, completed, failed)
└── installments → PaymentArrangementInstallment[]
```

### Provisioning & Service Order Models

```
ServiceOrder (Workflow for New/Change/Disconnect)
├── subscriber_id → Subscriber
├── subscription_id → Subscription
├── status (draft, submitted, scheduled, provisioning, active, canceled, failed)
├── order_type (new_install, upgrade, downgrade, disconnect)
├── notes
│
├── appointments → InstallAppointment[]
├── tasks → ProvisioningTask[]
├── provisioning_runs → ProvisioningRun[]
│   └── steps → ProvisioningStep[]
└── state_transitions → ServiceStateTransition[]

ProvisioningWorkflow
├── name, vendor (mikrotik, huawei, zte, nokia, genieacs)
├── description, template (Jinja2 config generation)
├── is_active
└── [defines provisioning logic for vendor/service]
```

### Network Models (OLT, ONT, CPE, Fiber Plant)

```
OLTDevice (Optical Line Terminal)
├── name, code, location
├── vendor (HUAWEI, ZTE, Nokia, etc)
├── model, serial_number, ip_address, mac_address
├── status (active, maintenance, offline, retired)
│
├── shelves → OltShelf[]
│   └── cards → OltCard[]
│       └── ports → OltCardPort[]
│           └── pon_ports → PonPort[]
│               └── ont_assignments → OntAssignment[]
│
├── power_units → OltPowerUnit[]
└── sfp_modules → OltSfpModule[]

CPEDevice (Customer Premises Equipment)
├── subscriber_id → Subscriber
├── subscription_id → Subscription
├── service_address_id → Address
├── device_type (ont, router, modem, cpe)
├── status (active, inactive, retired)
├── serial_number, model, vendor, mac_address
├── installed_at
│
├── ports → Port[]
│   └── vlans → PortVlan[]
└── ip_assignments

OntAssignment (Subscriber's ONT on Fiber Plant)
├── subscriber_id → Subscriber
├── ont_unit_id → OntUnit
├── pon_port_id → PonPort
├── serial_number, olt_id
├── status (active, inactive, retired)
└── installed_at

Fiber Plant Infrastructure
├── FiberSegment (feeder, distribution, drop)
├── FiberStrand (individual strands)
├── FiberSpliceClosure, FiberSplice
├── FiberTerminationPoint
├── FdhCabinet, Splitter
│
├── IP Infrastructure
│   ├── IpPool, IpBlock
│   ├── IPv4Address, IPv6Address
│   └── IPAssignment
│
└── VLAN Management
    ├── Vlan
    └── PortVlan
```

### RADIUS & Authentication Models

```
RadiusServer
├── name, host, auth_port (1812), acct_port (1813)
├── description, is_active
└── clients → RadiusClient[]

RadiusClient (NAS/Access Point)
├── server_id → RadiusServer
├── nas_device_id → NasDevice
├── client_ip, shared_secret_hash
├── description, is_active

RadiusProfile (Service Profile)
├── name, description
├── bandwidth_limit_down, bandwidth_limit_up
├── session_timeout, idle_timeout
├── attributes → RadiusAttribute[]

RadiusUser (RADIUS account record)
├── username (unique)
├── password_hash, is_active
├── subscription_id → Subscription
├── profile_id → RadiusProfile
├── attributes (JSON)

AccessCredential (Subscriber's network credentials)
├── subscriber_id → Subscriber
├── subscription_id → Subscription
├── connection_type (pppoe, dhcp, ipoe, static, hotspot)
├── username, password (encrypted)
├── nas_username, ip_address, vlan_id
```

### User Authentication Models

```
UserCredential (Login credentials)
├── subscriber_id | system_user_id | reseller_user_id (legacy R1 login authority)
├── provider (legacy persisted enum; sso is compatibility-only/unimplemented)
├── username, password_hash
├── radius_server_id (when provider='radius')
├── party_id → Person Party (nullable additive projection; migration 525)
├── authentication_binding_id → installed verifier binding
├── tenant_id → operator tenant
├── party_bound_at, party_binding_source, party_binding_reason
├── must_change_password
├── failed_login_attempts, locked_until
├── last_login_at, is_active

AuthenticationBinding (installed verifier configuration)
├── binding_key + mechanism_code (immutable deployment-global identity)
├── mechanism_code vocabulary is open and owner-declared: local, radius
├── name (operator-facing label), is_active
└── no credential material and no radius_server_id coupling

Staff Party credential adoption (R1 operator adapter)
├── exact UUID-only plan + SHA-256 decision evidence
├── digest-bound, attributable, maximum-24-hour approval
├── party.staff_principal_adoption → party.registry for existing SystemUser binding
├── typed expected SystemUser precondition checked under the credential lock
├── party.credential_authentication_projection for credential projection
└── no identity inference, script-owned mutation/commit, or auth reader cutover

MFAMethod (Multi-Factor Authentication)
├── subscriber_id → Subscriber
├── method_type (totp, sms, email)
├── is_primary, is_active

Session (Portal session)
├── subscriber_id → Subscriber
├── status (active, revoked, expired)
├── user_agent, ip_address
├── created_at, expires_at

ApiKey (API authentication)
├── subscriber_id → Subscriber
├── key_hash (SHA256)
├── name, description
├── last_used_at, is_active
```

### Event Store & Audit Models

```
EventStore (Event sourcing)
├── event_id (UUID)
├── event_type (string)
├── payload (JSON)
├── status (processing, succeeded, failed)
├── actor
├── subscriber_id, account_id, subscription_id, invoice_id, service_order_id
├── failed_handlers (JSON)
└── error

AuditEvent (Operation-level audit log)
├── actor_type, actor_id
├── resource_type, resource_id
├── operation (create, update, delete, read)
├── method, path, status_code
├── request_body, response_status
└── timestamp
```

### Collections/Dunning Models

```
DunningCase (Overdue account workflow)
├── account_id → Subscriber
├── started_at, resolved_at
├── status (open, paused, resolved, closed)
├── policy_set_id → PolicySet
└── current_step

DunningActionLog
├── case_id → DunningCase
├── invoice_id → Invoice
├── action (notify, throttle, suspend, reject)
├── outcome, executed_at
└── access_consequence_id → FinancialAccessConsequence (access actions only)

FinancialAccessConsequence (confirmed financial access decision)
├── account_id → Subscriber
├── action, requested_reason, origin, outcome
├── preview_fingerprint, idempotency_key
├── separated decision_inputs and exact result
└── evidence[] → exact EnforcementLock / AccessCredential / DunningCase changes
```

---

## 3. Service Layer Patterns & Business Logic Flows

### Service Layer Architecture Pattern

Business behavior is exposed through registered typed command and query
owners. Adapters map transport input/output; they do not own transactions or
raise transport errors inside the domain:

```python
def get_some(db: Session, query: GetSome) -> SomeView:
    obj = db.get(Some, query.some_id)
    if obj is None:
        raise SomeNotFoundError(query.some_id)
    return SomeView.from_record(obj)


_UPDATE_SOME = OwnerCommandDefinition(
    owner="example.some_records",
    concern="some record updates",
    name="update_some",
)


def update_some(
    db: Session,
    command: UpdateSome,
    *,
    context: CommandContext,
) -> SomeUpdated:
    return execute_owner_command(
        db,
        definition=_UPDATE_SOME,
        context=context,
        operation=lambda: _update_some(db, command),
    )


def _update_some(db: Session, command: UpdateSome) -> SomeUpdated:
    obj = db.get(Some, command.some_id, with_for_update=True)
    if obj is None:
        raise SomeNotFoundError(command.some_id)
    obj.display_name = command.display_name
    db.flush()
    # Stage audit evidence and a versioned event in this transaction.
    return SomeUpdated(some_id=obj.id)
```

**Key Features**:
- typed commands, queries, outcomes, and domain errors;
- explicit field assignment instead of free-form mass assignment;
- one manifest-verified `execute_owner_command` boundary for transaction, lock,
  idempotency, audit, and event ownership;
- authoritative settings resolution through the registered control-plane owner;
- adapter-only mapping for HTTP, form, task retry, CLI, and provider semantics.

### Critical Business Logic Flows

#### Subscriber Lifecycle

```
Subscriber Creation
├── Validation (email unique, address format)
├── Assign subscriber_number via numbering service
├── Set initial status (active)
├── Event: subscriber.created
└── Return subscriber record

Subscriber Update
├── Validate changes
├── Update fields (name, address, contact, billing)
├── Event: subscriber.updated
└── Optional: subscriber.suspended (status change)
    ├── → Enforcement: suspend all active subscriptions
    ├── → Enforcement: throttle network access
    └── → Event: subscriber.suspended
```

#### Billing Workflow

```
Invoice Creation
├── Validate account exists
├── Auto-generate invoice_number if enabled
├── Validate totals (subtotal + tax = total)
├── Default currency/status from domain settings
├── Create InvoiceLine items (with tax rate lookup)
├── Create Invoice record
├── Event: invoice.created
└── Return invoice

Billing Run (Automated)
├── Query all active subscriptions
├── For each subscription:
│   ├── Check billing_period matches today
│   ├── Calculate charges (base + usage overages)
│   ├── Create Invoice
│   └── Create InvoiceLines (recurring + usage-based)
├── Event: invoice.created (for each)
├── Update subscription.last_billed_at
└── BillingRun.status = success|failed

Payment Processing
├── Create Payment record with status=pending
├── Route to payment provider (Stripe, PayPal, Manual)
├── On success:
│   ├── Payment.status = succeeded
│   ├── Create LedgerEntry (credit)
│   ├── Allocate to invoices via PaymentAllocation
│   ├── Update Invoice.balance_due
│   ├── Event: payment.received
│   └── On invoice paid: Event: invoice.paid
└── On failure:
    ├── Payment.status = failed
    └── Event: payment.failed

Dunning Collection Workflow
├── Detect overdue invoice (now > due_at)
├── Create DunningCase
├── Get PolicySet.dunning_steps
├── For each step (day_offset):
│   ├── Wait until day_offset elapsed
│   ├── Preview action from canonical receivable/profile/shield/health facts
│   ├── Lock + recompute + confirm with idempotency
│   ├── Link exact access consequence evidence in DunningActionLog
│   └── Event: dunning.action_executed
├── When payment received:
│   ├── Ask the financial access owner to reconcile
│   ├── Resolve only eligible overdue/prepaid locks, cases, and throttle
│   └── Event: dunning.resolved
```

#### Subscription Lifecycle

```
Subscription Activation
├── Create Subscription (status=pending)
├── Create ServiceOrder (order_type=new_install)
├── Create InstallAppointment (status=proposed)
├── Event: subscription.created
│
└── On provisioning complete:
    ├── Subscription.status = active
    ├── Subscription.active_from = now
    ├── ServiceOrder.status = active
    └── Event: subscription.activated

Subscription Upgrade/Downgrade
├── Create SubscriptionChangeRequest
├── Validate new offer available
├── Determine proration (immediate vs next cycle)
├── Calculate credits/charges
├── Event: subscription.upgraded/downgraded

Subscription Suspension
├── Set subscription.status = suspended
├── Event: subscription.suspended
└── Enforcement Actions:
    ├── Throttle: apply QoS limits via RADIUS CoA
    └── Full Suspend: block traffic, disconnect sessions

Subscription Cancellation
├── Set subscription.status = canceled
├── Calculate refund (if applicable)
├── Release network resources (IP, ONT)
├── Event: subscription.canceled
```

---

## 4. API & Web Route Organization

### API Route Organization

**Pattern**: All API routes available at both `/path` and `/api/v1/path`

**Key API Modules**:

```
app/api/

Subscriber Management
├── /organizations
├── /resellers
├── /subscribers
└── /subscribers/{id}/custom-fields

Catalog & Subscriptions
├── /offers
├── /add-ons
├── /subscriptions
├── /subscription-engines
└── /policy-sets

Billing
├── /invoices
├── /payments
├── /credit-notes
├── /ledger-entries
├── /billing-runs
├── /payment-arrangements
└── /tax-rates

Provisioning
├── /service-orders
├── /provisioning-workflows
├── /provisioning-runs
├── /install-appointments
└── /provisioning-tasks

Network
├── /cpe-devices
├── /olt-devices
├── /ont-assignments
├── /ip-pools
├── /ip-assignments
├── /vlans
├── /fiber-segments
└── /nas-devices

RADIUS
├── /radius-servers
├── /radius-clients
├── /radius-users
├── /radius-profiles
└── /radius-sync-jobs

Authentication
├── /users
├── /mfa-methods
├── /api-keys
└── /sessions

Utilities
├── /search
├── /settings
├── /domain-settings
├── /rbac
└── /audit-events
```

### Web Routes Organization

```
app/web/

Admin Portal (/admin/*)
├── /admin/ → dashboard
├── /admin/subscribers
├── /admin/catalog
├── /admin/billing
├── /admin/network
├── /admin/usage
├── /admin/reports
├── /admin/notifications
├── /admin/integrations
└── /admin/system

Customer Portal (/portal/*)
├── /portal/ → dashboard
├── /portal/subscriptions
├── /portal/invoices
├── /portal/payments
├── /portal/usage
└── /portal/account

Reseller Portal (/reseller/*)
├── /reseller/ → dashboard
├── /reseller/customers
├── /reseller/orders
└── /reseller/billing

Authentication
├── /auth/login
├── /auth/logout
├── /auth/register
├── /auth/forgot-password
├── /auth/reset-password
├── /auth/mfa-setup
└── /auth/mfa-verify
```

---

## 5. Authentication Flows for Different Portals

### Admin Portal Authentication

```
Admin User Flow
├── UserCredential with provider = "local"|"radius"|"sso"
│
├── Login (/auth/login):
│   ├── Validate credentials
│   ├── Check brute force protection
│   ├── On success:
│   │   ├── Create Session record
│   │   ├── Set session cookie
│   │   ├── Generate JWT token (API)
│   │   └── Redirect to dashboard
│   └── On failure: increment failed_login_attempts
│
├── MFA (if enabled):
│   ├── TOTP: 6-digit code from authenticator
│   ├── SMS: OTP sent to phone
│   └── Email: OTP sent to email
│
├── Session Management:
│   ├── require_user_auth: check session/JWT/API key
│   ├── require_role("admin"): check SubscriberRole
│   └── require_permission(): check RolePermission
│
└── CSRF Protection: double-submit cookie pattern
```

### API Authentication

```
JWT Token Flow
├── POST /api/v1/auth/login {username, password}
├── Validate credentials + MFA
├── Return: {access_token, token_type, expires_in}
└── Use: Authorization: Bearer <token>

API Key Flow
├── Create via /api/v1/api-keys
├── Use: Authorization: Bearer sk_live_xxx
├── Rate limited: 5 requests/60 seconds
```

---

## 6. Billing & Payment Flows

### Invoice Lifecycle

```
1. Creation → Draft/Issued
2. Issued → Partially Paid (confirmed payment/credit settlement)
3. Partially Paid → Paid (confirmed full settlement)
4. Issued → Overdue (collections resolver after `due_at`)
5. Draft/Issued/Overdue → Void (owner preview/confirmation; no effective settlement)
6. Issued/Partially Paid/Overdue → Written-off (owner preview/confirmation of remaining collectible debt)

Paid, void, and written-off state cannot be set through generic invoice CRUD.
`financial.payments`, `financial.credit_notes`, and `financial.invoices` own the
corresponding transition and exact evidence contract.
```

### Payment Processing

```
Payment Creation
├── Create Payment (status=pending)
├── Route to provider (Stripe/PayPal/Manual)
├── On success:
│   ├── Create LedgerEntry (credit)
│   ├── Allocate to invoices
│   ├── Update invoice.balance_due
│   └── Event: payment.received
└── On failure:
    └── Event: payment.failed

Refund
├── Call provider API (stripe.Refund.create)
├── Create LedgerEntry (debit)
└── Event: payment.refunded
```

### Automated Billing Run

```
Triggered: celery task (daily/monthly)

For each active subscription:
├── Calculate charges (base + usage overages)
├── Create Invoice + InvoiceLines
├── Update subscription.last_billed_at
└── Event: invoice.created
```

### Dunning Process

```
Detect Overdue → Create DunningCase

For each PolicySet.dunning_step:
├── day_offset 7: notify
├── day_offset 14: throttle
├── day_offset 21: suspend
├── day_offset 30: reject
└── Confirm owner preview and link exact consequence in DunningActionLog

On Payment: request reconciliation; restoration occurs only if every named
financial gate for the affected lock reason passes.
```

---

## 7. Network Provisioning Flows

### CPE/ONT Assignment

```
1. Service Order Creation
   ├── Select service address
   ├── Select available ONT from OLT
   └── Determine IP allocation method

2. IP Pool Management
   ├── Static: Admin selects IP from pool
   └── Dynamic: RADIUS handles DHCP

3. ONT Assignment
   ├── Create OntAssignment (PON port → ONT)
   └── Create CPEDevice record

4. RADIUS User Setup
   ├── Create AccessCredential (PPPoE/IPoE)
   └── Create RadiusUser with profile

5. NAS Provisioning
   ├── Render Jinja2 template
   ├── Push config via SSH/API
   └── Verify connection
```

### RADIUS Integration

```
Access-Request (Authentication)
├── NAS sends RADIUS request
├── Server validates user/password
├── Returns Access-Accept with attributes
│   ├── Framed-IP-Address
│   ├── Bandwidth limits
│   └── VLAN, firewall rules

Accounting-Request (Usage)
├── NAS sends Start/Interim/Stop
├── Server logs octets, duration
└── dotmac_sub creates UsageRecord

CoA (Change of Authorization)
├── Update limits in real-time
├── No re-authentication needed
└── Used for throttling

Disconnect-Request
├── Terminate active session
└── Used for suspension
```

### Enforcement Actions

```
Throttle
├── RADIUS CoA with lower speed profile
├── OR MikroTik API: modify queue limits

Suspend
├── RADIUS Disconnect-Request
├── MikroTik: kill sessions, add to block list
├── Set RadiusUser.is_active = false

Reactivate
├── Restore RADIUS profile
├── Remove from block list
└── Set RadiusUser.is_active = true
```

---

## 8. Background Task Processing (Celery)

### Configuration

```python
# app/celery_app.py
celery_app = Celery("dotmac_sm")
# Broker: redis://localhost:6379/0
# Backend: redis://localhost:6379/1
```

### Critical Tasks

**Billing Tasks**
- `run_monthly_billing` - Generate invoices (1st of month)
- `process_scheduled_payments` - Process pending payments (daily)
- `run_billing_enforcement` - Invoke the permanent postpaid collections owner;
  current financial-access behavior is documented only in
  `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`
- `apply_usage_charges` - Rate usage at period end

**Usage & Network Tasks**
- `record_usage_from_radius` - Create UsageRecord from accounting
- `rate_usage` - Calculate overage charges
- `monitor_links` - SNMP poll network devices (5 min)
- `sync_radius_users` - Sync to RADIUS servers (daily)

**Notification & Webhook Tasks**
- `send_notifications` - Deliver email/SMS/push
- `deliver_webhook` - POST to webhook endpoints (retry on failure)

**Integration Tasks**
- `sync_fiber_plant` - GIS sync (weekly)

### Task Pattern

```python
@celery_app.task(name="app.tasks.module.task_name")
def task_name(arg1, arg2):
    db = SessionLocal()
    try:
        command = OwningCommand(arg1=arg1, arg2=arg2)
        outcome = owning_service.execute(db, command)
        return outcome.to_task_result()
    finally:
        db.close()
```

The task is a delivery adapter. The registered owner completes or rolls back
the business transaction before returning. The task maps only retry/reject and
serialization behavior.

---

## 9. Key Integrations & External Dependencies

### Payment Providers

**Stripe**
- Charge creation via `stripe.Charge.create()`
- Webhook handling for async events
- Refunds via `stripe.Refund.create()`

**PayPal**
- OAuth flow for authorization
- Webhook callbacks for completion

### RADIUS Server

- FreeRADIUS with SQL backend
- Access-Request/Accept for authentication
- Accounting-Request for usage tracking
- CoA/Disconnect for real-time enforcement

### Network Device APIs

**MikroTik RouterOS**
- SSH command execution
- REST API for queue/firewall management
- SNMP for monitoring

**Huawei OLT**
- SSH for ONT activation
- TR-069 via GenieACS for CPE management

### Notification Channels

- **Email**: SMTP with Jinja2 templates
- **SMS**: Twilio API
- **Push**: FCM/APNs

### Webhook Delivery

- HMAC-SHA256 signed payloads
- Exponential backoff retry (1m, 5m, 30m, 4h)
- Delivery logging

---

## 10. Summary & Key Architectural Patterns

### Core Patterns

| Pattern | Description |
|---------|-------------|
| Command/query owner | Typed intent and results with one named decision and transaction owner |
| Event-Driven | ~40 event types with handlers for webhooks, notifications, enforcement |
| Multi-Tenant | Domain-based tenancy with DomainSetting configuration |
| RBAC | Role/Permission checks via decorators |
| Double-Entry Ledger | LedgerEntry for account balance tracking |

### Key Lifecycles

| Entity | States |
|--------|--------|
| Subscriber | Created → Active → Suspended/Canceled → Archived |
| Subscription | Pending → Active → Suspended/Canceled → Expired |
| Invoice | Draft → Issued/Overdue → Partially Paid/Paid, or owner-confirmed Void/Written-off |
| ServiceOrder | Draft → Submitted → Scheduled → Provisioning → Active |

### Database Conventions

- UUID primary keys on all models
- `created_at`, `updated_at` timestamps
- Soft delete via `is_active` flag
- PostgreSQL enum types
- Foreign keys with cascade relationships

### Security Measures

- CSRF: Double-submit cookie
- Brute Force: Failed login tracking with lockout
- Passwords: PBKDF2/bcrypt hashing
- API Keys: SHA256 hashed, rate limited
- JWT: Signed tokens with expiration
- PCI: Stripe/PayPal tokenization (no raw card storage)
- Audit: Middleware captures all API calls

### Performance

- Settings cache with TTL
- Redis for session tokens
- Connection pooling (SQLAlchemy)
- Background processing (Celery)
- HTMX for dynamic UI updates
- Composite database indexes

---

## 11. Recent Additions (Since Initial Documentation)

### New Modules

| Module | Models | Services | Web Routes | Status |
|--------|--------|----------|------------|--------|
| **Support Tickets** | `support.py` | `support.py` | `support_tickets.py` | In development |
| **ONT Provisioning Profiles** | via `provisioning.py` | `network/ont_provisioning_profiles.py`, `ont_profile_apply.py` | `network_ont_provisioning_profiles.py` | In development |
| **Vendor Capabilities** | via `network.py` | `network/vendor_capabilities.py` | `network_vendor_capabilities.py` | In development |
| **ONU Types** | via `network.py` | existing | `network_onu_types.py` | Committed |
| **Speed Profiles** | via `catalog.py` | existing | `network_speed_profiles.py` | Committed |
| **DNS Threat Monitoring** | — | — | `network_dns_threats.py` | Committed |
| **Network Weathermap** | — | — | `network_weathermap.py` | Committed |
| **Speed Tests** | — | — | `network_speedtests.py` | Committed |
| **POP/Network Sites** | — | — | `network_pop_sites.py` | Committed |
| **Site Survey** | — | — | `network_site_survey.py` | Committed |

### Admin Web Route Growth

The admin portal has grown from ~10 route files to **53 route files**, reflecting the decomposition of monolithic modules:

- **Billing** decomposed into: `billing_accounts`, `billing_arrangements`, `billing_channels`, `billing_collection_accounts`, `billing_credits`, `billing_dunning`, `billing_invoice_actions`, `billing_invoice_batch`, `billing_invoice_bulk`, `billing_invoices`, `billing_payments`, `billing_providers`, `billing_reporting`
- **Network** decomposed into: `network`, `network_core_devices`, `network_cpes`, `network_dns_threats`, `network_fiber_plant`, `network_fiber_splice`, `network_ip_management`, `network_monitoring`, `network_olts_onts`, `network_onu_types`, `network_ont_provisioning_profiles`, `network_pop_sites`, `network_radius`, `network_site_survey`, `network_speed_profiles`, `network_speedtests`, `network_tr069`, `network_vendor_capabilities`, `network_weathermap`, `network_zones`

### Network Service Decomposition

`app/services/network/` now has **20 files** including:
- `olt.py`, `olt_polling.py` — OLT management and polling
- `ont_actions.py`, `ont_tr069.py` — ONT operations
- `ont_provisioning_profiles.py`, `ont_profile_apply.py` — Profile-based provisioning
- `vendor_capabilities.py` — Per-vendor/model feature registry
- `_resolve.py` — Network entity resolution

### Customer Portal Expansion

The customer portal (`/portal/*`) now includes:
- Service detail with subscription management
- Billing with payment arrangements
- Profile management
- Support ticket submission (in development)

### Reseller Portal

Enhanced with detail views, forms, and management capabilities.

---

*Generated: 2026-01-27 | Updated: 2026-03-14*
