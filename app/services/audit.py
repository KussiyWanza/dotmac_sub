import logging
from dataclasses import asdict, dataclass

from fastapi import HTTPException, Request, Response
from sqlalchemy import or_, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import DetachedInstanceError

from app.models.audit import AuditActorType, AuditEvent
from app.schemas.audit import AuditEventCreate
from app.services.common import apply_ordering, apply_pagination
from app.services.response import ListResponseMixin
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditR1ParityReport:
    """Aggregate-only evidence for the kernel audit R1 dual-write."""

    total_rows: int
    historical_rows_without_created_at: int
    r1_rows: int
    missing_details: int
    metadata_mismatches: int
    ip_address_mismatches: int
    user_agent_mismatches: int
    unknown_actor_types: int
    missing_required_actor_ids: int

    @property
    def blocking_mismatches(self) -> int:
        return sum(
            (
                self.missing_details,
                self.metadata_mismatches,
                self.ip_address_mismatches,
                self.user_agent_mismatches,
                self.unknown_actor_types,
                self.missing_required_actor_ids,
            )
        )

    @property
    def status(self) -> str:
        if self.blocking_mismatches:
            return "drift"
        if not self.r1_rows:
            return "no_r1_rows"
        return "parity"

    def as_dict(self) -> dict[str, int | str]:
        return {
            **asdict(self),
            "blocking_mismatches": self.blocking_mismatches,
            "status": self.status,
        }

    @classmethod
    def from_mapping(cls, row: RowMapping) -> "AuditR1ParityReport":
        return cls(
            total_rows=int(row["total_rows"]),
            historical_rows_without_created_at=int(
                row["historical_rows_without_created_at"]
            ),
            r1_rows=int(row["r1_rows"]),
            missing_details=int(row["missing_details"]),
            metadata_mismatches=int(row["metadata_mismatches"]),
            ip_address_mismatches=int(row["ip_address_mismatches"]),
            user_agent_mismatches=int(row["user_agent_mismatches"]),
            unknown_actor_types=int(row["unknown_actor_types"]),
            missing_required_actor_ids=int(row["missing_required_actor_ids"]),
        )


_AUDIT_R1_PARITY_QUERY = text(
    """
    SELECT
        count(*) AS total_rows,
        count(*) FILTER (WHERE created_at IS NULL)
            AS historical_rows_without_created_at,
        count(*) FILTER (WHERE created_at IS NOT NULL) AS r1_rows,
        count(*) FILTER (
            WHERE created_at IS NOT NULL AND details IS NULL
        ) AS missing_details,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND NOT (
                  (details - ARRAY['ip_address', 'user_agent'])
                  @> (COALESCE(metadata::jsonb, '{}'::jsonb)
                      - ARRAY['ip_address', 'user_agent'])
              )
        ) AS metadata_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND (
                  (ip_address IS NOT NULL
                   AND details ->> 'ip_address' IS DISTINCT FROM ip_address)
                  OR (ip_address IS NULL AND details ? 'ip_address')
              )
        ) AS ip_address_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND (
                  (user_agent IS NOT NULL
                   AND details ->> 'user_agent' IS DISTINCT FROM user_agent)
                  OR (user_agent IS NULL AND details ? 'user_agent')
              )
        ) AS user_agent_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND (
                  actor_type IS NULL
                  OR actor_type::text NOT IN ('system', 'user', 'api_key', 'service')
              )
        ) AS unknown_actor_types,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND actor_type::text IN ('user', 'api_key', 'service')
              AND (actor_id IS NULL OR btrim(actor_id) = '')
        ) AS missing_required_actor_ids
    FROM audit_events
    """
)


class AuditEvents(ListResponseMixin):
    @staticmethod
    def _event_data(payload: AuditEventCreate) -> dict[str, object]:
        """Build the one persistence shape used by every audit writer.

        R1 keeps ``metadata`` as Sub's readable legacy surface while
        dual-writing the kernel's ``details`` JSONB. Column values win over
        same-named JSON keys so parity remains deterministic and repairable.
        """

        data = payload.model_dump()
        if payload.occurred_at is None:
            data.pop("occurred_at", None)

        details = dict(payload.metadata_ or {})
        details.update(payload.details or {})
        if payload.ip_address is not None:
            details["ip_address"] = payload.ip_address
        else:
            details.pop("ip_address", None)
        if payload.user_agent is not None:
            details["user_agent"] = payload.user_agent
        else:
            details.pop("user_agent", None)
        data["details"] = details
        return data

    @classmethod
    def _build_event(cls, payload: AuditEventCreate) -> AuditEvent:
        """The only application constructor for ``AuditEvent`` rows."""

        return cls._build_event_from_data(cls._event_data(payload))

    @staticmethod
    def _build_event_from_data(data: dict[str, object]) -> AuditEvent:
        return AuditEvent(**data)

    @staticmethod
    def parse_actor_type(value: str | None) -> AuditActorType | None:
        if value is None:
            return None
        try:
            return AuditActorType(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(a.value for a in AuditActorType))
            raise HTTPException(
                status_code=400,
                detail=f"Invalid actor_type. Allowed: {allowed}",
            ) from exc

    @staticmethod
    def r1_parity(db: Session) -> AuditR1ParityReport:
        """Measure R1 drift without retrieving forensic row content."""

        row = db.execute(_AUDIT_R1_PARITY_QUERY).mappings().one()
        return AuditR1ParityReport.from_mapping(row)

    @staticmethod
    def create(db: Session, payload: AuditEventCreate) -> AuditEvent:
        event = AuditEvents._build_event(payload)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def record(
        db: Session,
        payload: AuditEventCreate,
        *,
        defer_until_commit: bool = True,
    ) -> AuditEvent:
        """Record an audit event. Alias for create with optional deferred commit."""
        data = AuditEvents._event_data(payload)
        if not defer_until_commit:
            event = AuditEvents._build_event_from_data(data)
            db.add(event)
            db.commit()
            db.refresh(event)
            return event

        pending_event = AuditEvents._build_event_from_data(data)

        def _persist(callback_db: Session) -> None:
            event = AuditEvents._build_event_from_data(data)
            callback_db.add(event)
            callback_db.commit()

        run_after_commit(db, _persist)
        return pending_event

    @staticmethod
    def stage(db: Session, payload: AuditEventCreate) -> AuditEvent:
        """Stage an audit row in the caller's current transaction."""
        event = AuditEvents._build_event(payload)
        db.add(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(AuditEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Audit event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        actor_id: str | None = None,
        actor_search: str | None = None,
        actor_type: AuditActorType | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        request_id: str | None = None,
        is_success: bool | None = None,
        status_code: int | None = None,
        is_active: bool | None = None,
        order_by: str = "occurred_at",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ):
        stmt = select(AuditEvent)
        if actor_id:
            stmt = stmt.where(AuditEvent.actor_id == actor_id)
        if actor_search:
            # Search by the resolved person/key label. Also matches an exact
            # actor_id so the field accepts either a name or a pasted uuid.
            term = actor_search.strip()
            if term:
                stmt = stmt.where(
                    or_(
                        AuditEvent.actor_label.ilike(f"%{term}%"),
                        AuditEvent.actor_id == term,
                    )
                )
        if actor_type:
            stmt = stmt.where(AuditEvent.actor_type == actor_type)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        if entity_type:
            stmt = stmt.where(AuditEvent.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(AuditEvent.entity_id == entity_id)
        if request_id:
            stmt = stmt.where(AuditEvent.request_id == request_id)
        if is_success is not None:
            stmt = stmt.where(AuditEvent.is_success == is_success)
        if status_code is not None:
            stmt = stmt.where(AuditEvent.status_code == status_code)
        if is_active is None:
            stmt = stmt.where(AuditEvent.is_active.is_(True))
        else:
            stmt = stmt.where(AuditEvent.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "occurred_at": AuditEvent.occurred_at,
                "action": AuditEvent.action,
                "entity_type": AuditEvent.entity_type,
                "status_code": AuditEvent.status_code,
            },
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def log_request(db: Session, request: Request, response: Response):
        payload = AuditEvents.build_request_payload(request, response)
        event = AuditEvents._build_event(payload)
        db.add(event)
        db.commit()

    @staticmethod
    def build_request_payload(request: Request, response: Response) -> AuditEventCreate:
        actor_type = request.headers.get("x-actor-type")
        actor_id = request.headers.get("x-actor-id")
        if not actor_type:
            actor_type = getattr(request.state, "actor_type", None)
        if not actor_id:
            actor_id = getattr(request.state, "actor_id", None)
        if not actor_type:
            actor_type = AuditActorType.system.value
        request_id = request.headers.get("x-request-id")
        entity_id = request.headers.get("x-entity-id")
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            resolved_actor_type = AuditActorType(actor_type)
        except ValueError:
            resolved_actor_type = AuditActorType.system
        try:
            query_params = dict(request.query_params)
        except KeyError:
            query_params = {}
        sensitive = {"token", "password", "secret", "api_key", "api_token"}
        metadata = {
            "path": request.url.path,
            "query": {
                key: "<redacted>" if key.lower() in sensitive else value
                for key, value in query_params.items()
            },
        }
        person = getattr(request.state, "user", None)
        if person:
            # request.state.user can be detached by middleware ordering; read only
            # already-loaded values to avoid lazy-loading on a dead Session.
            state = getattr(person, "__dict__", {})
            try:
                first_name = (state.get("first_name") or "").strip()
                last_name = (state.get("last_name") or "").strip()
                display_name = (state.get("display_name") or "").strip()
                if not display_name:
                    display_name = f"{first_name} {last_name}".strip()
                email = state.get("email")
            except DetachedInstanceError:
                display_name = None
                email = None
            except Exception:
                display_name = None
                email = None
            if display_name:
                metadata["actor_name"] = display_name
            if email:
                metadata["actor_email"] = email
        payload = AuditEventCreate(
            actor_type=resolved_actor_type,
            actor_id=actor_id,
            action=request.method,
            entity_type=request.url.path,
            entity_id=entity_id,
            status_code=response.status_code,
            is_success=response.status_code < 400,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            metadata_=metadata,
        )
        return payload

    @staticmethod
    def delete(db: Session, event_id: str):
        event = db.get(AuditEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Audit event not found")
        event.is_active = False
        db.commit()


audit_events = AuditEvents()
