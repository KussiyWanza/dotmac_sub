"""
Provisioning Template Service.

Manages CRUD operations and template rendering for NAS provisioning templates.
Extracted from the monolithic nas.py service.
"""

import logging
import re
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasVendor,
    ProvisioningAction,
    ProvisioningTemplate,
)
from app.schemas.catalog import (
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


def _assert_template_body_allowed(content: object) -> None:
    """Reject a template body that would mutate a NAS-local PPPoE secret.

    Enforced at authoring time as well as execution time. Catching it only on
    execution would leave a saved row that looks legitimate in the UI and fails
    for the first operator who runs it.
    """
    from app.services.nas.local_secret_policy import assert_command_text_allowed

    if content is None:
        return
    assert_command_text_allowed(str(content), context="Provisioning template body")


class ProvisioningTemplates(ListResponseMixin):
    """Service class for provisioning template operations."""

    @staticmethod
    def create(
        db: Session, payload: ProvisioningTemplateCreate
    ) -> ProvisioningTemplate:
        """Create a new provisioning template."""
        data = payload.model_dump(exclude_unset=True)

        # Extract placeholders from template content if not provided
        if not data.get("placeholders"):
            content = data.get("template_content", "")
            # Find all {{placeholder}} patterns
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        _assert_template_body_allowed(data.get("template_content"))

        template = ProvisioningTemplate(**data)
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def get(db: Session, template_id: str | UUID) -> ProvisioningTemplate:
        """Get a provisioning template by ID."""
        template_id = coerce_uuid(template_id)
        template = cast(
            ProvisioningTemplate | None, db.get(ProvisioningTemplate, template_id)
        )
        if not template:
            raise HTTPException(
                status_code=404, detail="Provisioning template not found"
            )
        return template

    @staticmethod
    def get_by_code(db: Session, code: str) -> ProvisioningTemplate | None:
        """Get a template by its code."""
        return cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate).where(ProvisioningTemplate.code == code)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> list[ProvisioningTemplate]:
        """List provisioning templates with filtering."""
        query = select(ProvisioningTemplate).order_by(ProvisioningTemplate.name)

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> int:
        """Count provisioning templates with filtering (same filters as list)."""
        query = select(func.count(ProvisioningTemplate.id))

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        return db.execute(query).scalar() or 0

    @staticmethod
    def find_template(
        db: Session,
        vendor: NasVendor,
        connection_type: ConnectionType,
        action: ProvisioningAction,
    ) -> ProvisioningTemplate | None:
        """Find the best matching template for given criteria."""
        # First try exact match
        template = cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate)
                .where(ProvisioningTemplate.vendor == vendor)
                .where(ProvisioningTemplate.connection_type == connection_type)
                .where(ProvisioningTemplate.action == action)
                .where(ProvisioningTemplate.is_active == True)
                .order_by(ProvisioningTemplate.is_default.desc())
                .limit(1)
            ).scalar_one_or_none(),
        )

        if template:
            return template

        # Fall back to "other" vendor with same connection type and action
        return cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate)
                .where(ProvisioningTemplate.vendor == NasVendor.other)
                .where(ProvisioningTemplate.connection_type == connection_type)
                .where(ProvisioningTemplate.action == action)
                .where(ProvisioningTemplate.is_active == True)
                .limit(1)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def update(
        db: Session, template_id: str | UUID, payload: ProvisioningTemplateUpdate
    ) -> ProvisioningTemplate:
        """Update a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        data = payload.model_dump(exclude_unset=True)

        # Re-extract placeholders if content changed
        if "template_content" in data and not data.get("placeholders"):
            content = data["template_content"]
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        if "template_content" in data:
            _assert_template_body_allowed(data["template_content"])

        for key, value in data.items():
            setattr(template, key, value)

        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def delete(db: Session, template_id: str | UUID) -> None:
        """Delete a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        db.delete(template)
        db.commit()

    @staticmethod
    def render(template: ProvisioningTemplate, variables: dict[str, Any]) -> str:
        """Render a template with given variables."""
        content = str(template.template_content or "")
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))
        return content
