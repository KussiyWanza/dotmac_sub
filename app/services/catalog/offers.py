"""Offer management services.

Provides services for Offers, OfferPrices, OfferVersions, and OfferVersionPrices.
"""

import logging
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

import app.services.catalog.offer_access_requirement as offer_access_requirement
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    OfferAddOn,
    OfferPrice,
    OfferRadiusProfile,
    OfferStatus,
    OfferVersion,
    OfferVersionPrice,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferVersionCreate,
    OfferVersionPriceCreate,
    OfferVersionPriceUpdate,
    OfferVersionUpdate,
)
from app.services import catalog_billing_governance as billing_governance
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.crud import CRUDManager
from app.services.owner_commands import CommandContext
from app.services.query_builders import apply_active_state, apply_optional_equals

logger = logging.getLogger(__name__)


class Offers(CRUDManager[CatalogOffer]):
    model = CatalogOffer
    not_found_detail = "Offer not found"

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Return catalog dashboard KPIs, charts, and popular plans."""
        total_count = db.query(func.count(CatalogOffer.id)).scalar() or 0
        active_count = (
            db.query(func.count(CatalogOffer.id))
            .filter(CatalogOffer.status == OfferStatus.active)
            .scalar()
            or 0
        )
        archived_count = (
            db.query(func.count(CatalogOffer.id))
            .filter(CatalogOffer.status == OfferStatus.archived)
            .scalar()
            or 0
        )

        total_subscriptions = (
            db.query(func.count(Subscription.id))
            .filter(Subscription.status == SubscriptionStatus.active)
            .scalar()
            or 0
        )

        # Subscriptions by plan (top 10)
        plan_rows = db.execute(
            select(CatalogOffer.name, func.count(Subscription.id))
            .join(Subscription, Subscription.offer_id == CatalogOffer.id)
            .where(Subscription.status == SubscriptionStatus.active)
            .group_by(CatalogOffer.id, CatalogOffer.name)
            .order_by(func.count(Subscription.id).desc())
            .limit(10)
        ).all()
        subscriptions_by_plan = {
            "labels": [row[0] for row in plan_rows],
            "values": [row[1] for row in plan_rows],
        }

        # Recent offers (last 10)
        recent_offers = (
            db.query(CatalogOffer)
            .order_by(CatalogOffer.created_at.desc())
            .limit(10)
            .all()
        )

        # Status chart
        status_rows = db.execute(
            select(CatalogOffer.status, func.count(CatalogOffer.id)).group_by(
                CatalogOffer.status
            )
        ).all()
        status_map = {
            row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
            for row in status_rows
        }
        chart_labels = ["Active", "Inactive", "Archived", "Draft"]
        chart_keys = ["active", "inactive", "archived", "draft"]
        chart_colors = ["#10b981", "#f59e0b", "#94a3b8", "#64748b"]
        chart_data = {
            "labels": chart_labels,
            "values": [status_map.get(k, 0) for k in chart_keys],
            "colors": chart_colors,
        }

        return {
            "total_count": total_count,
            "active_count": active_count,
            "archived_count": archived_count,
            "total_subscriptions": total_subscriptions,
            "subscriptions_by_plan": subscriptions_by_plan,
            "chart_data": chart_data,
            "recent_offers": recent_offers,
        }

    @staticmethod
    def create(
        db: Session,
        payload: CatalogOfferCreate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "billing_cycle" not in fields_set:
            default_billing_cycle = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_billing_cycle"
            )
            if default_billing_cycle:
                data["billing_cycle"] = validate_enum(
                    default_billing_cycle, BillingCycle, "billing_cycle"
                )
        if "billing_mode" not in fields_set:
            default_billing_mode = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_billing_mode"
            )
            if default_billing_mode:
                data["billing_mode"] = validate_enum(
                    default_billing_mode, BillingMode, "billing_mode"
                )
        if "contract_term" not in fields_set:
            default_contract_term = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_contract_term"
            )
            if default_contract_term:
                data["contract_term"] = validate_enum(
                    default_contract_term, ContractTerm, "contract_term"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_offer_status"
            )
            if default_status:
                data["status"] = validate_enum(default_status, OfferStatus, "status")
        offer = CatalogOffer(**data)
        db.add(offer)
        db.flush()
        billing_governance.stage_billing_catalog_change(
            db,
            action="created",
            entity_type="catalog_offer",
            entity_id=offer.id,
            changes=data,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=offer.id,
        )
        db.commit()
        db.refresh(offer)
        return offer

    @classmethod
    def get(cls, db: Session, offer_id: str):
        offer = db.get(
            CatalogOffer,
            offer_id,
            options=[
                selectinload(CatalogOffer.region_zone),
                selectinload(CatalogOffer.usage_allowance),
                selectinload(CatalogOffer.sla_profile),
                selectinload(CatalogOffer.policy_set),
                selectinload(CatalogOffer.prices),
                selectinload(CatalogOffer.add_on_links).selectinload(OfferAddOn.add_on),
                selectinload(CatalogOffer.radius_profiles).selectinload(
                    OfferRadiusProfile.profile
                ),
            ],
        )
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        return offer

    @staticmethod
    def list(
        db: Session,
        service_type: str | None,
        access_type: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CatalogOffer).options(
            selectinload(CatalogOffer.region_zone),
            selectinload(CatalogOffer.usage_allowance),
            selectinload(CatalogOffer.sla_profile),
            selectinload(CatalogOffer.policy_set),
            selectinload(CatalogOffer.prices),
            selectinload(CatalogOffer.add_on_links).selectinload(OfferAddOn.add_on),
            selectinload(CatalogOffer.radius_profiles).selectinload(
                OfferRadiusProfile.profile
            ),
        )
        if service_type:
            query = query.filter(
                CatalogOffer.service_type
                == validate_enum(service_type, ServiceType, "service_type")
            )
        if access_type:
            query = query.filter(
                CatalogOffer.access_type
                == validate_enum(access_type, AccessType, "access_type")
            )
        if status:
            query = query.filter(
                CatalogOffer.status == validate_enum(status, OfferStatus, "status")
            )
        query = apply_active_state(query, CatalogOffer.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CatalogOffer.created_at,
                "name": CatalogOffer.name,
                "status": CatalogOffer.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(
        cls,
        db: Session,
        offer_id: str,
        payload: CatalogOfferUpdate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        offer = cls._get_or_404(db, offer_id)
        data = payload.model_dump(exclude_unset=True)
        changes = billing_governance.billing_field_changes(offer, data)
        billing_governance.assert_offer_update_safe(db, offer, changes)
        for key, value in data.items():
            setattr(offer, key, value)
        # Status is authoritative: the edit form exposes both the status select
        # and the is_active checkbox, and they used to drift (archived offers
        # left is_active=True stayed visible on the customer portal).
        if offer.status != OfferStatus.active and offer.is_active:
            offer.is_active = False
            changes["is_active"] = False
        critical_changes = billing_governance.billing_critical_changes(
            "catalog_offer", changes
        )
        if critical_changes:
            billing_governance.stage_billing_catalog_change(
                db,
                action="updated",
                entity_type="catalog_offer",
                entity_id=offer.id,
                changes=critical_changes,
                actor_id=actor_id,
                actor_type=actor_type,
                offer_id=offer.id,
            )
        db.commit()
        db.refresh(offer)
        return offer

    @staticmethod
    def delete(
        db: Session,
        offer_id: str,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        """Soft-delete: archive the offer. Caller controls the transaction."""
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        offer.status = OfferStatus.archived
        offer.is_active = False
        billing_governance.stage_billing_catalog_change(
            db,
            action="archived",
            entity_type="catalog_offer",
            entity_id=offer.id,
            changes={"status": OfferStatus.archived, "is_active": False},
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=offer.id,
        )
        db.flush()

    @staticmethod
    def restore(
        db: Session,
        offer_id: str,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        """Move an archived offer back to active. Caller controls the transaction."""
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        offer.status = OfferStatus.active
        offer.is_active = True
        billing_governance.stage_billing_catalog_change(
            db,
            action="restored",
            entity_type="catalog_offer",
            entity_id=offer.id,
            changes={"status": OfferStatus.active, "is_active": True},
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=offer.id,
        )
        db.flush()


class OfferPrices(CRUDManager[OfferPrice]):
    model = OfferPrice
    not_found_detail = "Offer price not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(
        db: Session,
        payload: OfferPriceCreate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        billing_governance.assert_offer_price_create_safe(db, payload)
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "price_type" not in fields_set:
            default_price_type = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_price_type"
            )
            if default_price_type:
                data["price_type"] = validate_enum(
                    default_price_type, PriceType, "price_type"
                )
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        price = OfferPrice(**data)
        db.add(price)
        db.flush()
        billing_governance.stage_billing_catalog_change(
            db,
            action="price_created",
            entity_type="offer_price",
            entity_id=price.id,
            changes=data,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=price.offer_id,
        )
        db.commit()
        db.refresh(price)
        return price

    @classmethod
    def get(cls, db: Session, price_id: str):
        return super().get(db, price_id)

    @staticmethod
    def list(
        db: Session,
        offer_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferPrice)
        query = apply_optional_equals(query, {OfferPrice.offer_id: offer_id})
        query = apply_active_state(query, OfferPrice.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OfferPrice.created_at, "amount": OfferPrice.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(
        cls,
        db: Session,
        price_id: str,
        payload: OfferPriceUpdate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        price = cls._get_or_404(db, price_id)
        data = payload.model_dump(exclude_unset=True)
        changes = billing_governance.billing_field_changes(price, data)
        billing_governance.assert_offer_price_update_safe(db, price, changes)
        previous_amount = Decimal(str(price.amount))
        for key, value in data.items():
            setattr(price, key, value)
        renewal_outcome = None
        if (
            "amount" in changes
            and price.price_type == PriceType.recurring
            and price.is_active
        ):
            renewal_outcome = billing_governance.apply_offer_price_at_renewal(
                db,
                billing_governance.ApplyOfferPriceAtRenewal(
                    offer_price_id=price.id,
                    offer_id=price.offer_id,
                    previous_amount=previous_amount,
                    next_amount=Decimal(str(price.amount)),
                ),
            )
        critical_changes = billing_governance.billing_critical_changes(
            "offer_price", changes
        )
        if critical_changes:
            if renewal_outcome is not None:
                critical_changes = {
                    **critical_changes,
                    "previous_amount": previous_amount,
                    "renewal_subscription_count": (
                        renewal_outcome.affected_subscription_count
                    ),
                }
            billing_governance.stage_billing_catalog_change(
                db,
                action="price_updated",
                entity_type="offer_price",
                entity_id=price.id,
                changes=critical_changes,
                actor_id=actor_id,
                actor_type=actor_type,
                offer_id=price.offer_id,
            )
        db.commit()
        db.refresh(price)
        return price

    @classmethod
    def delete(
        cls,
        db: Session,
        price_id: str,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        price = cls._get_or_404(db, price_id)
        changes = {"is_active": False}
        billing_governance.assert_offer_price_update_safe(db, price, changes)
        price.is_active = False
        billing_governance.stage_billing_catalog_change(
            db,
            action="price_deactivated",
            entity_type="offer_price",
            entity_id=price.id,
            changes=changes,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=price.offer_id,
        )
        db.commit()


#: _assert_offer_version_identity_immutable used to live here. Round 16
#: (Michael's ruling on round 15 finding 3) moved it to
#: offer_access_requirement.py alongside the rest of the update mutation
#: logic, when OfferVersions.update stopped constructing/mutating the row
#: itself and became a thin adapter over the new, registered
#: update_offer_version owner command.


class OfferVersions(CRUDManager[OfferVersion]):
    model = OfferVersion
    not_found_detail = "Offer version not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def _resolve_admission_principal(
        actor_id: str | None, actor_type: str | None
    ) -> "offer_access_requirement.AdmissionPrincipal":
        """Resolve a recognized, authenticated actor into its typed
        principal. FAILS CLOSED: any ``actor_id``/``actor_type`` combination
        that is not a real ``system_user``, ``api_key``, or ``subscriber``
        raises a typed error rather than silently defaulting to
        ``SystemAdmission`` — a caller with no authenticated actor at all
        must go through ``create``'s distinct ``principal=`` argument
        instead (see its docstring). ``app/api/catalog.py``'s route always
        supplies a real, authenticated ``system_user``/``api_key``/
        ``subscriber`` actor, so this only ever raises for a caller that
        invokes this adapter directly with neither a recognized actor nor an
        explicit ``principal``.

        A ``subscriber`` actor is a supported caller shape (a subscriber
        mapped to the ``admin`` role, or any role holding the compound
        admission permission, via the seeded role-assignment path) — see
        ``offer_access_requirement.SubscriberPrincipal``'s docstring."""

        if actor_type == "system_user" and actor_id:
            return offer_access_requirement.StaffPrincipal(
                system_user_id=UUID(str(actor_id))
            )
        if actor_type == "api_key" and actor_id:
            return offer_access_requirement.ApiKeyPrincipal(
                api_key_id=UUID(str(actor_id))
            )
        if actor_type == "subscriber" and actor_id:
            return offer_access_requirement.SubscriberPrincipal(
                subscriber_id=UUID(str(actor_id))
            )
        raise offer_access_requirement.OfferAccessRequirementError(
            code=f"{offer_access_requirement.OWNER}.unattributed_admission_actor",
            message=(
                "offer_versions.create requires either a recognized "
                "system_user/api_key/subscriber actor_id/actor_type pair or "
                "an explicit principal= argument (e.g. SystemAdmission for a "
                "genuinely internal/test caller); neither was supplied."
            ),
            details={"actor_id": actor_id, "actor_type": actor_type},
            retryable=False,
        )

    @staticmethod
    def create(
        db: Session,
        payload: OfferVersionCreate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
        idempotency_key: str | None = None,
        principal: "offer_access_requirement.AdmissionPrincipal | None" = None,
    ):
        """Thin adapter. The actual persist, defaults resolution, and
        transaction are owned by
        ``service_intent.offer_access_requirement.admit_offer_version`` —
        this method builds the command and returns its result; it never
        constructs the ``OfferVersion`` row itself.

        ``idempotency_key`` is optional: a caller that supplies one and
        retries with the SAME key and the same request gets back the
        original row instead of a ``duplicate_version_number`` conflict. A
        caller that supplies none is not idempotent — a lost response or a
        retried POST with no key is not distinguishable from a genuinely new
        admission.

        Authorization has ONE decision owner
        (``offer_access_requirement.authorize_offer_version_admission``),
        which BOTH the route's ``_require_offer_version_admission``
        dependency and a fresh, in-transaction re-check inside
        ``admit_offer_version`` itself
        (``offer_access_requirement.verify_admission_authorization``)
        delegate to — this method does not decide authorization itself, but
        the command it builds does. ``actor_id``/``actor_type`` become the
        admission's typed principal, used for BOTH that re-check and audit
        attribution.

        ``principal`` is the ONLY way to admit with no authenticated actor
        (e.g. ``SystemAdmission`` for an internal/test caller) — pass it
        explicitly rather than omitting ``actor_id``/``actor_type`` and
        relying on an implicit fallback: an unrecognized or omitted
        ``actor_id``/``actor_type`` with no ``principal`` supplied is a
        typed error, never a silent ``SystemAdmission``.
        """
        resolved_principal = (
            principal
            if principal is not None
            else OfferVersions._resolve_admission_principal(actor_id, actor_type)
        )
        command_id = uuid4()
        result = offer_access_requirement.admit_offer_version(
            db,
            offer_access_requirement.AdmitOfferVersionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=offer_access_requirement.admission_actor_label(
                        resolved_principal
                    ),
                    scope=offer_access_requirement.ADMISSION_SCOPE,
                    reason="offer version admitted via catalog API",
                    idempotency_key=idempotency_key,
                ),
                payload=payload,
                principal=resolved_principal,
            ),
        )
        return result.offer_version

    @classmethod
    def get(cls, db: Session, version_id: str):
        return super().get(db, version_id)

    @staticmethod
    def list(
        db: Session,
        offer_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferVersion)
        query = apply_optional_equals(query, {OfferVersion.offer_id: offer_id})
        query = apply_active_state(query, OfferVersion.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": OfferVersion.created_at,
                "version_number": OfferVersion.version_number,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        version_id: str,
        payload: OfferVersionUpdate,
        *,
        principal: "offer_access_requirement.AdmissionPrincipal",
        request_id: str | None = None,
    ):
        """Thin adapter. The actual mutation, read-only validation, the
        in-transaction authorization recheck, and the billing-governance
        audit participant are owned by
        ``service_intent.offer_access_requirement.update_offer_version``
        (round 16 — Michael's ruling on round 15 finding 3) — this method
        builds the command and returns its result; it never mutates the
        row or completes the transaction itself. Before this, ``update``
        called ``db.commit()`` directly: a direct caller with unrelated
        pending work in the same session had that work silently committed
        alongside the version update, regardless of that caller's own
        intent.

        ``principal`` is REQUIRED: the route's own gate
        (``_require_offer_version_admission``) authorizes ADMISSION only —
        reaching this method proves nothing about whether the caller's
        grant is still valid by the time the mutation actually happens.
        ``update_offer_version`` re-verifies through the SAME owner
        (``authorize_offer_version_admission``, via
        ``verify_admission_authorization``) immediately before the
        mutation, inside its own transaction, against the LIVE database —
        never trusted from the route's earlier check alone. A caller with
        no authenticated actor (internal/test code) must pass
        ``SystemAdmission`` explicitly; there is no silent default.
        Audit attribution is derived from ``principal`` itself inside the
        command (the same evidence-derivation admission already uses),
        not from separate ``actor_id``/``actor_type`` arguments.
        """

        command_id = uuid4()
        version = offer_access_requirement.update_offer_version(
            db,
            offer_access_requirement.UpdateOfferVersionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=offer_access_requirement.admission_actor_label(principal),
                    scope=offer_access_requirement.ADMISSION_SCOPE,
                    reason="offer version updated via catalog API",
                ),
                offer_version_id=UUID(str(version_id)),
                payload=payload,
                principal=principal,
            ),
        )
        return version

    @classmethod
    def delete(
        cls,
        db: Session,
        version_id: str,
        *,
        principal: "offer_access_requirement.AdmissionPrincipal",
        actor_id: str | None = None,
        actor_type: str | None = None,
        request_id: str | None = None,
    ):
        """Deactivate an already-admitted offer version.

        ``principal`` is REQUIRED (round 15 finding 2 — Michael's ruling
        covers "PATCH and every mutation", and deactivation deactivates a
        real, already-admitted commercial offering exactly as a PATCH
        mutates one). The route's own gate authorizes reaching the route at
        all; this method re-verifies through the SAME owner
        (``offer_access_requirement.verify_admission_authorization``)
        IMMEDIATELY before the mutation below, inside this method's own
        transaction, against the LIVE database — never trusted from the
        route's earlier check alone, and never skippable by a direct
        service caller that bypasses the route entirely. A caller with no
        authenticated actor (internal/test code) must pass
        ``SystemAdmission`` explicitly, the same escape hatch admission and
        update use — there is no silent default.
        """

        version = cls._get_or_404(db, version_id)
        changes = {"is_active": False}
        billing_governance.assert_offer_version_update_safe(db, version, changes)
        # Immediately before the mutation, not at the top of this method —
        # narrowing the window between the re-check and the write it gates
        # to the read-only validation above, which touches no session state.
        offer_access_requirement.verify_admission_authorization(
            db, principal, request_id=request_id
        )
        version.is_active = False
        billing_governance.stage_billing_catalog_change(
            db,
            action="version_deactivated",
            entity_type="offer_version",
            entity_id=version.id,
            changes=changes,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=version.offer_id,
        )
        db.commit()


class OfferVersionPrices(CRUDManager[OfferVersionPrice]):
    model = OfferVersionPrice
    not_found_detail = "Offer version price not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(
        db: Session,
        payload: OfferVersionPriceCreate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        billing_governance.assert_offer_version_price_create_safe(db, payload)
        version = db.get(OfferVersion, payload.offer_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "price_type" not in fields_set:
            default_price_type = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_price_type"
            )
            if default_price_type:
                data["price_type"] = validate_enum(
                    default_price_type, PriceType, "price_type"
                )
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        price = OfferVersionPrice(**data)
        db.add(price)
        db.flush()
        billing_governance.stage_billing_catalog_change(
            db,
            action="version_price_created",
            entity_type="offer_version_price",
            entity_id=price.id,
            changes=data,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=version.offer_id,
        )
        db.commit()
        db.refresh(price)
        return price

    @classmethod
    def get(cls, db: Session, price_id: str):
        return super().get(db, price_id)

    @staticmethod
    def list(
        db: Session,
        offer_version_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferVersionPrice)
        query = apply_optional_equals(
            query,
            {OfferVersionPrice.offer_version_id: offer_version_id},
        )
        query = apply_active_state(query, OfferVersionPrice.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": OfferVersionPrice.created_at,
                "amount": OfferVersionPrice.amount,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        price_id: str,
        payload: OfferVersionPriceUpdate,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        price = db.get(OfferVersionPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer version price not found")
        data = payload.model_dump(exclude_unset=True)
        changes = billing_governance.billing_field_changes(price, data)
        billing_governance.assert_offer_version_price_update_safe(db, price, changes)
        if "offer_version_id" in data:
            version = db.get(OfferVersion, data["offer_version_id"])
            if not version:
                raise HTTPException(status_code=404, detail="Offer version not found")
        for key, value in data.items():
            setattr(price, key, value)
        version = db.get(OfferVersion, price.offer_version_id)
        critical_changes = billing_governance.billing_critical_changes(
            "offer_version_price", changes
        )
        if critical_changes:
            billing_governance.stage_billing_catalog_change(
                db,
                action="version_price_updated",
                entity_type="offer_version_price",
                entity_id=price.id,
                changes=critical_changes,
                actor_id=actor_id,
                actor_type=actor_type,
                offer_id=version.offer_id if version else None,
            )
        db.commit()
        db.refresh(price)
        return price

    @classmethod
    def delete(
        cls,
        db: Session,
        price_id: str,
        *,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ):
        price = cls._get_or_404(db, price_id)
        changes = {"is_active": False}
        billing_governance.assert_offer_version_price_update_safe(db, price, changes)
        price.is_active = False
        version = db.get(OfferVersion, price.offer_version_id)
        billing_governance.stage_billing_catalog_change(
            db,
            action="version_price_deactivated",
            entity_type="offer_version_price",
            entity_id=price.id,
            changes=changes,
            actor_id=actor_id,
            actor_type=actor_type,
            offer_id=version.offer_id if version else None,
        )
        db.commit()
