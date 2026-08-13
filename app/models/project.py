"""Native project-domain persistence adapted from the CRM contract.

CRM shapes (``dotmac_crm/app/models/projects.py``) carried verbatim with the
sub conventions applied:

* PostgreSQL enums become string columns plus application enums. The
  CRM enum classes ``TaskStatus``/``TaskPriority``/``TaskDependencyType`` are
  exported here as ``ProjectTaskStatus``/``ProjectTaskPriority``/
  ``ProjectTaskDependencyType`` to avoid clashing with sub's provisioning
  ``TaskStatus``; the values remain identical to CRM.
* Staff ``people.id`` foreign keys are carried as plain UUIDs:
  the five ``projects.*_person_id`` roles, ``project_tasks.{assigned_to,
  created_by}_person_id``, both comment tables' ``author_person_id``, and
  ``project_task_assignees.person_id`` (still half of the composite PK).
  Display resolves through the shared staff identity map.
* ``projects.subscriber_id`` re-points at sub ``subscribers.id`` (link key 1),
  ``projects.lead_id`` is a real FK to the native ``leads`` table, and
  ``project_tasks.ticket_id`` points at Sub ``support_tickets.id``; import
  translates legacy ticket identifiers at the boundary.
* Field execution belongs to the work-order root. ``work_order.project_task_id``
  is the native optional FK, so one project task can require multiple visits.
  Migration 386 cuts over imported task-side links before removing the legacy
  UUID column.
* Both comment tables gain a ``metadata`` JSON column for import provenance
  because CRM has no metadata column there.

CRM UUID primary keys retained during import are now native Sub project keys.
There is no parallel project read projection after the authority cutover.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProjectStatus(enum.Enum):
    open = "open"
    planned = "planned"
    active = "active"
    on_hold = "on_hold"
    completed = "completed"
    canceled = "canceled"


class ProjectPriority(enum.Enum):
    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class ProjectType(enum.Enum):
    cable_rerun = "cable_rerun"
    fiber_optics_relocation = "fiber_optics_relocation"
    air_fiber_relocation = "air_fiber_relocation"
    fiber_optics_installation = "fiber_optics_installation"
    air_fiber_installation = "air_fiber_installation"
    cross_connect = "cross_connect"


class ProjectTaskStatus(enum.Enum):
    """CRM ``TaskStatus`` (renamed to avoid the provisioning TaskStatus)."""

    backlog = "backlog"
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    canceled = "canceled"


class ProjectTaskPriority(enum.Enum):
    """CRM ``TaskPriority`` (same vocabulary as ProjectPriority)."""

    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class ProjectTaskDependencyType(enum.Enum):
    """CRM ``TaskDependencyType``."""

    finish_to_start = "finish_to_start"
    start_to_start = "start_to_start"
    finish_to_finish = "finish_to_finish"
    start_to_finish = "start_to_finish"


class ProjectTemplate(Base):
    __tablename__ = "project_templates"
    __table_args__ = (
        UniqueConstraint("project_type", name="uq_project_templates_project_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    project_type: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text)
    creates_vendor_assignment_scope: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    tasks = relationship("ProjectTemplateTask", back_populates="template")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint(
            "external_system",
            "external_reference",
            name="uq_projects_external_system_reference",
        ),
        UniqueConstraint("quote_id", name="uq_projects_quote_id"),
        UniqueConstraint("sales_order_id", name="uq_projects_sales_order_id"),
        # Native /me/projects (+ reseller subtree) subscriber scan — partial on
        # is_active (see migration 251). The functional index backs the H1
        # quote→project lookup; its expression matches SQLAlchemy's
        # metadata_['quote_id'].as_string() (CAST(... AS VARCHAR)) so the
        # planner uses it. postgresql_where is ignored on sqlite create_all.
        Index(
            "ix_projects_subscriber_id",
            "subscriber_id",
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_projects_metadata_quote_id",
            text("CAST((metadata ->> 'quote_id') AS VARCHAR)"),
            postgresql_where=text("is_active"),
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    # Retains CRM's non-unique number contract.
    number: Mapped[str | None] = mapped_column(String(40))
    external_system: Mapped[str | None] = mapped_column(String(40))
    external_reference: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    customer_address: Mapped[str | None] = mapped_column(Text)
    project_type: Mapped[str | None] = mapped_column(String(60))
    project_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_templates.id")
    )
    status: Mapped[str] = mapped_column(
        String(40), default=ProjectStatus.open.value, nullable=False
    )
    priority: Mapped[str] = mapped_column(
        String(40), default=ProjectPriority.normal.value, nullable=False
    )
    # Customer party is the Sub subscriber.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id")
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quotes.id", name="fk_projects_quote_id", ondelete="RESTRICT"),
    )
    sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "sales_orders.id",
            name="fk_projects_sales_order_id",
            ondelete="RESTRICT",
        ),
    )
    # Staff UUIDs have no local FK; the staff map owns display resolution.
    # assistant_manager_person_id is the Site Project Coordinator.
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    manager_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    assistant_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    # Real FK to the shared service-team owner.
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    region: Mapped[str | None] = mapped_column(String(80))
    tags: Mapped[list | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    lead = relationship("Lead", foreign_keys=[lead_id])
    quote = relationship("Quote", back_populates="project", foreign_keys=[quote_id])
    sales_order = relationship(
        "SalesOrder", back_populates="project", foreign_keys=[sales_order_id]
    )
    service_team = relationship("ServiceTeam", foreign_keys=[service_team_id])
    project_template = relationship("ProjectTemplate")
    tasks = relationship("ProjectTask", back_populates="project")
    comments = relationship("ProjectComment", back_populates="project")
    work_orders = relationship("WorkOrder", back_populates="project")


class ProjectTask(Base):
    __tablename__ = "project_tasks"
    __table_args__ = (
        UniqueConstraint(
            "external_system",
            "external_reference",
            name="uq_project_tasks_external_system_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    number: Mapped[str | None] = mapped_column(String(40))
    external_system: Mapped[str | None] = mapped_column(String(40))
    external_reference: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    template_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id")
    )
    status: Mapped[str] = mapped_column(
        String(40), default=ProjectTaskStatus.todo.value, nullable=False
    )
    priority: Mapped[str] = mapped_column(
        String(40), default=ProjectTaskPriority.normal.value, nullable=False
    )
    # Staff person UUIDs have no local FK.
    assigned_to_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Native ticket link; import resolves legacy ticket identifiers first.
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id")
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effort_hours: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list | None] = mapped_column(JSON)
    # Preserves the CRM fiber-stage keys: fiber_stage_key, fiber_stage_title,
    # fiber_sla_managed, sla_breached, sla_breached_at.
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    project = relationship("Project", back_populates="tasks")
    parent_task = relationship("ProjectTask", remote_side=[id])
    template_task = relationship("ProjectTemplateTask")
    ticket = relationship("Ticket", foreign_keys=[ticket_id])
    work_orders = relationship("WorkOrder", back_populates="project_task")
    comments = relationship("ProjectTaskComment", back_populates="task")
    assignees = relationship(
        "ProjectTaskAssignee",
        back_populates="task",
        cascade="all, delete-orphan",
    )

    @property
    def assigned_to_person_ids(self) -> list[uuid.UUID]:
        if self.assignees:
            return [assignee.person_id for assignee in self.assignees]
        if self.assigned_to_person_id:
            return [self.assigned_to_person_id]
        return []


class ProjectTaskAssignee(Base):
    """Task↔staff assignment fact.

    ``person_id`` is a CRM staff-person UUID and half of the composite primary
    key. It has no local FK but remains identity material.
    Names resolve through the shared staff map; native assignments use Sub
    principals.
    """

    __tablename__ = "project_task_assignees"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    task = relationship("ProjectTask", back_populates="assignees")


class ProjectTemplateTask(Base):
    __tablename__ = "project_template_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_templates.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(40))
    priority: Mapped[str | None] = mapped_column(String(40))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    effort_hours: Mapped[int | None] = mapped_column(Integer)
    auto_create_work_order: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    work_order_requires_as_built_evidence: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    template = relationship("ProjectTemplate", back_populates="tasks")


class ProjectTemplateTaskDependency(Base):
    # Retain the established singular table name for schema compatibility.
    __tablename__ = "project_template_task_dependency"
    __table_args__ = (
        UniqueConstraint(
            "template_task_id",
            "depends_on_template_task_id",
            name="uq_project_template_task_dependency",
        ),
        CheckConstraint(
            "template_task_id <> depends_on_template_task_id",
            name="ck_project_template_task_dependency_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id"), nullable=False
    )
    depends_on_template_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(
        String(40),
        default=ProjectTaskDependencyType.finish_to_start.value,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0)

    template_task = relationship(
        "ProjectTemplateTask",
        foreign_keys=[template_task_id],
    )
    depends_on_template_task = relationship(
        "ProjectTemplateTask",
        foreign_keys=[depends_on_template_task_id],
    )


class ProjectTaskDependency(Base):
    __tablename__ = "project_task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "depends_on_task_id",
            name="uq_project_task_dependencies",
        ),
        CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_project_task_dependencies_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(
        String(40),
        default=ProjectTaskDependencyType.finish_to_start.value,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0)

    task = relationship("ProjectTask", foreign_keys=[task_id])
    depends_on_task = relationship("ProjectTask", foreign_keys=[depends_on_task_id])


class ProjectTaskComment(Base):
    __tablename__ = "project_task_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    # Staff person UUID with no local FK.
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    # Not in CRM; retained as import provenance.
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    task = relationship("ProjectTask", back_populates="comments")


class ProjectComment(Base):
    __tablename__ = "project_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    # Staff person UUID with no local FK.
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    # Not in CRM; retained as import provenance.
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    project = relationship("Project", back_populates="comments")
