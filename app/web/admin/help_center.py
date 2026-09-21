"""Repository-curated admin help center."""

from __future__ import annotations

from dataclasses import dataclass
from re import sub

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_workflow_guidance
from app.web.templates import templates

router = APIRouter(prefix="/help", tags=["web-admin-help"])


@dataclass(frozen=True)
class HelpArticle:
    category: str
    title: str
    summary: str
    steps: tuple[str, ...]
    audience: str = ""
    notes: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        value = sub(r"[^a-z0-9]+", "-", self.title.casefold()).strip("-")
        return value or "article"


def _article_matches(article: HelpArticle, *, query: str, category: str) -> bool:
    return (not category or article.category == category) and (
        not query
        or query in article.title.casefold()
        or query in article.summary.casefold()
        or query in article.audience.casefold()
        or any(query in step.casefold() for step in article.steps)
        or any(query in note.casefold() for note in article.notes)
    )


def _group_articles(articles: list[HelpArticle]) -> list[dict[str, object]]:
    categories: list[dict[str, object]] = []
    for category in admin_workflow_guidance.guidance_categories():
        category_articles = [
            article for article in articles if article.category == category
        ]
        if category_articles:
            categories.append({"category": category, "articles": category_articles})
    return categories


ARTICLES: tuple[HelpArticle, ...] = (
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

# The workflow-guidance registry is the authoritative Help Center content.
# ``HelpArticle`` is the typed presentation shape consumed by the existing UI.
ARTICLES = tuple(
    HelpArticle(
        category=guide.category,
        title=guide.title,
        summary=guide.purpose,
        steps=guide.steps,
        audience=guide.audience,
        notes=guide.notes,
    )
    for guide in admin_workflow_guidance.all_guidance()
)


@router.get(
    "",
    response_class=HTMLResponse,
)
def help_center(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    article: str = Query(""),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    query = q.strip().casefold()
    selected = category.strip()
    articles = [
        article
        for article in ARTICLES
        if _article_matches(article, query=query, category=selected)
    ]
    selected_article = next(
        (item for item in articles if item.slug == article.strip()),
        articles[0] if articles else None,
    )
    context: dict[str, object] = {
        "request": request,
        "active_page": "help-center",
        "active_menu": "help",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "articles": articles,
        "grouped_articles": _group_articles(articles),
        "selected_article": selected_article,
        "categories": admin_workflow_guidance.guidance_categories(),
        "query": q,
        "selected_category": selected,
    }
    return templates.TemplateResponse(
        "admin/help/index.html",
        context,
    )
