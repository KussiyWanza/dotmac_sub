"""Who writes, adapts and consumes cohort-isp-01 source state today.

The writer *census* is mechanical — `scripts/architecture/isp_cohort_writers.py`
counts it from the AST, and two ratchets freeze the result. This module is the
other half: what each counted surface **is**, which no static scan can decide.
A file that assigns `subscriber.status` and a file that assigns
`subscriber.metadata_` are the same shape to a parser and completely different
facts to a migration.

## Three questions, three vocabularies

The first version of this inventory answered all of it with one enum, and the
enum kept forcing dishonest answers. An applied migration is not an
"authorized adapter", but calling it a "legacy parallel writer" reads as
something a cutover could retire. A fixture seeder writes real rows and has no
authority at all. Those are not edge cases in one classification — they are
three independent questions being asked at once:

- **`AuthorityRole`** — what say does this surface have over the fact?
- **`BoundaryRole`** — what does it actually do at the boundary?
- **`Reachability`** — how can it still be reached against production?

They are genuinely orthogonal, and
`tests/test_isp_cohort_source_readiness.py::test_the_three_axes_are_orthogonal`
proves it rather than asserting it: for every ordered pair of axes, knowing one
value must leave at least two possibilities open on the other. If a future
inventory collapses to where one axis determines another, that test fails and
the axes should be merged rather than quietly kept apart.

`SurfaceClassification` survives as a *derived* view over the axes, so the
original eight-member vocabulary — and every document written against it —
still means what it meant.

## A disposition, for every surface

Classification says what a surface is. It does not say what happens to it, and
"what happens to it" is the question a cutover actually needs answered. Every
surface therefore carries a `Disposition`, including the adapters and readers
that write nothing: a route that reads a customer account has to come from
somewhere after that account lives in another application.

`Disposition.UNDECIDED` is a real state and carries a required `open_question`.
Three surfaces hold one today. They are enumerable through
`undecided_surfaces()` precisely so they cannot be lost between here and
`ctl-isp-006`.

## The classification is a finding, not a plan

`PARALLEL_WRITER` here does not schedule anything. It records that a second
path writes a fact some other owner is declared to own, so the cutover sequence
knows what has to be displaced before authority can move. Nothing in this
module retires, disables or reroutes a writer; Sub remains the sole production
writer for this cohort until a separately authorised sealed switch.

## UNDETERMINED means undetermined

Each axis has an `UNDETERMINED` member reserved for a surface whose answer was
looked for and not established. It is never a synonym for "none found" and
never a placeholder for "probably fine" — an undetermined axis is an open
question carried into the cutover gate, and the validators below refuse one
that does not say what is unknown about it.

The inverse matters just as much. `organizations` and `organization_memberships`
have **no** counted writer, and that is a searched result rather than an
absence of effort: no construction, no tracked mutation, no set-based DML and
no raw statement names them anywhere under `app/`, `scripts/` or the executable
migration lineage. That is recorded as a stated finding in
`TABLES_WITH_NO_COUNTED_WRITER`, with the census's blind spots named alongside
it, so nobody later reads the empty result as undetermined or as proof.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from app.migration_source.cohort import CohortEntityType


class AuthorityRole(StrEnum):
    """What say a surface has over the cohort fact it touches."""

    #: The SOT registry declares it the owner of this fact.
    DECLARED_OWNER = "declared_owner"
    #: Writes a value derived from a decision owned elsewhere.
    PROJECTION_WRITER = "projection_writer"
    #: Writes a fact another owner is declared to own.
    PARALLEL_WRITER = "parallel_writer"
    #: Writes under Alembic's deployed-schema authority rather than a
    #: service's. Real authority, exercised once, at deploy time.
    SCHEMA_LINEAGE = "schema_lineage"
    #: Decides nothing about the fact.
    NO_AUTHORITY = "no_authority"
    #: Authority was looked for and not established.
    UNDETERMINED = "undetermined"


class BoundaryRole(StrEnum):
    """What a surface does at the boundary, independent of its authority."""

    #: Issues the write itself.
    PERSISTS = "persists"
    #: Validates, authorises and calls an owner. Owns the transaction and
    #: decides nothing — which is what `db.commit()` in a route means here.
    DELEGATES = "delegates"
    #: Records an external fact as an observation, deciding nothing.
    OBSERVES = "observes"
    #: Carries a payload between systems and holds none of it.
    TRANSPORTS = "transports"
    #: Reads cohort state and never writes it.
    READS = "reads"
    #: The boundary behaviour was looked for and not established.
    UNDETERMINED = "undetermined"


class Reachability(StrEnum):
    """How a surface can still be reached against the production database."""

    #: A live request path — HTTP or WebSocket.
    ONLINE_REQUEST = "online_request"
    #: A task, worker or scheduled job.
    BACKGROUND_JOB = "background_job"
    #: A command a human runs on a host.
    OPERATOR_COMMAND = "operator_command"
    #: Reached only from other application code.
    INTERNAL_ONLY = "internal_only"
    #: An applied migration. The lineage will not run it again.
    APPLIED_ONCE = "applied_once"
    #: Disposable databases only.
    NON_PRODUCTION = "non_production"
    #: Reachability was looked for and not established.
    UNDETERMINED = "undetermined"


class Disposition(StrEnum):
    """What happens to this surface's cohort-1 touch when authority moves.

    About the surface's *cohort-1 touch*, not the whole file.
    `account_lifecycle.py` keeps owning subscription lifecycle long after its
    Subscriber projection writes are displaced.

    There is deliberately no "stays as it is" member. Every surface here
    touches cohort-1 state — that is the inclusion criterion — and once that
    state lives in another application, no such touch can survive unchanged.
    A member meaning "nothing happens" would be the one anybody reached for to
    avoid deciding, and this vocabulary exists to force the decision.
    """

    #: Displaced by the target. Must reach zero for `ctl-isp-009`.
    RETIRE_AFTER_CUTOVER = "retire_after_cutover"
    #: Must stop bypassing its declared owner *before* the cohort can be
    #: shadowed — a comparison against a source with two writers cannot
    #: distinguish drift from the second writer.
    ROUTE_THROUGH_OWNER_FIRST = "route_through_owner_first"
    #: Reads or forwards a cohort fact, so after the switch it must reach the
    #: target through a versioned contract. ADR 0012 permits no other shape:
    #: the two applications share no tables, sessions or transactions.
    REPOINT_TO_TARGET_API = "repoint_to_target_api"
    #: An applied migration. There is nothing to retire.
    HISTORICAL_NO_ACTION = "historical_no_action"
    #: Writes only disposable databases.
    NON_PRODUCTION_NO_ACTION = "non_production_no_action"
    #: Needs a decision. Carries a required `open_question`.
    UNDECIDED = "undecided"


class SurfaceClassification(StrEnum):
    """The original single vocabulary, retained as a derived view.

    Every document and control record written against these eight names keeps
    meaning what it meant. `SourceSurface.classification` computes it from the
    axes, so the two can never disagree — there is no second place to edit.
    """

    AUTHORITATIVE_WRITER = "authoritative_writer"
    AUTHORIZED_ADAPTER = "authorized_adapter"
    DERIVED_PROJECTION = "derived_projection"
    OBSERVATION_COLLECTOR = "observation_collector"
    TRANSPORT = "transport"
    READ_ONLY_CONSUMER = "read_only_consumer"
    LEGACY_PARALLEL_WRITER = "legacy_parallel_writer"
    UNKNOWN = "unknown"


class EntryPointFamily(StrEnum):
    """Mirrors the census families so inventory and ratchet speak one language.

    Duplicated deliberately rather than imported from
    `scripts/architecture/isp_cohort_writers`: `app/` must not import from
    `scripts/`, and a test asserts the two enumerations still have identical
    members, so the duplication cannot drift unnoticed.
    """

    API_ROUTE = "api_route"
    WEBHOOK_HANDLER = "webhook_handler"
    WEB_ROUTE = "web_route"
    WEB_PRESENTER = "web_presenter"
    SERVICE = "service"
    TASK_WORKER = "task_worker"
    SCHEDULED_JOB = "scheduled_job"
    EVENT_HANDLER = "event_handler"
    WEBSOCKET = "websocket"
    IMPORTER = "importer"
    POLLER = "poller"
    CLI_SCRIPT = "cli_script"
    MIGRATION = "migration"
    APP_MODULE = "app_module"
    REPOSITORY_ROOT = "repository_root"


#: Authority roles that mean the surface decides something about what it wrote.
_WRITING_AUTHORITY: Final[frozenset[AuthorityRole]] = frozenset(
    {
        AuthorityRole.DECLARED_OWNER,
        AuthorityRole.PROJECTION_WRITER,
        AuthorityRole.PARALLEL_WRITER,
        AuthorityRole.SCHEMA_LINEAGE,
    }
)

#: Reachability values that cannot touch the production database again.
_SPENT_REACHABILITY: Final[frozenset[Reachability]] = frozenset(
    {Reachability.APPLIED_ONCE, Reachability.NON_PRODUCTION}
)

_DERIVED_CLASSIFICATION: Final[dict[AuthorityRole, SurfaceClassification]] = {
    AuthorityRole.DECLARED_OWNER: SurfaceClassification.AUTHORITATIVE_WRITER,
    AuthorityRole.PROJECTION_WRITER: SurfaceClassification.DERIVED_PROJECTION,
    AuthorityRole.PARALLEL_WRITER: SurfaceClassification.LEGACY_PARALLEL_WRITER,
    AuthorityRole.SCHEMA_LINEAGE: SurfaceClassification.LEGACY_PARALLEL_WRITER,
    AuthorityRole.NO_AUTHORITY: SurfaceClassification.LEGACY_PARALLEL_WRITER,
}

_BOUNDARY_CLASSIFICATION: Final[dict[BoundaryRole, SurfaceClassification]] = {
    BoundaryRole.DELEGATES: SurfaceClassification.AUTHORIZED_ADAPTER,
    BoundaryRole.OBSERVES: SurfaceClassification.OBSERVATION_COLLECTOR,
    BoundaryRole.TRANSPORTS: SurfaceClassification.TRANSPORT,
    BoundaryRole.READS: SurfaceClassification.READ_ONLY_CONSUMER,
}


#: The disposition every *referencing* file falls under unless this inventory
#: gives it one individually.
#:
#: 401 files name a cohort model or table; 47 are inventoried here. Assigning
#: an individual disposition to the other 356 would be fabrication at scale —
#: the reference census is a bounded reach, not an impact analysis, and many of
#: those files only mention a model in a type hint.
#:
#: A class default is still an answer rather than a gap, because there is only
#: one shape available to them. ADR 0012 gives the two applications separate
#: databases, sessions and transactions, so after the switch a file that reads
#: a cohort fact either reaches the target through a versioned contract or
#: stops reading it. Nothing else is permitted, so nothing else can be
#: defaulted to.
#:
#: What the default may NEVER cover is a writer.
#: `test_no_counted_writer_relies_on_the_default_disposition` fails the build
#: if one appears, because "displace this writer" is a decision about a
#: specific line of code and a blanket rule cannot make it.
DEFAULT_CALLER_DISPOSITION: Final[Disposition] = Disposition.REPOINT_TO_TARGET_API


class SourceSurface(BaseModel):
    """One classified file in the cohort-isp-01 source surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Repository-relative path, matching the census key exactly.
    path: str
    family: EntryPointFamily
    authority: AuthorityRole
    boundary: BoundaryRole
    reachability: Reachability
    disposition: Disposition
    #: Cohort entity types this surface touches.
    entity_types: tuple[CohortEntityType, ...]
    #: The owner this surface acts as or bypasses. A SOT registry service name
    #: when `registry_declared`; otherwise a plain description of the
    #: authority it runs under, or `None` when there is none to name.
    owning_service: str | None
    #: Whether `app/services/sot_registry/` declares this module as a service.
    registry_declared: bool
    #: Required exactly when the disposition is `UNDECIDED`, and forbidden
    #: otherwise. A surface nobody has decided about must say what the
    #: decision is, or it reads as one somebody merely forgot to fill in.
    open_question: str | None
    #: Why this classification, in one sentence a reviewer can disagree with.
    note: str

    @model_validator(mode="after")
    def _check(self) -> SourceSurface:
        if not self.note.strip():
            raise ValueError(f"{self.path} carries no classification rationale")
        if not self.entity_types:
            raise ValueError(
                f"{self.path} claims to be a cohort surface but names no entity"
            )
        if len(set(self.entity_types)) != len(self.entity_types):
            raise ValueError(f"{self.path} repeats an entity type")

        writes = self.boundary is BoundaryRole.PERSISTS
        if writes and self.authority is AuthorityRole.NO_AUTHORITY:
            if self.reachability not in _SPENT_REACHABILITY:
                raise ValueError(
                    f"{self.path} persists cohort state against a reachable "
                    "database while claiming no authority over it. A surface "
                    "that writes has authority over what it wrote whether or "
                    "not anybody granted it — say which."
                )
        if not writes and self.authority in _WRITING_AUTHORITY:
            raise ValueError(
                f"{self.path} claims the authority of a writer without "
                f"writing; {self.authority} is only available to a surface "
                "whose boundary role is PERSISTS"
            )
        if self.reachability is Reachability.APPLIED_ONCE and (
            self.authority is not AuthorityRole.SCHEMA_LINEAGE
        ):
            raise ValueError(
                f"{self.path} is reachable only as an applied migration, so "
                "its authority is the schema lineage and nothing else"
            )
        if (
            self.authority is AuthorityRole.DECLARED_OWNER
            and not self.registry_declared
        ):
            raise ValueError(
                f"{self.path} is called a declared owner and the SOT registry "
                "does not declare it. An owner nobody declared is a parallel "
                "writer with a flattering name."
            )
        undetermined = (
            self.authority is AuthorityRole.UNDETERMINED
            or self.boundary is BoundaryRole.UNDETERMINED
            or self.reachability is Reachability.UNDETERMINED
        )
        if undetermined and self.owning_service is not None:
            raise ValueError(
                f"{self.path} leaves an axis undetermined yet names an owner; "
                "one of the two statements is wrong and neither may be guessed"
            )
        if (self.disposition is Disposition.UNDECIDED) != (
            self.open_question is not None
        ):
            raise ValueError(
                f"{self.path} must carry an open question exactly when its "
                "disposition is UNDECIDED — an undecided surface with no "
                "stated question is indistinguishable from an unfinished row, "
                "and a decided one with a question is hiding a live doubt"
            )
        if self.open_question is not None and len(self.open_question.split()) < 8:
            raise ValueError(f"{self.path} states an open question too short to act on")
        return self

    @property
    def writes(self) -> bool:
        """Whether this surface mutates cohort state."""

        return self.boundary is BoundaryRole.PERSISTS

    @property
    def production_runtime(self) -> bool:
        """Whether it can write the production database again.

        False for applied migrations and disposable-database tooling. They
        still count in the ratchet — a *new* one is exactly what the guard
        should catch — but folding them into the production writer count would
        overstate what a cutover has to displace, and none of them can be
        "retired".
        """

        return self.reachability not in _SPENT_REACHABILITY

    @property
    def classification(self) -> SurfaceClassification:
        """The original eight-member vocabulary, derived from the axes."""

        if (
            self.authority is AuthorityRole.UNDETERMINED
            or self.boundary is BoundaryRole.UNDETERMINED
        ):
            return SurfaceClassification.UNKNOWN
        if self.boundary is BoundaryRole.PERSISTS:
            return _DERIVED_CLASSIFICATION[self.authority]
        return _BOUNDARY_CLASSIFICATION[self.boundary]


COHORT_SURFACES: Final[tuple[SourceSurface, ...]] = (
    SourceSurface(
        path="app/api/crm_webhooks.py",
        family=EntryPointFamily.WEBHOOK_HANDLER,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.TRANSPORTS,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "The one inbound provider callback carrying customer identity. It "
            "terminates the request, verifies it, and hands the payload to "
            "`crm_customers.observe_customer`; it names no cohort model and writes no "
            "cohort row. Inventoried because a webhook writer is the hardest kind to "
            "notice by reading code — nothing in this repository calls one."
        ),
    ),
    SourceSurface(
        path="app/api/me.py",
        family=EntryPointFamily.API_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "The authenticated self-service API; reads the account and delegates "
            "writes."
        ),
    ),
    SourceSurface(
        path="app/api/reseller.py",
        family=EntryPointFamily.API_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "The reseller-scoped API; reaches accounts only through owning services."
        ),
    ),
    SourceSurface(
        path="app/api/subscribers.py",
        family=EntryPointFamily.API_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "The JSON account API. Imports the model for typing and delegates every "
            "mutation."
        ),
    ),
    SourceSurface(
        path="app/services/account_deletion.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "Writes the account's `metadata` blob directly rather than through "
            "customer.accounts."
        ),
    ),
    SourceSurface(
        path="app/services/account_lifecycle.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PROJECTION_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="access.subscription_lifecycle",
        registry_declared=True,
        open_question=None,
        note=(
            "Writes the Subscriber lifecycle projection and its override columns. The "
            "customer_context registry already names this a projection of canonical "
            "subscription lifecycle state, so the account row is downstream of a "
            "decision made elsewhere — the export must carry it as derived, never as "
            "the account's own status."
        ),
    ),
    SourceSurface(
        path="app/services/billing_cleanup_remediation.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "A billing remediation path sets `subscribers.billing_mode` without the "
            "account owner."
        ),
    ),
    SourceSurface(
        path="app/services/brand_profiles.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.BRAND_PROFILE,),
        owning_service="customer.branding",
        registry_declared=True,
        open_question=None,
        note=(
            "The declared owner of platform, reseller and organization brand profiles."
        ),
    ),
    SourceSurface(
        path="app/services/crm_customer_name_repair.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.name_remediation",
        registry_declared=True,
        open_question=None,
        note=("Declared owner of the reviewed legacy customer-name repair manifest."),
    ),
    SourceSurface(
        path="app/services/crm_customers.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.READS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "Interprets a verified CRM observation by matching it to an existing "
            "account through exact retained provenance. It reads `Subscriber`, "
            "creates and updates nothing, and completes no transaction — its own "
            "docstring says so and the census agrees."
        ),
    ),
    SourceSurface(
        path="app/services/crm_portal.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="party.registry",
        registry_declared=False,
        open_question=None,
        note=(
            "Stamps `subscribers.crm_subscriber_id` in place. External "
            "identity-reference provenance is declared to party.registry and belongs "
            "in `party_external_references`, so this is a second provenance store on "
            "the authoritative row."
        ),
    ),
    SourceSurface(
        path="app/services/crm_ticket_pull.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.BACKGROUND_JOB,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="party.registry",
        registry_declared=False,
        open_question=None,
        note=(
            "A ticket importer that also writes the customer account's CRM provenance "
            "id. Collecting an observation is legitimate; writing it onto the "
            "authoritative row is what makes it parallel."
        ),
    ),
    SourceSurface(
        path="app/services/customer_canonical_profile_patch.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.canonical_profile_patch",
        registry_declared=True,
        open_question=None,
        note=(
            "The registered flush-only participant applies an explicit typed field "
            "set to an existing canonical Customer and service address inside its "
            "coordinator's transaction."
        ),
    ),
    SourceSurface(
        path="app/services/customer_location_requests.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "Writes the account `metadata` blob from an undeclared module, bypassing "
            "`customer.accounts`, which "
            "`docs/designs/SUBSCRIBER_SERVICE_LOCATION_SOT.md` names as the owner of "
            "the service Address's identity and text. An earlier version of this note "
            "said addresses had no declared owner at all; that was wrong, and it made "
            "a plain bypass look like unowned debt nobody could route around. "
            "The address half is closed as of 2026-08-22: this module now calls "
            "`customer.accounts.create_address` and `gis.spatial_sync"
            ".project_address_point` rather than constructing `Address` rows and "
            "projections itself. The `metadata` blob is what is left. "
            "Decided 2026-08-21 (dec-isp-007): the target owner is a product-first "
            "`dotmac-addresses`, extracted from `customer.accounts` plus "
            "`gis.spatial_sync`, `customer.location_capture` and "
            "`customer.location_verification`."
        ),
    ),
    SourceSurface(
        path="app/services/customer_portal_contacts.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_CONTACT,),
        owning_service="party.registry",
        registry_declared=False,
        open_question=None,
        note=(
            "Creates and edits legacy `subscriber_contacts` rows from the customer "
            "portal. party.registry owns the canonical contact-point lifecycle these "
            "rows are projected into."
        ),
    ),
    SourceSurface(
        path="app/services/customer_portal_notifications.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=True,
        open_question=None,
        note=(
            "Declared as communications.customer_read_state, and stores that read "
            "state inside the customer account's `metadata` blob. A declared owner "
            "writing another owner's row is still a parallel writer of that row."
        ),
    ),
    SourceSurface(
        path="app/services/mrr_snapshot.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PROJECTION_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.BACKGROUND_JOB,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "Writes `subscribers.mrr_total`, a money figure derived from subscription "
            "state, from a module the registry does not declare. Decided 2026-08-21: "
            "the column does NOT migrate — the target recomputes monthly recurring "
            "revenue from its own Subscriptions — so this writer is displaced with "
            "the rest of cohort 1 rather than reproduced. The export still carries "
            "the value as a declared derived field, because a reconciliation that "
            "cannot see it cannot explain a difference; carrying it is not the same "
            "as migrating it."
        ),
    ),
    SourceSurface(
        path="app/services/network_subscriber_bridge.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "Constructs `Subscriber` rows from a provisioning bridge instead of the "
            "account owner."
        ),
    ),
    SourceSurface(
        path="app/services/nin_verifications.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("Writes NIN verification outcome into the account `metadata` blob."),
    ),
    SourceSurface(
        path="app/services/party.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(
            CohortEntityType.PARTY,
            CohortEntityType.PARTY_ROLE,
            CohortEntityType.PARTY_RELATIONSHIP,
            CohortEntityType.PARTY_MEMBERSHIP,
            CohortEntityType.PARTY_CONTACT_POINT,
            CohortEntityType.PARTY_EXTERNAL_REFERENCE,
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_CONTACT,
        ),
        owning_service="party.registry",
        registry_declared=True,
        open_question=None,
        note=(
            "The declared native identity owner. Its writes to `subscribers` and "
            "`subscriber_contacts` are the canonical Party binding columns, not the "
            "legacy identity columns beside them."
        ),
    ),
    SourceSurface(
        path="app/services/gis_sync.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ADDRESS,),
        owning_service="gis.spatial_sync",
        registry_declared=True,
        open_question=None,
        note=(
            "The declared owner of an address coordinate and its map projection. It "
            "appears in the census for the first time in this change, and that is the "
            "point: it previously offered only committing full-table sweeps with the "
            "real write in a private helper, so three callers wrote the coordinate "
            "themselves and one of them wrote it with the axes reversed. "
            "`project_address_point` is the flush-only per-address operation they "
            "now call. A census counts files, not owners — membership rose while the "
            "surface shrank."
        ),
    ),
    SourceSurface(
        path="app/services/subscriber.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.accounts",
        registry_declared=True,
        open_question=None,
        note=(
            "The declared owner of Subscriber account creation. Its own SOT note "
            "already records that existing direct writers elsewhere are shrink-only "
            "migration debt."
        ),
    ),
    SourceSurface(
        path="app/services/subscriber_profile_cleanup.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.profile_cleanup",
        registry_declared=True,
        open_question=None,
        note=("Declared owner of evidence-bound gender and date-of-birth repair."),
    ),
    SourceSurface(
        path="app/services/team_inbox_commands.py",
        family=EntryPointFamily.SERVICE,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.PARTY_RELATIONSHIP,),
        owning_service="party.registry",
        registry_declared=True,
        open_question=None,
        note=(
            "Constructs `PartyRelationship` rows from the team inbox. party.registry "
            "is declared to own directional relationships and their effective-date "
            "contract."
        ),
    ),
    SourceSurface(
        path="app/services/web_admin_resellers.py",
        family=EntryPointFamily.WEB_PRESENTER,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=True,
        open_question=None,
        note=(
            "Declared as ui.reseller_list_projection — a list projection — and "
            "assigns `subscribers.reseller_id`. Account ownership is not a "
            "list-rendering concern."
        ),
    ),
    SourceSurface(
        path="app/services/web_customer_actions.py",
        family=EntryPointFamily.WEB_PRESENTER,
        authority=AuthorityRole.DECLARED_OWNER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.profile_commands",
        registry_declared=True,
        open_question=None,
        note=(
            "Named like a presenter, declared as an owner. Kept in the "
            "`web_presenter` family because that is where it lives and how a reader "
            "will find it, and classified as an owner because the registry says so — "
            "family is location, classification is authority, and conflating them is "
            "how a real owner gets mistaken for a stray adapter."
        ),
    ),
    SourceSurface(
        path="app/services/web_customer_details.py",
        family=EntryPointFamily.WEB_PRESENTER,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "An undeclared detail presenter initialises the account `metadata` blob "
            "in place."
        ),
    ),
    SourceSurface(
        path="app/services/web_system_import_wizard.py",
        family=EntryPointFamily.WEB_PRESENTER,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("An admin import wizard constructs `Subscriber` rows directly."),
    ),
    SourceSurface(
        path="app/services/web_system_restore_tool.py",
        family=EntryPointFamily.WEB_PRESENTER,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.INTERNAL_ONLY,
        disposition=Disposition.ROUTE_THROUGH_OWNER_FIRST,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "A restore tool reactivates accounts and rewrites their `metadata` blob. "
            "Decided 2026-08-21: Customers owns recovery INTENT and the existing "
            "cross-domain cascade is decomposed, so this tool stops reaching into "
            "account rows and asks the account owner to restore instead. Decomposing "
            "the cascade is what makes the cohort-1 half separable at all — today one "
            "restore touches invoices, payments, credentials, RADIUS, IP assignments "
            "and ONT assignments in the same pass, and only the account rows are "
            "cohort 1."
        ),
    ),
    SourceSurface(
        path="app/tasks/nin_tasks.py",
        family=EntryPointFamily.TASK_WORKER,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.BACKGROUND_JOB,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "The NIN verification worker. It persists verification rows, which are "
            "outside this cohort, and reaches account state only through "
            "`nin_verifications`."
        ),
    ),
    SourceSurface(
        path="app/web/admin/billing_payments.py",
        family=EntryPointFamily.WEB_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.READS,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "Reads account rows to render payment screens; writes billing rows only."
        ),
    ),
    SourceSurface(
        path="app/web/admin/customers.py",
        family=EntryPointFamily.WEB_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("The admin customer portal; owns the transaction, decides nothing."),
    ),
    SourceSurface(
        path="app/web/admin/system.py",
        family=EntryPointFamily.WEB_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=(
            "Admin system tooling; delegates account changes to presenters and "
            "services."
        ),
    ),
    SourceSurface(
        path="app/web/customer/location.py",
        family=EntryPointFamily.WEB_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.location_capture",
        registry_declared=False,
        open_question=None,
        note=(
            "Portal location confirmation; calls location capture and owns only the "
            "commit."
        ),
    ),
    SourceSurface(
        path="app/web/customer/routes.py",
        family=EntryPointFamily.WEB_ROUTE,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.DELEGATES,
        reachability=Reachability.ONLINE_REQUEST,
        disposition=Disposition.REPOINT_TO_TARGET_API,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("The customer portal shell; delegates every account mutation."),
    ),
    SourceSurface(
        path="scripts/migration/backfill_crm_subscriber_links.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.OPERATOR_COMMAND,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("A one-off backfill issuing raw DML against `subscribers`."),
    ),
    SourceSurface(
        path="scripts/migration/backfill_party_status.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.OPERATOR_COMMAND,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.PARTY,
        ),
        owning_service="party.registry",
        registry_declared=False,
        open_question=None,
        note=("A one-off backfill issuing raw DML against party and account rows."),
    ),
    SourceSurface(
        path="scripts/migration/import_crm_phase3.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.OPERATOR_COMMAND,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("A CRM import phase writing `subscribers` directly."),
    ),
    SourceSurface(
        path="scripts/migration/kernel_lineage_rehearsal_canaries.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.NON_PRODUCTION,
        disposition=Disposition.NON_PRODUCTION_NO_ACTION,
        entity_types=(
            CohortEntityType.PARTY,
            CohortEntityType.PARTY_ROLE,
            CohortEntityType.CUSTOMER_ACCOUNT,
        ),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "Builds synthetic canaries inside a disposable rehearsal database. "
            "Counted so the ratchet still sees it, marked non-production so it does "
            "not inflate the writer surface a cutover has to displace."
        ),
    ),
    SourceSurface(
        path="scripts/one_off/backfill_crm_subscriber_ids.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.PARALLEL_WRITER,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.OPERATOR_COMMAND,
        disposition=Disposition.RETIRE_AFTER_CUTOVER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        open_question=None,
        note=("A one-off script stamping CRM provenance ids onto account rows."),
    ),
    SourceSurface(
        path="scripts/seed/seed_test_fixtures.py",
        family=EntryPointFamily.CLI_SCRIPT,
        authority=AuthorityRole.NO_AUTHORITY,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.NON_PRODUCTION,
        disposition=Disposition.NON_PRODUCTION_NO_ACTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        open_question=None,
        note=(
            "A fixture seeder for test databases; same treatment as the rehearsal "
            "canaries."
        ),
    ),
    SourceSurface(
        path="alembic/versions/045_contact_channels_without_name.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.CUSTOMER_CONTACT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "An applied data migration wrote contact rows outside the owning service. "
            "Alembic owns the deployed schema, so this ran under a real authority — "
            "but it wrote a fact a service owns, and it cannot run again against a "
            "migrated database, which is why it is counted and not counted as "
            "production."
        ),
    ),
    SourceSurface(
        path="alembic/versions/116_add_billing_accounts.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "An applied data migration over `subscribers`; applied, so it cannot run "
            "again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/208_map_karu_customers_to_bts.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "An applied one-off remap of a named customer cohort; applied, so it "
            "cannot run again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/267_brand_profiles.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.BRAND_PROFILE,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "The migration that created and seeded `brand_profiles`, before "
            "`customer.branding` existed to own them."
        ),
    ),
    SourceSurface(
        path="alembic/versions/277_lifecycle_communications_sot.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "An applied lifecycle-communications migration touching account rows; "
            "applied, so it cannot run again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/383_replaceable_backoffice_boundary.py",
        family=EntryPointFamily.MIGRATION,
        authority=AuthorityRole.SCHEMA_LINEAGE,
        boundary=BoundaryRole.PERSISTS,
        reachability=Reachability.APPLIED_ONCE,
        disposition=Disposition.HISTORICAL_NO_ACTION,
        entity_types=(CohortEntityType.ORGANIZATION,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        open_question=None,
        note=(
            "An applied migration writing back-office reference columns on "
            "`organizations`; applied, so it cannot run again."
        ),
    ),
)


class UnmappedAdjacentTable(BaseModel):
    """A cohort-adjacent Sub table deliberately outside the export contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    reason: str

    @model_validator(mode="after")
    def _check(self) -> UnmappedAdjacentTable:
        if len(self.reason.split()) < 4:
            raise ValueError(
                f"{self.table} is excluded without a reason a reviewer could "
                "disagree with; an unexplained exclusion is indistinguishable "
                "from an oversight"
            )
        return self


#: Tables a reader might expect in "party and customer" and will not find.
#: Recorded so their absence is a decision on the record rather than a gap
#: someone discovers at cutover.
UNMAPPED_ADJACENT_TABLES: Final[tuple[UnmappedAdjacentTable, ...]] = (
    UnmappedAdjacentTable(
        table="resellers",
        reason=(
            "Reseller Management is Governance cohort 7; exporting the channel "
            "record with cohort 1 would move a later cohort's fact early."
        ),
    ),
    UnmappedAdjacentTable(
        table="reseller_users",
        reason="Same cohort-7 boundary as `resellers`, plus it carries login identity.",
    ),
    UnmappedAdjacentTable(
        table="customer_identity_index",
        reason=(
            "A rebuildable search index over identity, not a source fact; the "
            "target rebuilds its own from imported parties."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_channels",
        reason="Communication channel preference belongs with cohort 6 communications.",
    ),
    UnmappedAdjacentTable(
        table="subscriber_nin_verifications",
        reason=(
            "Regulatory identity-verification evidence. Decided 2026-08-21: it "
            "takes a Compliance/Records retention disposition rather than a "
            "cohort-1 migration one, so it stays out of this export and is "
            "governed by retention policy. The cohort exports only a presence "
            "flag on the account; the evidence itself never crosses."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_custom_fields",
        reason=(
            "Operator-defined fields with no declared schema; exporting them "
            "typed would invent a contract nobody owns."
        ),
    ),
    UnmappedAdjacentTable(
        table="carried_source_identity_adjudications",
        reason=(
            "Reviewed adjudication decisions about carried identity, not "
            "identity itself; they describe the migration rather than migrate."
        ),
    ),
    UnmappedAdjacentTable(
        table="party_identity_backfill_receipts",
        reason=(
            "PII-free receipts proving a past backfill replayed idempotently; "
            "evidence about the source, not source state."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_contact_relationship_projections",
        reason=(
            "A reviewed projection from legacy contacts into party "
            "relationships; the target rebuilds it from the exported originals."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_contact_point_projections",
        reason="Same projection rationale as the relationship projections beside it.",
    ),
)


class NoCountedWriter(BaseModel):
    """A cohort table the census found no writer for, and how hard it looked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    #: Stated so the empty result is a bounded claim rather than a conclusion.
    searched: str


#: Cohort tables with zero counted writers. This is a *searched* result, and
#: it is not UNKNOWN: construction, tracked mutation, set-based DML and raw
#: statements were all looked for across every scanned family.
#:
#: The census's blind spots, stated once so they are not rediscovered as a
#: surprise: a mutation through a local this module could not bind to a cohort
#: model, a generic `setattr` helper, a SQLAlchemy event hook, and any writer
#: living outside the scanned roots. None of those would be caught, which is
#: why this reads "no counted writer" rather than "no writer".
TABLES_WITH_NO_COUNTED_WRITER: Final[tuple[NoCountedWriter, ...]] = (
    NoCountedWriter(
        table="organizations",
        searched=(
            "No `Organization(...)` construction, tracked mutation, set-based "
            "DML or raw statement anywhere under app/, scripts/ or the "
            "executable migration lineage, apart from one applied migration "
            "writing back-office reference columns."
        ),
    ),
    NoCountedWriter(
        table="organization_memberships",
        searched=(
            "No construction, tracked mutation, set-based DML or raw statement "
            "in any scanned family."
        ),
    ),
)


def surfaces_by_classification() -> dict[SurfaceClassification, tuple[str, ...]]:
    """Group inventoried paths by the derived eight-member classification."""

    grouped: dict[SurfaceClassification, list[str]] = {
        classification: [] for classification in SurfaceClassification
    }
    for surface in COHORT_SURFACES:
        grouped[surface.classification].append(surface.path)
    return {
        classification: tuple(sorted(paths))
        for classification, paths in grouped.items()
    }


def surfaces_by_authority() -> dict[AuthorityRole, tuple[str, ...]]:
    """Group inventoried paths by what say they have over the fact."""

    grouped: dict[AuthorityRole, list[str]] = {role: [] for role in AuthorityRole}
    for surface in COHORT_SURFACES:
        grouped[surface.authority].append(surface.path)
    return {role: tuple(sorted(paths)) for role, paths in grouped.items()}


def surfaces_by_boundary() -> dict[BoundaryRole, tuple[str, ...]]:
    """Group inventoried paths by what they do at the boundary."""

    grouped: dict[BoundaryRole, list[str]] = {role: [] for role in BoundaryRole}
    for surface in COHORT_SURFACES:
        grouped[surface.boundary].append(surface.path)
    return {role: tuple(sorted(paths)) for role, paths in grouped.items()}


def surfaces_by_reachability() -> dict[Reachability, tuple[str, ...]]:
    """Group inventoried paths by how they can still be reached."""

    grouped: dict[Reachability, list[str]] = {reach: [] for reach in Reachability}
    for surface in COHORT_SURFACES:
        grouped[surface.reachability].append(surface.path)
    return {reach: tuple(sorted(paths)) for reach, paths in grouped.items()}


def surfaces_by_disposition() -> dict[Disposition, tuple[str, ...]]:
    """Group inventoried paths by what happens to them when authority moves."""

    grouped: dict[Disposition, list[str]] = {
        disposition: [] for disposition in Disposition
    }
    for surface in COHORT_SURFACES:
        grouped[surface.disposition].append(surface.path)
    return {disposition: tuple(sorted(paths)) for disposition, paths in grouped.items()}


def undecided_surfaces() -> tuple[SourceSurface, ...]:
    """Every surface whose disposition is still an open question.

    Enumerable on purpose. These are inputs to `ctl-isp-006`, and a question
    that lives only in prose is one somebody answers by accident.
    """

    return tuple(
        sorted(
            (
                surface
                for surface in COHORT_SURFACES
                if surface.disposition is Disposition.UNDECIDED
            ),
            key=lambda surface: surface.path,
        )
    )


def displaced_writer_paths() -> tuple[str, ...]:
    """Production writers a cohort cutover has to displace.

    The number `ctl-isp-009` ratchets to zero: everything that persists cohort
    state and can still reach the production database.
    """

    return tuple(
        sorted(
            surface.path
            for surface in COHORT_SURFACES
            if surface.writes and surface.production_runtime
        )
    )


def production_writer_paths() -> tuple[str, ...]:
    """Retained name for `displaced_writer_paths`.

    Documents and an earlier control record cite this name; keeping it as an
    alias costs one line and avoids a rename that means nothing.
    """

    return displaced_writer_paths()


def inventoried_paths() -> frozenset[str]:
    """Every path this inventory classifies."""

    return frozenset(surface.path for surface in COHORT_SURFACES)


__all__ = [
    "COHORT_SURFACES",
    "DEFAULT_CALLER_DISPOSITION",
    "TABLES_WITH_NO_COUNTED_WRITER",
    "UNMAPPED_ADJACENT_TABLES",
    "AuthorityRole",
    "BoundaryRole",
    "Disposition",
    "EntryPointFamily",
    "NoCountedWriter",
    "Reachability",
    "SourceSurface",
    "SurfaceClassification",
    "UnmappedAdjacentTable",
    "displaced_writer_paths",
    "inventoried_paths",
    "production_writer_paths",
    "surfaces_by_authority",
    "surfaces_by_boundary",
    "surfaces_by_classification",
    "surfaces_by_disposition",
    "surfaces_by_reachability",
    "undecided_surfaces",
]
