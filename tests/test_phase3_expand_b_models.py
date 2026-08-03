"""PR 2: expand-B native tables — schema-shape assertions.

Covers the table map (projects, leads/pipeline, quotes, sales orders,
referrals, work_links), the enum vocabularies (exact values), the
person/staff FK-clash treatment (customer FKs → subscribers, staff columns →
plain UUIDs), and the 244 migration's revision chain.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.db import Base
from app.models.project import (
    Project,
    ProjectComment,
    ProjectPriority,
    ProjectStatus,
    ProjectTask,
    ProjectTaskAssignee,
    ProjectTaskComment,
    ProjectTaskDependencyType,
    ProjectTaskPriority,
    ProjectTaskStatus,
    ProjectTemplate,
    ProjectType,
)
from app.models.referral_native import (
    Referral,
    ReferralCode,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.sales import (
    Lead,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteLineItem,
    QuoteStatus,
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.models.work_link import WorkEntityType, WorkLink, WorkLinkType
from app.models.work_order import WorkOrder

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every table the expand-B migration creates, named exactly per .
PHASE3_TABLES = [
    "pipelines",
    "pipeline_stages",
    "leads",
    "quotes",
    "quote_line_items",
    "sales_orders",
    "sales_order_lines",
    "project_templates",
    "project_template_tasks",
    "project_template_task_dependency",  # singular in CRM — kept
    "projects",
    "project_tasks",
    "project_task_assignees",
    "project_task_dependencies",
    "project_task_comments",
    "project_comments",
    "referral_codes",
    "referrals",
    "work_links",
]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", PHASE3_TABLES)
def test_phase3_table_registered(table_name):
    assert table_name in Base.metadata.tables


def test_only_unmigrated_vertical_mirrors_remain():
    for mirror in (
        "quote_mirror",
        "quote_sync_state",
        "referral_mirror",
        "referral_program_cache",
    ):
        assert mirror in Base.metadata.tables
    assert "project_mirror" not in Base.metadata.tables
    assert "project_sync_state" not in Base.metadata.tables
    assert "work_order_sync_state" not in Base.metadata.tables


# ---------------------------------------------------------------------------
# Enum vocabularies — exact values
# ---------------------------------------------------------------------------


def _values(enum_cls) -> list[str]:
    return [member.value for member in enum_cls]


def test_enum_vocabularies_exact():
    assert _values(ProjectStatus) == [
        "open",
        "planned",
        "active",
        "on_hold",
        "completed",
        "canceled",
    ]
    assert _values(ProjectPriority) == [
        "lower",
        "low",
        "medium",
        "normal",
        "high",
        "urgent",
    ]
    assert _values(ProjectTaskPriority) == _values(ProjectPriority)
    assert _values(ProjectType) == [
        "cable_rerun",
        "fiber_optics_relocation",
        "air_fiber_relocation",
        "fiber_optics_installation",
        "air_fiber_installation",
        "cross_connect",
    ]
    assert _values(ProjectTaskStatus) == [
        "backlog",
        "todo",
        "in_progress",
        "blocked",
        "done",
        "canceled",
    ]
    assert _values(ProjectTaskDependencyType) == [
        "finish_to_start",
        "start_to_start",
        "finish_to_finish",
        "start_to_finish",
    ]
    assert _values(LeadStatus) == [
        "new",
        "contacted",
        "qualified",
        "proposal",
        "negotiation",
        "won",
        "lost",
    ]
    assert _values(QuoteStatus) == ["draft", "sent", "accepted", "rejected", "expired"]
    assert _values(SalesOrderStatus) == [
        "draft",
        "confirmed",
        "paid",
        "fulfilled",
        "cancelled",
    ]
    assert _values(SalesOrderPaymentStatus) == ["pending", "partial", "paid", "waived"]
    assert _values(ReferralStatus) == [
        "pending",
        "qualified",
        "rewarded",
        "rejected",
        "expired",
    ]
    assert _values(ReferralRewardStatus) == [
        "none",
        "pending",
        "approved",
        "issued",
        "void",
    ]
    assert _values(WorkEntityType) == [
        "ticket",
        "project",
        "project_task",
        "work_order",
        "lead",
        "sales_order",
        "subscriber",
        "internal",
    ]
    assert _values(WorkLinkType) == [
        "originated",
        "fulfills",
        "blocks",
        "related",
        "resulted_in",
    ]


def test_status_columns_are_strings_not_pg_enums():
    """Sub convention: PG enums become String columns + app enums."""
    from sqlalchemy import Enum as SaEnum
    from sqlalchemy import String as SaString

    checks = [
        ("projects", "status"),
        ("projects", "priority"),
        ("projects", "project_type"),
        ("project_tasks", "status"),
        ("project_tasks", "priority"),
        ("project_task_dependencies", "dependency_type"),
        ("leads", "status"),
        ("quotes", "status"),
        ("sales_orders", "status"),
        ("sales_orders", "payment_status"),
        ("referrals", "status"),
        ("referrals", "reward_status"),
        ("work_links", "source_type"),
        ("work_links", "link_type"),
    ]
    for table_name, column_name in checks:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, SaString), (table_name, column_name)
        assert not isinstance(column.type, SaEnum), (table_name, column_name)


# ---------------------------------------------------------------------------
# FK clash treatment
# ---------------------------------------------------------------------------


def _fk_targets(table_name: str, column_name: str) -> set[str]:
    column = Base.metadata.tables[table_name].columns[column_name]
    return {fk.target_fullname for fk in column.foreign_keys}


# Staff person / external agent / inventory / WO columns: FKs
# dropped, plain UUIDs carried verbatim. Native campaign FKs materialized in
# revision 355 and therefore no longer belong in this legacy list.
NO_FK_COLUMNS = [
    ("projects", "created_by_person_id"),
    ("projects", "owner_person_id"),
    ("projects", "manager_person_id"),
    ("projects", "project_manager_person_id"),
    ("projects", "assistant_manager_person_id"),
    ("project_tasks", "assigned_to_person_id"),
    ("project_tasks", "created_by_person_id"),
    ("project_task_assignees", "person_id"),  # composite-PK half, still no FK
    ("project_task_comments", "author_person_id"),
    ("project_comments", "author_person_id"),
    ("leads", "owner_agent_id"),
    ("quotes", "owner_person_id"),
    ("quote_line_items", "inventory_item_id"),
    ("sales_orders", "owner_agent_id"),
    ("sales_order_lines", "inventory_item_id"),
    ("work_links", "created_by_person_id"),
]

# Customer-party columns: real FKs to sub subscribers.
SUBSCRIBER_FK_COLUMNS = [
    ("projects", "subscriber_id"),
    ("leads", "subscriber_id"),
    ("quotes", "subscriber_id"),
    ("sales_orders", "subscriber_id"),
    ("referral_codes", "subscriber_id"),
    ("referrals", "referrer_subscriber_id"),
    ("referrals", "referred_subscriber_id"),
]


@pytest.mark.parametrize("table_name,column_name", NO_FK_COLUMNS)
def test_staff_and_deferred_columns_are_plain_uuids(table_name, column_name):
    assert _fk_targets(table_name, column_name) == set(), (table_name, column_name)


@pytest.mark.parametrize("table_name,column_name", SUBSCRIBER_FK_COLUMNS)
def test_customer_party_columns_fk_subscribers(table_name, column_name):
    assert _fk_targets(table_name, column_name) == {"subscribers.id"}


def test_customer_party_columns_not_null():
    """Account-specific records remain required; Party-first Lead/referral
    account links are nullable compatibility/conversion context."""
    for table_name, column_name in [
        ("sales_orders", "subscriber_id"),
        ("referral_codes", "subscriber_id"),
        ("referrals", "referrer_subscriber_id"),
    ]:
        assert not Base.metadata.tables[table_name].columns[column_name].nullable
    # Draft/Sent Quotes are Lead-first and receive their account only during
    # the atomic Accepted transition.
    assert Base.metadata.tables["quotes"].columns["subscriber_id"].nullable
    assert Base.metadata.tables["leads"].columns["subscriber_id"].nullable
    assert Base.metadata.tables["referrals"].columns["referred_subscriber_id"].nullable
    # projects.subscriber_id stays nullable (CRM shape).
    assert Base.metadata.tables["projects"].columns["subscriber_id"].nullable


def test_intra_vertical_fks():
    assert _fk_targets("projects", "lead_id") == {"leads.id"}
    assert _fk_targets("projects", "service_team_id") == {"service_teams.id"}
    assert _fk_targets("projects", "project_template_id") == {"project_templates.id"}
    assert _fk_targets("project_tasks", "ticket_id") == {"support_tickets.id"}
    assert _fk_targets("project_tasks", "project_id") == {"projects.id"}
    assert _fk_targets("quotes", "lead_id") == {"leads.id"}
    assert _fk_targets("quote_line_items", "quote_id") == {"quotes.id"}
    assert _fk_targets("sales_orders", "quote_id") == {"quotes.id"}
    assert _fk_targets("sales_order_lines", "sales_order_id") == {"sales_orders.id"}
    assert _fk_targets("leads", "pipeline_id") == {"pipelines.id"}
    assert _fk_targets("leads", "stage_id") == {"pipeline_stages.id"}
    assert _fk_targets("leads", "campaign_id") == {"campaigns.id"}
    assert _fk_targets("leads", "campaign_recipient_id") == {"campaign_recipients.id"}
    assert _fk_targets("referrals", "referral_code_id") == {"referral_codes.id"}
    assert _fk_targets("referrals", "referred_lead_id") == {"leads.id"}


def test_subscribers_sales_order_fk_materialized():
    """PR1 deferred the FK; expand B makes it real."""
    assert _fk_targets("subscribers", "sales_order_id") == {"sales_orders.id"}


def test_project_task_assignees_composite_pk():
    table = Base.metadata.tables["project_task_assignees"]
    assert {c.name for c in table.primary_key.columns} == {"task_id", "person_id"}
    task_fk = _fk_targets("project_task_assignees", "task_id")
    assert task_fk == {"project_tasks.id"}


# ---------------------------------------------------------------------------
# Constraints & indexes preserved
# ---------------------------------------------------------------------------


def _constraint_names(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].constraints if c.name}


def _index_names(table_name: str) -> set[str]:
    return {i.name for i in Base.metadata.tables[table_name].indexes}


def test_ported_unique_and_check_constraints():
    assert "uq_project_templates_project_type" in _constraint_names("project_templates")
    assert {
        "uq_project_template_task_dependency",
        "ck_project_template_task_dependency_no_self",
    } <= _constraint_names("project_template_task_dependency")
    assert {
        "uq_project_task_dependencies",
        "ck_project_task_dependencies_no_self",
    } <= _constraint_names("project_task_dependencies")
    assert {
        "uq_sales_orders_order_number",
        "uq_sales_orders_quote_id",
    } <= _constraint_names("sales_orders")
    assert "uq_work_links_source_target_link_type" in _constraint_names("work_links")


def test_projects_number_stays_non_unique():
    column = Base.metadata.tables["projects"].columns["number"]
    assert not column.unique
    assert not any(
        column in list(index.columns) and index.unique
        for index in Base.metadata.tables["projects"].indexes
    )


def test_referral_indexes_recreated_on_subscriber_columns():
    table = Base.metadata.tables["referrals"]
    indexes = {i.name: i for i in table.indexes}
    referrer = indexes["ix_referrals_referrer"]
    assert [c.name for c in referrer.columns] == ["referrer_subscriber_id", "status"]
    guard = indexes["uq_referrals_active_referred_subscriber"]
    assert guard.unique
    assert [c.name for c in guard.columns] == ["referred_subscriber_id"]
    assert "referred_subscriber_id IS NOT NULL" in str(
        guard.dialect_options["postgresql"]["where"]
    )


def test_work_links_indexes():
    assert {
        "ix_work_links_source",
        "ix_work_links_target",
        "ix_work_links_contract",
    } <= _index_names("work_links")


# ---------------------------------------------------------------------------
# Migration 244 — revision chain + guards
# ---------------------------------------------------------------------------


def _load_migration():
    path = REPO_ROOT / "alembic" / "versions" / "244_phase3_expand_b_tables.py"
    spec = importlib.util.spec_from_file_location("migration_244", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_244_revision_chain():
    module = _load_migration()
    assert module.revision == "244_phase3_expand_b_tables"
    assert module.down_revision == "243_phase3_organizations_party"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_244_is_single_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    # The chain keeps advancing as later PRs stack on; ONT confirmation added
    # asset inventory now extends the current migration chain.
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {len(heads)}: {heads}. "
        "Two branches almost certainly numbered their migrations off the same "
        "parent -- renumber the later one onto the current head."
    )


def test_migration_244_source_recreates_leads_partial_unique():
    source = (
        REPO_ROOT / "alembic" / "versions" / "244_phase3_expand_b_tables.py"
    ).read_text()
    assert "uq_leads_one_open_per_subscriber_pipeline" in source
    assert "status NOT IN ('won', 'lost')" in source


# ---------------------------------------------------------------------------
# End-to-end model wiring (sqlite create_all via the db_session fixture)
# ---------------------------------------------------------------------------


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Amara",
        last_name="Eze",
        email=f"amara-{uuid4().hex[:10]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_full_vertical_chain_persists(db_session):
    """Lead → quote → sales order → project (+ referral) round-trips."""
    subscriber = _subscriber(db_session)

    pipeline = Pipeline(name="Fiber installs")
    db_session.add(pipeline)
    db_session.flush()
    stage = PipelineStage(pipeline_id=pipeline.id, name="New", order_index=1)
    db_session.add(stage)
    db_session.flush()

    lead = Lead(
        subscriber_id=subscriber.id,
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        title="Self-serve installation request",
        lead_source="Portal",
        estimated_value=150000,
        probability=50,
        metadata_={"source": "portal_self_serve"},
    )
    db_session.add(lead)
    db_session.flush()
    assert lead.status == LeadStatus.new.value
    assert lead.contact_id == subscriber.id
    assert lead.weighted_value == 75000

    quote = Quote(
        subscriber_id=subscriber.id,
        lead_id=lead.id,
        metadata_={"source": "portal_self_serve", "deposit_percent": 50},
    )
    db_session.add(quote)
    db_session.flush()
    line = QuoteLineItem(
        quote_id=quote.id,
        description="Fiber installation bundle",
        quantity=1,
        unit_price=150000,
        amount=150000,
        metadata_={"sub_offer_id": str(uuid4())},
    )
    db_session.add(line)
    db_session.flush()
    assert quote.status == QuoteStatus.draft.value
    assert quote.line_items[0].id == line.id

    order = SalesOrder(
        quote_id=quote.id,
        subscriber_id=subscriber.id,
        order_number="SO-000123",
        total=150000,
        balance_due=150000,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        SalesOrderLine(
            sales_order_id=order.id,
            description="Fiber installation bundle",
            amount=150000,
        )
    )
    db_session.flush()
    assert order.status == SalesOrderStatus.draft.value
    assert order.payment_status == SalesOrderPaymentStatus.pending.value
    assert quote.sales_order_id == order.id

    # Account-matrix link: subscriber → creating sales order.
    subscriber.sales_order_id = order.id
    db_session.flush()

    template = ProjectTemplate(
        name="Fiber install", project_type=ProjectType.fiber_optics_installation.value
    )
    db_session.add(template)
    db_session.flush()
    project = Project(
        name="Install — Amara Eze",
        project_type=ProjectType.fiber_optics_installation.value,
        project_template_id=template.id,
        subscriber_id=subscriber.id,
        lead_id=lead.id,
        owner_person_id=uuid4(),  # staff UUID, no FK to satisfy
        metadata_={"quote_id": str(quote.id)},
    )
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        title="Survey",
        assigned_to_person_id=uuid4(),
        metadata_={"fiber_stage_key": "survey"},
    )
    db_session.add(task)
    db_session.flush()
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        project_id=project.id,
        project_task_id=task.id,
        title="Survey",
    )
    db_session.add(work_order)
    db_session.flush()
    db_session.add(ProjectTaskAssignee(task_id=task.id, person_id=uuid4()))
    db_session.add(ProjectTaskComment(task_id=task.id, body="Booked", metadata_={}))
    db_session.add(ProjectComment(project_id=project.id, body="Kickoff"))
    db_session.flush()
    assert project.status == ProjectStatus.open.value
    assert task.status == ProjectTaskStatus.todo.value
    assert task.assigned_to_person_ids == [
        assignee.person_id for assignee in task.assignees
    ]
    assert task.work_orders == [work_order]

    code = ReferralCode(subscriber_id=subscriber.id, code="DM7X4KQ2")
    db_session.add(code)
    db_session.flush()
    referred = _subscriber(db_session)
    referral = Referral(
        referrer_subscriber_id=subscriber.id,
        referral_code_id=code.id,
        referred_subscriber_id=referred.id,
        referred_lead_id=lead.id,
        metadata_={"capture": {"name": "Chidi"}},
    )
    db_session.add(referral)
    db_session.flush()
    assert referral.status == ReferralStatus.pending.value
    assert referral.reward_status == ReferralRewardStatus.none.value

    link = WorkLink(
        source_type=WorkEntityType.lead.value,
        source_id=lead.id,
        target_type=WorkEntityType.project.value,
        target_id=project.id,
        link_type=WorkLinkType.resulted_in.value,
        created_at=datetime.now(UTC),
    )
    db_session.add(link)
    db_session.flush()
    assert link.id is not None
