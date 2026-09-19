"""Repository-curated admin help center."""

from __future__ import annotations

from dataclasses import dataclass, replace

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_workflow_guidance
from app.services.admin_workflow_guidance import AdminHelpAction
from app.services.auth_dependencies import can
from app.web.templates import templates

router = APIRouter(prefix="/help", tags=["web-admin-help"])


@dataclass(frozen=True)
class HelpArticle:
    id: str
    category: str
    title: str
    summary: str
    actions: tuple[AdminHelpAction, ...] = ()
    audience: str = ""
    notes: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.id


def _article_matches(article: HelpArticle, *, query: str, category: str) -> bool:
    return (not category or article.category == category) and (
        not query
        or query in article.title.casefold()
        or query in article.summary.casefold()
        or query in article.audience.casefold()
        or any(query in action.title.casefold() for action in article.actions)
        or any(
            query in step.casefold()
            for action in article.actions
            for step in action.steps
        )
        or any(query in note.casefold() for note in article.notes)
    )


def _group_articles(
    articles: list[HelpArticle], *, selected_id: str = ""
) -> list[dict[str, object]]:
    article_by_id = {article.id: article for article in articles}
    categories: list[dict[str, object]] = []
    for section in admin_workflow_guidance.help_navigation():
        category_articles = [
            article_by_id[guide_id]
            for guide_id in section.guide_ids
            if guide_id in article_by_id
        ]
        if category_articles:
            categories.append(
                {
                    "id": section.id,
                    "category": section.label,
                    "permission": section.permission,
                    "articles": category_articles,
                    "selected": any(
                        article.id == selected_id for article in category_articles
                    ),
                }
            )
    return categories


# The workflow-guidance registry is the authoritative Help Center content.
# ``HelpArticle`` is the typed presentation shape consumed by the existing UI.
ARTICLES = tuple(
    HelpArticle(
        id=guide.id,
        category=guide.category,
        title=guide.title,
        summary=guide.purpose,
        actions=admin_workflow_guidance.help_actions_for(guide),
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
    visible_sections = tuple(
        section
        for section in admin_workflow_guidance.help_navigation()
        if (
            (not section.permission and not section.any_permissions)
            or (section.permission and can(request, section.permission))
            or any(can(request, permission) for permission in section.any_permissions)
        )
    )
    visible_guide_ids = {
        guide_id
        for section in visible_sections
        for guide_id in section.guide_ids
        if not (
            required := admin_workflow_guidance.HELP_GUIDE_VIEW_PERMISSIONS.get(
                guide_id, ()
            )
        )
        or any(can(request, permission) for permission in required)
    }
    permission_filtered_articles = [
        replace(
            item,
            actions=tuple(
                action
                for action in item.actions
                if not action.permission or can(request, action.permission)
            ),
        )
        for item in ARTICLES
        if item.id in visible_guide_ids
    ]
    articles = [
        item
        for item in permission_filtered_articles
        if _article_matches(item, query=query, category=selected)
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
        "grouped_articles": _group_articles(
            articles, selected_id=selected_article.id if selected_article else ""
        ),
        "selected_article": selected_article,
        "categories": admin_workflow_guidance.guidance_categories(),
        "query": q,
        "selected_category": selected,
    }
    return templates.TemplateResponse(
        "admin/help/index.html",
        context,
    )
