"""Repository-curated admin help center."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_permission
from app.web.templates import templates

router = APIRouter(prefix="/help", tags=["web-admin-help"])


@dataclass(frozen=True)
class HelpArticle:
    category: str
    title: str
    summary: str
    steps: tuple[str, ...]


ARTICLES = (
    HelpArticle(
        "Getting started",
        "Navigate Selfcare",
        "Find customers, work and reports.",
        (
            "Choose a workspace from the left navigation.",
            "Use global search to locate a customer.",
            "Open Workqueue for assigned operational work.",
        ),
    ),
    HelpArticle(
        "Customers",
        "Find and update a customer",
        "Locate customer identity, services and billing.",
        (
            "Open Customers.",
            "Search by name, account number, phone or email.",
            "Choose the relevant tab before making a permitted change.",
        ),
    ),
    HelpArticle(
        "Support",
        "Manage a support ticket",
        "Assign, update and resolve customer issues.",
        (
            "Open Support tickets.",
            "Confirm priority, region and assignment.",
            "Record progress and use the configured transition.",
        ),
    ),
    HelpArticle(
        "Inbox",
        "Respond from Team Inbox",
        "Handle email, WhatsApp and social conversations.",
        (
            "Open Inbox.",
            "Select or assign a conversation.",
            "Reply, add an internal note, or create a linked ticket.",
        ),
    ),
    HelpArticle(
        "Surveys",
        "Create and share a survey",
        "Collect structured customer feedback.",
        (
            "Open Surveys and create a survey.",
            "Add question definitions and save.",
            "Copy the public response link from survey details.",
        ),
    ),
    HelpArticle(
        "Integrations",
        "Connect Meta",
        "Review Facebook and Instagram readiness.",
        (
            "Open Meta connection.",
            "Configure the Meta application under communication settings.",
            "Connect and monitor Page and Instagram token health.",
        ),
    ),
    HelpArticle(
        "FAQ",
        "Why can’t I see an action?",
        "Actions are hidden when your role lacks permission.",
        (
            "Ask an administrator to review your assigned role.",
            "Provide the page URL and action you need.",
            "Never share credentials or session cookies.",
        ),
    ),
    HelpArticle(
        "FAQ",
        "Where should I report a problem?",
        "Create a support ticket with reproducible evidence.",
        (
            "Record the page and approximate time.",
            "Describe expected and observed results.",
            "Attach a screenshot without unnecessary customer data.",
        ),
    ),
)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def help_center(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    query = q.strip().casefold()
    selected = category.strip()
    articles = [
        article
        for article in ARTICLES
        if (not selected or article.category == selected)
        and (
            not query
            or query in article.title.casefold()
            or query in article.summary.casefold()
            or any(query in step.casefold() for step in article.steps)
        )
    ]
    return templates.TemplateResponse(
        "admin/help/index.html",
        {
            "request": request,
            "active_page": "help-center",
            "active_menu": "help",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "articles": articles,
            "categories": sorted({article.category for article in ARTICLES}),
            "query": q,
            "selected_category": selected,
        },
    )
