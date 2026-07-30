import argparse

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.rbac import (
    Permission,
    Role,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import rbac_catalog, subscriber_assignments, system_user_assignments

ADMIN_ONLY_PERMISSION_KEYS = {
    "*",
    "auth:manage",
    "auth:credential:read",
    "auth:credential:write",
    "network:admin",
    "reseller:impersonate",
    "system:db_admin",
    "system:read",
    "system:write",
    "billing:read",
    "billing:write",
    "catalog:read",
    "catalog:write",
    "network:read",
    "network:operation:redrive",
    "provisioning:service_change_reconcile",
    "network:write",
    "rbac:assign",
    "rbac:permissions:delete",
    "rbac:permissions:read",
    "rbac:permissions:write",
    "rbac:roles:delete",
    "rbac:roles:read",
    "rbac:roles:write",
    "subscriber:impersonate",
    "subscriber:read",
    "subscriber:write",
    "system:settings:read",
    "system:settings:write",
}

DEFAULT_PERMISSIONS = [
    ("*", "Full access (wildcard) — grants every permission"),
    # Audit
    ("audit:read", "Read audit events"),
    # Integrations (service ApiKey scopes)
    ("integration:crm", "CRM service integration API access"),
    # Auth & System
    ("auth:manage", "Manage authentication settings"),
    ("auth:credential:read", "View authentication credential metadata"),
    ("auth:credential:write", "Manage authentication credentials"),
    ("system:read", "View system administration resources"),
    ("system:write", "Manage system administration resources"),
    ("notification:read", "View notification templates, queue, and history"),
    ("notification:write", "Manage notification templates and delivery"),
    ("system:db_admin", "Perform restricted database administration"),
    ("system:settings:read", "View system settings"),
    ("system:settings:write", "Modify system settings"),
    # Secret management (OpenBao) — admin-only; intentionally NOT granted to any
    # non-admin seeded role, so only wildcard (admin) principals can read/write.
    ("system:secrets:read", "View secret paths"),
    ("system:secrets:write", "Create/modify/delete secrets"),
    # RBAC - Granular permissions for role builder
    ("rbac:roles:read", "View roles"),
    ("rbac:roles:write", "Create and update roles"),
    ("rbac:roles:delete", "Delete roles"),
    ("rbac:permissions:read", "View permissions"),
    ("rbac:permissions:write", "Create and update permissions"),
    ("rbac:permissions:delete", "Delete permissions"),
    ("rbac:assign", "Assign roles to users"),
    # Customers/Subscribers
    ("customer:read", "View customers and subscribers"),
    ("customer:create", "Create customers and subscribers"),
    ("customer:update", "Update customers and subscribers"),
    ("customer:delete", "Delete customers and subscribers"),
    ("customer:impersonate", "Impersonate customer accounts"),
    ("customer:write", "Manage customers through compatibility endpoints"),
    ("reseller:impersonate", "Impersonate reseller portal principals"),
    ("reseller:read", "View resellers"),
    ("reseller:write", "Manage resellers"),
    # Billing - Invoices
    ("billing:invoice:read", "View invoices"),
    ("billing:invoice:create", "Create invoices"),
    ("billing:invoice:update", "Update invoices"),
    ("billing:invoice:delete", "Delete/void invoices"),
    # Billing - Payments
    ("billing:payment:read", "View payments"),
    ("billing:payment:create", "Record payments"),
    ("billing:payment:update", "Update payments"),
    ("billing:payment:delete", "Delete/refund payments"),
    # Billing - Credit Notes
    ("billing:extension:read", "View service extensions"),
    ("billing:extension:create", "Create service extensions"),
    ("billing:extension:apply", "Apply or cancel service extensions"),
    ("billing:treatment:read", "View subscription billing treatments"),
    ("billing:treatment:write", "Approve and revoke billing treatments"),
    ("billing:credit_note:read", "View credit notes"),
    ("billing:credit_note:create", "Create credit notes"),
    ("billing:credit_note:update", "Update credit notes"),
    ("billing:credit_note:delete", "Delete credit notes"),
    # Billing - Accounts & Ledger
    ("billing:account:read", "View billing accounts"),
    ("billing:account:write", "Manage billing accounts"),
    ("billing:ledger:read", "View ledger entries"),
    ("billing:ledger:write", "Manage ledger entries"),
    ("billing:tax:read", "View tax rates"),
    ("billing:tax:write", "Manage tax rates"),
    # Billing - Dunning & Collections
    ("billing:dunning:read", "View dunning cases and collection status"),
    ("billing:dunning:write", "Manage dunning cases (pause, resume, close)"),
    # Billing - Payment Providers & Channels
    ("billing:provider:read", "View payment provider configuration"),
    ("billing:provider:write", "Manage payment provider configuration"),
    ("billing:channel:read", "View payment channel configuration"),
    ("billing:channel:write", "Manage payment channel configuration"),
    # Billing - Arrangements
    ("billing:arrangement:read", "View payment arrangements"),
    ("billing:arrangement:write", "Manage payment arrangements"),
    # Billing - Batch & Import
    ("billing:batch:read", "View invoice batch history"),
    ("billing:batch:write", "Generate and manage invoice batches"),
    ("billing:import:write", "Import payment data"),
    ("billing:proof:read", "View submitted payment proofs"),
    ("billing:proof:verify", "Verify or reject submitted payment proofs"),
    # Catalog
    ("catalog:product:read", "View catalog products"),
    ("catalog:product:write", "Manage catalog products"),
    ("catalog:offer:read", "View catalog offers"),
    ("catalog:offer:write", "Manage catalog offers"),
    # Subscriptions
    ("subscription:read", "View subscriptions"),
    ("subscription:create", "Create subscriptions"),
    ("subscription:update", "Update subscriptions"),
    ("subscription:activate", "Activate suspended subscriptions"),
    ("subscription:suspend", "Suspend subscriptions"),
    ("subscription:cancel", "Cancel subscriptions"),
    (
        "provisioning:service_change_reconcile",
        "Review and repair interrupted service-change execution chains",
    ),
    # Network - Devices
    ("network:hub:read", "View the network operations hub"),
    ("network:admin", "Perform restricted network administration"),
    ("network:map:read", "View the comprehensive network map"),
    ("network:device:read", "View network devices"),
    ("network:device:write", "Manage network devices"),
    (
        "network:operation:redrive",
        "Retry eligible failed network operations",
    ),
    ("network:olt:read", "View OLT devices and operations"),
    ("network:olt:write", "Manage OLT devices and operations"),
    (
        "network:ont:commission",
        "Commission unassigned ONTs for management access",
    ),
    ("network:cpe:read", "View CPE devices"),
    ("network:cpe:write", "Manage CPE devices"),
    ("network:nas:read", "View NAS device management"),
    ("network:nas:write", "Manage NAS devices"),
    ("network:ont:read", "View ONT units"),
    ("network:ont:write", "Manage ONT units"),
    ("network:tr069:read", "View TR-069 / ACS management"),
    ("network:tr069:write", "Manage TR-069 / ACS resources"),
    ("network:speedtest:read", "View network speed tests"),
    ("network:speedtest:write", "Manage network speed tests"),
    ("network:speed_profile:read", "View network speed profiles"),
    ("network:speed_profile:write", "Manage network speed profiles"),
    ("network:dns_threat:read", "View DNS threat monitoring"),
    ("network:dns_threat:write", "Manage DNS threat monitoring"),
    ("network:onu_type:read", "View ONU type catalog"),
    ("network:onu_type:write", "Manage ONU type catalog"),
    ("network:pop:read", "View network POP sites"),
    ("network:pop:write", "Manage network POP sites"),
    ("network:zone:read", "View network zones"),
    ("network:zone:write", "Manage network zones"),
    ("network:pon:read", "View PON interfaces"),
    ("network:pon:write", "Manage PON interfaces"),
    ("network:weathermap:read", "View network weathermap"),
    ("network:weathermap:write", "Manage network weathermap"),
    ("network:vendor_capability:read", "View network vendor capabilities"),
    ("network:vendor_capability:write", "Manage network vendor capabilities"),
    ("network:authorization:read", "View network authorization presets"),
    ("network:authorization:write", "Manage network authorization presets"),
    ("network:vpn:read", "View VPN infrastructure and tunnels"),
    ("network:vpn:write", "Manage VPN infrastructure and tunnels"),
    # Network - IP Management
    ("network:ip:read", "View IP pools and assignments"),
    ("network:ip:write", "Manage IP pools and assignments"),
    # Network - Fiber
    ("network:fiber:read", "View fiber infrastructure"),
    ("network:fiber:write", "Manage fiber infrastructure"),
    # Network - RADIUS
    ("network:radius:read", "View RADIUS configuration"),
    ("network:radius:write", "Manage RADIUS configuration"),
    ("monitoring:read", "View monitoring dashboards and alerts"),
    ("monitoring:write", "Manage monitoring rules and alert states"),
    ("usage:read", "View subscriber usage and metering records"),
    ("usage:write", "Manage usage and metering records"),
    # Operations - Work Orders
    ("operations:work_order:read", "View work orders"),
    ("operations:work_order:create", "Create work orders"),
    ("operations:work_order:update", "Update work orders"),
    ("operations:work_order:delete", "Delete work orders"),
    ("operations:work_order:dispatch", "Dispatch work orders"),
    # Operations - Service Orders
    ("operations:service_order:read", "View service orders"),
    ("operations:service_order:create", "Create service orders"),
    ("operations:service_order:update", "Update service orders"),
    # Operations - Technicians
    ("operations:technician:read", "View technicians"),
    ("operations:technician:write", "Manage technicians"),
    ("operations:service_team:read", "View service teams and memberships"),
    ("operations:service_team:create", "Create service teams"),
    ("operations:service_team:update", "Update service-team identity"),
    ("operations:service_team:membership", "Manage service-team membership"),
    ("operations:service_team:retire", "Activate or deactivate service teams"),
    # Operations - Field Expense Requests
    ("operations:expense_request:read", "View field expense requests"),
    ("operations:expense_request:write", "Approve or reject field expense requests"),
    ("operations:asset_custody:read", "View asset custody records"),
    ("operations:asset_custody:write", "Manage asset custody records"),
    ("operations:dispatch:read", "View dispatch work orders and maps"),
    ("operations:dispatch:write", "Create and edit dispatch work orders"),
    ("operations:dispatch:assign", "Queue and assign dispatch work orders"),
    ("operations:material_request:read", "View material requests"),
    ("operations:material_request:write", "Manage material requests"),
    # Support - Tickets
    ("support:ticket:read", "View tickets"),
    ("support:ticket:create", "Create tickets"),
    ("support:ticket:update", "Update tickets"),
    ("support:ticket:delete", "Delete tickets"),
    ("support:ticket:assign", "Assign tickets"),
    ("support:automation:read", "View ticket automation rules"),
    ("support:automation:write", "Manage ticket automation rules"),
    # CRM
    ("crm:contact:read", "View CRM contacts"),
    ("crm:contact:write", "Manage CRM contacts"),
    ("crm:conversation:read", "View conversations"),
    ("crm:conversation:write", "Manage conversations"),
    ("crm:lead:read", "View leads"),
    ("crm:lead:write", "Manage leads"),
    ("crm:lead:delete", "Delete leads"),
    # CRM - Sales vertical (Phase 3 §6 PR 12: quotes + sales orders, guarding
    # app/api/crm_sales.py + app/api/sales_orders.py). Like crm:lead:*, these
    # are deliberately granted to no seeded non-admin role — sub has no seeded
    # sales role, so they stay admin-implicit (wildcard) until one exists;
    # the keys are UI-assignable via the role builder.
    ("crm:quote:read", "View quotes"),
    ("crm:quote:write", "Manage quotes"),
    ("crm:sales_order:read", "View sales orders"),
    ("crm:sales_order:write", "Manage sales orders"),
    # Projects
    ("project:read", "View projects"),
    ("project:create", "Create projects"),
    ("project:update", "Update projects"),
    ("project:delete", "Delete projects"),
    ("project:task:read", "View project tasks"),
    ("project:task:write", "Manage project tasks"),
    # Vendors
    ("vendor:read", "View vendors"),
    ("vendor:write", "Manage vendors"),
    ("vendor:project:read", "View vendor projects"),
    ("vendor:project:write", "Manage vendor projects"),
    # Inventory
    ("inventory:read", "View inventory"),
    ("inventory:write", "Manage inventory"),
    ("finance:ap:read", "View accounts-payable resources"),
    ("finance:ap:write", "Manage accounts-payable resources"),
    # GIS / Mapping
    ("gis:map:view", "View maps and layers"),
    ("gis:location:write", "Create, edit, and delete map location markers"),
    ("gis:area:write", "Create, edit, and delete map coverage areas"),
    ("gis:layer:write", "Create, edit, and delete map layers"),
    (
        "gis:location_request:review",
        "Approve or reject customer location-change requests",
    ),
    ("gis:map:configure", "Configure map settings and layers"),
    ("gis:coverage:read", "View coverage areas"),
    ("gis:coverage:write", "Manage coverage areas"),
    ("gis:fiber:view", "View fiber routes on map"),
    ("gis:fiber:edit", "Edit fiber routes on map"),
    ("gis:serviceability:check", "Run address serviceability checks"),
    ("gis:export", "Export GIS data (KML, GeoJSON)"),
    # Reports
    ("reports:billing:read", "View billing and revenue reports"),
    ("reports:billing:export", "Export billing and revenue report data"),
    ("reports:network:read", "View network and bandwidth reports"),
    ("reports:network:export", "Export network and bandwidth report data"),
    ("reports:operations", "View operations reports"),
    ("reports:subscribers", "View subscriber reports"),
    ("reports:support:read", "View support and inbox operations reports"),
    # Router Management
    ("router:read", "View routers, templates, push history, snapshots"),
    ("router:write", "Create/edit routers, templates, trigger sync/snapshots"),
    ("router:push_config", "Execute config pushes and rollbacks"),
    ("router:admin", "Manage jump hosts, delete routers"),
    # Consolidated billing accounts (reseller billing parents)
    ("billing_account:read", "View consolidated billing accounts and statements"),
    ("billing_account:write", "Create/edit consolidated billing accounts"),
    (
        "billing_account:distribute",
        "Record bulk payments and distribute across child accounts",
    ),
    # Legacy broad permissions (for backward compatibility)
    ("billing:read", "Read all billing data"),
    ("billing:write", "Manage all billing data"),
    ("catalog:read", "Read catalog data"),
    ("catalog:write", "Manage catalog data"),
    (
        "catalog:billing_write",
        "Manage billing-critical catalog pricing and cadence",
    ),
    ("network:read", "Read network inventory and telemetry"),
    ("network:write", "Manage network inventory and telemetry"),
    ("provisioning:read", "Read provisioning data"),
    ("provisioning:write", "Manage provisioning data"),
    ("subscriber:read", "Read subscribers and accounts"),
    ("subscriber:write", "Manage subscribers and accounts"),
    ("subscriber:impersonate", "Impersonate subscriber accounts"),
]

DEFAULT_ROLES = [
    ("admin", "Full system access"),
    ("auditor", "Audit read-only access"),
    ("operator", "Network and provisioning operations"),
    ("support", "Subscriber and billing support"),
    ("finance_manager", "Full billing and finance access"),
]

ROLE_PERMISSIONS = {
    "admin": [perm for perm, _ in DEFAULT_PERMISSIONS],
    "auditor": [
        "audit:read",
        "billing:invoice:read",
        "billing:payment:read",
        "billing:credit_note:read",
        "billing:account:read",
        "billing:ledger:read",
        "billing:ledger:write",
        "billing:dunning:read",
        "billing:provider:read",
        "billing:channel:read",
        "billing:arrangement:read",
        "billing:treatment:read",
        "billing:batch:read",
        "billing_account:read",
        "customer:read",
        "reseller:read",
        "reports:billing:read",
        "reports:billing:export",
        "reports:subscribers",
    ],
    "operator": [
        "network:hub:read",
        "network:map:read",
        "network:device:read",
        "network:device:write",
        "network:olt:read",
        "network:olt:write",
        "network:ont:commission",
        "network:cpe:read",
        "network:cpe:write",
        "network:nas:read",
        "network:nas:write",
        "network:ont:read",
        "network:ont:write",
        "network:tr069:read",
        "network:tr069:write",
        "network:speedtest:read",
        "network:speedtest:write",
        "network:speed_profile:read",
        "network:speed_profile:write",
        "network:dns_threat:read",
        "network:dns_threat:write",
        "network:onu_type:read",
        "network:onu_type:write",
        "network:pop:read",
        "network:pop:write",
        "network:zone:read",
        "network:zone:write",
        "network:pon:read",
        "network:pon:write",
        "network:weathermap:read",
        "network:weathermap:write",
        "network:vendor_capability:read",
        "network:vendor_capability:write",
        "network:authorization:read",
        "network:authorization:write",
        "network:vpn:read",
        "network:vpn:write",
        "network:ip:read",
        "network:ip:write",
        "network:fiber:read",
        "network:fiber:write",
        "network:radius:read",
        "network:radius:write",
        "monitoring:read",
        "monitoring:write",
        "provisioning:read",
        "provisioning:write",
        "customer:read",
        "reseller:read",
        "subscription:read",
        "operations:work_order:read",
        "operations:work_order:update",
        "operations:technician:read",
        "operations:service_team:read",
        "operations:service_team:create",
        "operations:service_team:update",
        "operations:service_team:membership",
        "operations:service_team:retire",
        "operations:expense_request:read",
        "operations:expense_request:write",
        "reports:network:read",
        "reports:network:export",
        "reports:support:read",
    ],
    "support": [
        "customer:read",
        "reseller:read",
        "customer:update",
        "billing:invoice:read",
        "billing:payment:read",
        "billing:payment:create",
        "billing:credit_note:read",
        "billing:account:read",
        "billing:dunning:read",
        "billing_account:read",
        "subscription:read",
        "subscription:activate",
        "subscription:suspend",
        "support:ticket:read",
        "operations:service_team:read",
        "support:ticket:create",
        "support:ticket:update",
        "support:automation:read",
        "support:automation:write",
        "crm:contact:read",
        "crm:conversation:read",
        "crm:conversation:write",
        "reports:subscribers",
        "reports:support:read",
    ],
    "finance_manager": [
        "billing:invoice:read",
        "billing:invoice:create",
        "billing:invoice:update",
        "billing:invoice:delete",
        "billing:payment:read",
        "billing:payment:create",
        "billing:payment:update",
        "billing:payment:delete",
        "billing:credit_note:read",
        "billing:credit_note:create",
        "billing:credit_note:update",
        "billing:credit_note:delete",
        "billing:account:read",
        "billing:account:write",
        "billing:ledger:read",
        "billing:tax:read",
        "billing:tax:write",
        "billing:dunning:read",
        "billing:dunning:write",
        "billing:provider:read",
        "billing:provider:write",
        "billing:channel:read",
        "billing:channel:write",
        "billing:arrangement:read",
        "billing:arrangement:write",
        "billing:treatment:read",
        "billing:treatment:write",
        "billing:batch:read",
        "billing:batch:write",
        "billing:import:write",
        "billing:proof:read",
        "billing:proof:verify",
        "billing_account:read",
        "billing_account:write",
        "billing_account:distribute",
        "reports:billing:read",
        "reports:billing:export",
        "customer:read",
        "reseller:read",
    ],
}


def parse_args():
    parser = argparse.ArgumentParser(description="Seed RBAC roles and permissions.")
    admin_target = parser.add_mutually_exclusive_group()
    admin_target.add_argument(
        "--admin-email", help="System user email to map to the admin role."
    )
    admin_target.add_argument(
        "--admin-subscriber-id",
        help="Legacy subscriber principal ID to map to the admin role.",
    )
    return parser.parse_args()


def _ensure_role(db, name, description):
    return rbac_catalog.ensure_role(db, name=name, description=description)


def _ensure_permission(db, key, description):
    return rbac_catalog.ensure_permission(
        db,
        key=key,
        description=description,
        is_ui_assignable=key not in ADMIN_ONLY_PERMISSION_KEYS,
    )


def _ensure_role_permission(db, role_id, permission_id):
    return rbac_catalog.ensure_role_permission(
        db,
        role_id=role_id,
        permission_id=permission_id,
    )


def _ensure_subscriber_role(db, subscriber_id, role_id):
    return subscriber_assignments.ensure_seeded_role_grant(
        db,
        subscriber_id=subscriber_id,
        role_id=role_id,
    )


def _ensure_system_user_role(db, system_user_id, role_id):
    return system_user_assignments.ensure_source_role_by_id(
        db,
        user_id=system_user_id,
        role_id=role_id,
        source=system_user_assignments.LOCAL_ROLE_SOURCE,
    )


def main():
    load_dotenv()
    args = parse_args()
    db = SessionLocal()
    try:
        for name, description in DEFAULT_ROLES:
            _ensure_role(db, name, description)
        for key, description in DEFAULT_PERMISSIONS:
            _ensure_permission(db, key, description)

        roles = {role.name: role for role in db.query(Role).all()}
        permissions = {perm.key: perm for perm in db.query(Permission).all()}
        for role_name, permission_keys in ROLE_PERMISSIONS.items():
            role = roles.get(role_name)
            if not role:
                continue
            desired_permission_ids = tuple(
                permissions[key].id for key in permission_keys if key in permissions
            )
            rbac_catalog.replace_seeded_role_permissions(
                db,
                role=role,
                permission_ids=desired_permission_ids,
            )
        db.commit()

        admin_role = roles.get("admin")
        if admin_role and (args.admin_email or args.admin_subscriber_id):
            if args.admin_email:
                system_user = (
                    db.query(SystemUser)
                    .filter(SystemUser.email == args.admin_email)
                    .first()
                )
                if not system_user:
                    raise SystemExit("Admin system user not found.")
                _ensure_system_user_role(db, system_user.id, admin_role.id)
            else:
                subscriber = db.get(Subscriber, args.admin_subscriber_id)
                if not subscriber:
                    raise SystemExit("Admin subscriber not found.")
                _ensure_subscriber_role(db, subscriber.id, admin_role.id)
            db.commit()
            print("Admin role assigned.")
        print("RBAC seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
