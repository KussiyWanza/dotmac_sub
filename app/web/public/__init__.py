"""Public web routes (unauthenticated)."""

from fastapi import APIRouter

from app.web.public.branding import router as branding_router
from app.web.public.catalogues import router as catalogues_router
from app.web.public.lead_intake import router as lead_intake_router
from app.web.public.legal import router as legal_router
from app.web.public.network_graphs import router as network_graphs_router
from app.web.public.surveys import router as surveys_router
from app.web.public.ticket_confirm import router as ticket_confirm_router

router = APIRouter(tags=["web-public"])

router.include_router(branding_router)
router.include_router(catalogues_router)
router.include_router(legal_router)
router.include_router(lead_intake_router)
router.include_router(network_graphs_router)
router.include_router(ticket_confirm_router)
router.include_router(surveys_router)

__all__ = ["router"]
