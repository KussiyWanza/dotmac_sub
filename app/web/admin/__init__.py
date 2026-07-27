"""Admin web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import module_manager as module_manager_service
from app.services import web_admin as web_admin_service
from app.web.admin.admin_hub import router as admin_hub_router
from app.web.admin.alerts import router as alerts_router
from app.web.admin.billing_accounts import router as billing_accounts_router
from app.web.admin.billing_arrangements import router as billing_arrangements_router
from app.web.admin.billing_channels import router as billing_channels_router
from app.web.admin.billing_collection_accounts import (
    router as billing_collection_accounts_router,
)
from app.web.admin.billing_consolidated import router as billing_consolidated_router
from app.web.admin.billing_credits import router as billing_credits_router
from app.web.admin.billing_dunning import router as billing_dunning_router
from app.web.admin.billing_extensions import router as billing_extensions_router
from app.web.admin.billing_invoice_actions import (
    router as billing_invoice_actions_router,
)
from app.web.admin.billing_invoice_batch import router as billing_invoice_batch_router
from app.web.admin.billing_invoice_bulk import router as billing_invoice_bulk_router
from app.web.admin.billing_invoices import router as billing_invoices_router
from app.web.admin.billing_payment_proofs import router as billing_payment_proofs_router
from app.web.admin.billing_payments import router as billing_payments_router
from app.web.admin.billing_reporting import router as billing_reporting_router
from app.web.admin.catalog import router as catalog_router
from app.web.admin.catalog_settings import legacy_add_ons_router
from app.web.admin.catalog_settings import router as catalog_settings_router
from app.web.admin.configuration import router as configuration_router
from app.web.admin.crm_referrals import router as crm_referrals_router
from app.web.admin.customers import contacts_router
from app.web.admin.customers import router as customers_router
from app.web.admin.dashboard import router as dashboard_router
from app.web.admin.design_system import router as design_system_router
from app.web.admin.dispatch_work_orders import router as dispatch_work_orders_router
from app.web.admin.drift import router as drift_router
from app.web.admin.field_maps import router as field_maps_router
from app.web.admin.gis import router as gis_router
from app.web.admin.inbox import router as inbox_router
from app.web.admin.integrations import router as integrations_router
from app.web.admin.legal import router as legal_router
from app.web.admin.nas import router as nas_router
from app.web.admin.network import router as network_router
from app.web.admin.network_authorization_presets import (
    router as network_authorization_presets_router,
)
from app.web.admin.network_core_devices import router as network_core_devices_router
from app.web.admin.network_cpes import router as network_cpes_router
from app.web.admin.network_device_groups import router as network_device_groups_router
from app.web.admin.network_dns_threats import router as network_dns_threats_router
from app.web.admin.network_fiber_plant import router as network_fiber_plant_router
from app.web.admin.network_fiber_splice import router as network_fiber_splice_router
from app.web.admin.network_firmware_catalog import (
    router as network_firmware_catalog_router,
)
from app.web.admin.network_ip_management import router as network_ip_management_router
from app.web.admin.network_monitoring import router as network_monitoring_router
from app.web.admin.network_olts_inventory import router as network_olts_inventory_router
from app.web.admin.network_olts_profiles import router as network_olts_profiles_router
from app.web.admin.network_onts import router as network_onts_router
from app.web.admin.network_onts_actions import router as network_onts_actions_router
from app.web.admin.network_onts_inventory import router as network_onts_inventory_router
from app.web.admin.network_onts_provisioning import (
    router as network_onts_provisioning_router,
)
from app.web.admin.network_onu_types import router as network_onu_types_router
from app.web.admin.network_operations import router as network_operations_router
from app.web.admin.network_pop_sites import router as network_pop_sites_router
from app.web.admin.network_radius import router as network_radius_router
from app.web.admin.network_speed_profiles import router as network_speed_profiles_router
from app.web.admin.network_speedtests import router as network_speedtests_router
from app.web.admin.network_tr069 import router as network_tr069_router
from app.web.admin.network_tr069_presets import router as network_tr069_presets_router
from app.web.admin.network_tr069_provisions import (
    router as network_tr069_provisions_router,
)
from app.web.admin.network_uisp_control import router as network_uisp_control_router
from app.web.admin.network_vendor_capabilities import (
    router as network_vendor_capabilities_router,
)
from app.web.admin.network_weathermap import router as network_weathermap_router
from app.web.admin.network_zones import router as network_zones_router
from app.web.admin.notifications import router as notifications_router
from app.web.admin.payment_configuration_actions import (
    router as payment_configuration_actions_router,
)
from app.web.admin.projects import router as projects_router
from app.web.admin.provisioning import router as provisioning_router
from app.web.admin.reports import router as reports_router
from app.web.admin.resellers import router as resellers_router
from app.web.admin.sales import router as sales_router
from app.web.admin.service_change_reconciliation import (
    router as service_change_reconciliation_router,
)
from app.web.admin.service_requests_queue import router as service_requests_queue_router
from app.web.admin.service_teams import router as service_teams_router
from app.web.admin.support_assignment_rules import (
    router as support_assignment_rules_router,
)
from app.web.admin.support_automation import router as support_automation_router
from app.web.admin.support_tickets import router as support_tickets_router
from app.web.admin.system import router as system_router
from app.web.admin.system_whats_new import router as system_whats_new_router
from app.web.admin.usage import legacy_router as usage_legacy_router
from app.web.admin.usage import router as usage_router
from app.web.admin.vendor_operations import router as vendor_operations_router
from app.web.admin.vendor_routes import router as vendor_routes_router
from app.web.admin.vendors import router as vendors_router
from app.web.admin.wireguard import legacy_router as wireguard_legacy_router
from app.web.admin.wireguard import router as wireguard_router
from app.web.auth.dependencies import require_admin_web_auth

router = APIRouter(
    prefix="/admin",
    tags=["web-admin"],
    dependencies=[Depends(require_admin_web_auth)],
)


def get_current_user(request: Request) -> dict:
    """Get current user from session/request."""
    return web_admin_service.get_current_user(request)


def get_sidebar_stats(db: Session) -> dict:
    """Get stats for sidebar badges."""
    return web_admin_service.get_sidebar_stats(db)


@router.get("")
def admin_root():
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/operations/service-orders")
def operations_service_orders_legacy():
    """Legacy route redirect for service orders list."""
    return RedirectResponse(url="/admin/provisioning/orders", status_code=307)


@router.get("/operations/service-orders/new")
def operations_service_orders_new_legacy(subscriber: str | None = None):
    """Legacy route redirect — create form removed, redirect to orders list."""
    return RedirectResponse(url="/admin/provisioning/orders", status_code=307)


@router.get("/settings")
def admin_settings_hub(request: Request):
    """Canonical settings entry point."""
    query = str(request.url.query).strip()
    url = "/admin/system/settings-hub"
    if query:
        url = f"{url}?{query}"
    return RedirectResponse(url=url, status_code=307)


@router.get("/reports")
def admin_reports_hub():
    """Canonical reports entry point."""
    return RedirectResponse(url="/admin/reports/hub", status_code=307)


@router.get("/nas")
def admin_nas_legacy_root_redirect():
    """Legacy /admin/nas alias — NAS routes live under /admin/network/nas."""
    return RedirectResponse(url="/admin/network/nas/", status_code=307)


@router.get("/nas/{path:path}")
def admin_nas_legacy_path_redirect(path: str):
    """Legacy /admin/nas/<anything> alias."""
    return RedirectResponse(url=f"/admin/network/nas/{path}", status_code=307)


# Include all admin sub-routers
router.include_router(dashboard_router)
router.include_router(service_change_reconciliation_router)
router.include_router(design_system_router)
router.include_router(dispatch_work_orders_router)
router.include_router(field_maps_router)
router.include_router(vendor_routes_router)
router.include_router(crm_referrals_router)
router.include_router(
    customers_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("customer"))],
)
router.include_router(
    contacts_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("customer"))],
)
router.include_router(
    billing_invoices_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_accounts_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_consolidated_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_arrangements_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_payment_proofs_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_channels_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_collection_accounts_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    payment_configuration_actions_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_credits_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_extensions_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_dunning_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_invoice_batch_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_invoice_bulk_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_invoice_actions_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_payments_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(
    billing_reporting_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("billing"))],
)
router.include_router(inbox_router)
router.include_router(system_router)
router.include_router(system_whats_new_router)
router.include_router(
    network_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_core_devices_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_device_groups_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_fiber_plant_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_fiber_splice_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_ip_management_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_monitoring_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_cpes_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_uisp_control_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_olts_inventory_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_olts_profiles_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_onts_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_onts_actions_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_onts_inventory_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_operations_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_onts_provisioning_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_dns_threats_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_speedtests_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_weathermap_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_tr069_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_tr069_presets_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_tr069_provisions_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_radius_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_pop_sites_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_zones_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_onu_types_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_speed_profiles_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_authorization_presets_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_firmware_catalog_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    network_vendor_capabilities_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(
    catalog_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("catalog"))],
)
router.include_router(
    gis_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("gis"))],
)
router.include_router(
    reports_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("reports"))],
)
router.include_router(
    integrations_router,
    dependencies=[
        Depends(module_manager_service.require_module_enabled("integrations"))
    ],
)
router.include_router(resellers_router)
router.include_router(service_requests_queue_router)
router.include_router(service_teams_router)
router.include_router(alerts_router)
router.include_router(drift_router)
router.include_router(
    notifications_router,
    dependencies=[
        Depends(module_manager_service.require_module_enabled("notifications"))
    ],
)
router.include_router(support_tickets_router)
router.include_router(sales_router)
router.include_router(support_automation_router)
router.include_router(support_assignment_rules_router)
router.include_router(projects_router)
router.include_router(vendor_operations_router)
# Last of the /vendors routers on purpose: this one owns `/vendors/{vendor_id}`,
# which would otherwise swallow the literal `/vendors/routes` and
# `/vendors/operations` paths registered above.
router.include_router(vendors_router)
router.include_router(
    wireguard_router,
    prefix="/network",
    dependencies=[Depends(module_manager_service.require_module_enabled("vpn"))],
)
router.include_router(
    wireguard_legacy_router,
    prefix="/network",
    dependencies=[Depends(module_manager_service.require_module_enabled("vpn"))],
)
router.include_router(
    nas_router,
    prefix="/network",
    dependencies=[Depends(module_manager_service.require_module_enabled("network"))],
)
router.include_router(legal_router, prefix="/system")
router.include_router(
    catalog_settings_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("catalog"))],
)
router.include_router(
    legacy_add_ons_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("catalog"))],
)
router.include_router(
    usage_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("catalog"))],
)
router.include_router(
    usage_legacy_router,
    dependencies=[Depends(module_manager_service.require_module_enabled("catalog"))],
)
router.include_router(configuration_router, prefix="/system")
router.include_router(admin_hub_router, prefix="/system")
router.include_router(
    provisioning_router,
    dependencies=[
        Depends(module_manager_service.require_module_enabled("provisioning"))
    ],
)
__all__ = ["router"]
