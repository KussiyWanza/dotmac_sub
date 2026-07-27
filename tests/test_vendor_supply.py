"""Material released to a vendor, and money advanced to one.

Neither existed. ``FieldMaterialRequest`` is work-order scoped with a
``TechnicianProfile`` requester, so a contractor drawing our cable for a
buildout project had no path to request anything; and nothing modelled an
advance at all, so mobilisation money left no project-anchored record.

Both owners decide and record; the configured provider issues the stock or
moves the money. These tests pin the Sub decisions and — just as important —
the places Sub deliberately refuses to compute the provider's answer.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.domain_settings import (
    DomainSetting,
    SettingDomain,
    SettingValueType,
)
from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
)
from app.models.vendor_supply import VendorAdvanceStatus, VendorMaterialReleaseStatus
from app.services import vendor_advances, vendor_material_release

ACTOR = uuid4()


def _project(
    db_session,
    *,
    status: str = InstallationProjectStatus.in_progress.value,
    quote_total: Decimal | None = Decimal("1000000.00"),
    quote_status: str = ProjectQuoteStatus.approved.value,
):
    project = Project(name=f"Buildout {uuid4().hex[:6]}")
    vendor = Vendor(name=f"Vendor {uuid4().hex[:6]}", code=f"V-{uuid4().hex[:8]}")
    db_session.add_all([project, vendor])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        status=status,
    )
    db_session.add(installation)
    db_session.flush()
    if quote_total is not None:
        quote = ProjectQuote(
            project_id=installation.id,
            vendor_id=vendor.id,
            status=quote_status,
            currency="NGN",
            total=quote_total,
        )
        db_session.add(quote)
        db_session.flush()
        installation.approved_quote_id = quote.id
    db_session.commit()
    return installation, vendor


def _set_advance_cap(db_session, percent: int) -> None:
    """Operators set the guard rail; there is no code default percentage."""
    db_session.add(
        DomainSetting(
            domain=SettingDomain.projects,
            key="vendor_advance_max_percent",
            value_type=SettingValueType.integer,
            value_text=str(percent),
            is_active=True,
        )
    )
    db_session.commit()


def _items(**overrides):
    item = {"description": "Fibre cable 24F", "quantity": 500, "unit": "m"}
    item.update(overrides)
    return (item,)


# ---------------------------------------------------------------------------
# Material release
# ---------------------------------------------------------------------------


def test_a_vendor_can_request_material_for_their_project(db_session):
    installation, vendor = _project(db_session)

    release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=ACTOR,
            items=_items(),
        ),
    )
    db_session.commit()

    assert release.status == VendorMaterialReleaseStatus.requested.value
    assert len(release.items) == 1
    assert release.items[0].quantity == 500
    # Sub has not issued anything — no provider outcome yet.
    assert release.support_status is None


def test_material_cannot_be_released_to_a_vendor_who_is_not_assigned(db_session):
    installation, _vendor = _project(db_session)
    intruder = Vendor(name="Intruder", code=f"I-{uuid4().hex[:8]}")
    db_session.add(intruder)
    db_session.commit()

    with pytest.raises(vendor_material_release.VendorMaterialReleaseError) as exc:
        vendor_material_release.request_release(
            db_session,
            vendor_material_release.RequestMaterialRelease(
                project_id=installation.id,
                vendor_id=intruder.id,
                requested_by_person_id=ACTOR,
                items=_items(),
            ),
        )

    assert exc.value.code == "project_not_assigned"


def test_material_is_not_released_against_unstarted_or_finished_work(db_session):
    """Releasing stock for work nobody has agreed to, or that is already
    verified, is releasing it for nothing."""
    for status in (
        InstallationProjectStatus.draft.value,
        InstallationProjectStatus.open_for_bidding.value,
        InstallationProjectStatus.verified.value,
    ):
        installation, vendor = _project(db_session, status=status)

        with pytest.raises(vendor_material_release.VendorMaterialReleaseError) as exc:
            vendor_material_release.request_release(
                db_session,
                vendor_material_release.RequestMaterialRelease(
                    project_id=installation.id,
                    vendor_id=vendor.id,
                    requested_by_person_id=ACTOR,
                    items=_items(),
                ),
            )

        assert exc.value.code == "project_not_releasable", status


def test_a_release_needs_at_least_one_positive_line(db_session):
    installation, vendor = _project(db_session)

    for items, code in (
        ((), "items_required"),
        (({"description": "  ", "quantity": 5},), "items_required"),
        (_items(quantity=0), "invalid_quantity"),
        (_items(quantity=-3), "invalid_quantity"),
    ):
        with pytest.raises(vendor_material_release.VendorMaterialReleaseError) as exc:
            vendor_material_release.request_release(
                db_session,
                vendor_material_release.RequestMaterialRelease(
                    project_id=installation.id,
                    vendor_id=vendor.id,
                    requested_by_person_id=ACTOR,
                    items=items,
                ),
            )
        assert exc.value.code == code


def test_staff_approval_is_the_decision_the_provider_acts_on(db_session):
    installation, vendor = _project(db_session)
    release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=ACTOR,
            items=_items(),
        ),
    )
    db_session.commit()

    vendor_material_release.approve(db_session, release.id, actor_id=ACTOR)
    db_session.commit()

    assert release.status == VendorMaterialReleaseStatus.approved.value
    assert release.reviewed_at is not None
    # Approval alone does not claim the material left the warehouse.
    assert release.support_status is None


def test_a_provider_refusal_never_reverses_the_sub_approval(db_session):
    """Provider delivery failure is recorded for retry; it does not undo a
    valid Sub state transition."""
    installation, vendor = _project(db_session)
    release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=ACTOR,
            items=_items(),
        ),
    )
    vendor_material_release.approve(db_session, release.id, actor_id=ACTOR)
    db_session.commit()

    vendor_material_release.apply_provider_outcome(
        db_session,
        release.id,
        support_system="dotmac_erp",
        support_reference="ISS-9",
        support_status="out_of_stock",
    )
    db_session.commit()

    assert release.status == VendorMaterialReleaseStatus.approved.value
    assert release.support_status == "out_of_stock"
    assert release.support_system == "dotmac_erp"


def test_a_provider_issue_marks_the_release_issued(db_session):
    installation, vendor = _project(db_session)
    release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=ACTOR,
            items=_items(),
        ),
    )
    vendor_material_release.approve(db_session, release.id, actor_id=ACTOR)
    db_session.commit()
    item_id = str(release.items[0].id)

    vendor_material_release.apply_provider_outcome(
        db_session,
        release.id,
        support_system="dotmac_erp",
        support_reference="ISS-10",
        support_status="issued",
        issued_quantities={item_id: 480},
    )
    db_session.commit()

    assert release.status == VendorMaterialReleaseStatus.issued.value
    assert release.items[0].issued_quantity == 480


def test_provider_outcomes_are_idempotent(db_session):
    installation, vendor = _project(db_session)
    release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=ACTOR,
            items=_items(),
        ),
    )
    vendor_material_release.approve(db_session, release.id, actor_id=ACTOR)
    db_session.commit()

    for _ in range(2):
        vendor_material_release.apply_provider_outcome(
            db_session,
            release.id,
            support_system="dotmac_erp",
            support_reference="ISS-11",
            support_status="issued",
        )
        db_session.commit()

    assert release.status == VendorMaterialReleaseStatus.issued.value
    assert release.support_reference == "ISS-11"


# ---------------------------------------------------------------------------
# Advances
# ---------------------------------------------------------------------------


def test_an_advance_draws_against_the_approved_quote(db_session):
    installation, vendor = _project(db_session)

    advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id,
            vendor_id=vendor.id,
            amount="250000",
            requested_by_person_id=ACTOR,
            reason="Mobilisation",
        ),
    )
    db_session.commit()

    assert advance.status == VendorAdvanceStatus.requested.value
    assert advance.amount == Decimal("250000.00")
    # Currency is taken from the quote, never from the requester.
    assert advance.currency == "NGN"
    assert advance.quote_id == installation.approved_quote_id


def test_an_advance_needs_an_approved_quote_to_bound_it(db_session):
    """Without an agreed value there is no ceiling and no basis for paying."""
    installation, vendor = _project(db_session, quote_total=None)

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="1000"
            ),
        )

    assert exc.value.code == "approved_quote_required"


def test_any_amount_up_to_the_quote_total_is_allowed_by_default(db_session):
    """No percentage policy ships by default. Staff approval is the control,
    so an approver may advance whatever the job is worth."""
    installation, vendor = _project(db_session)

    advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="1000000"
        ),
    )
    db_session.commit()

    assert advance.amount == Decimal("1000000.00")


def test_advances_cannot_exceed_the_quote_total(db_session):
    """Arithmetic, not policy: you cannot advance more than the work is agreed
    to be worth. Cost escalation is answered by a variation, not an
    over-advance."""
    installation, vendor = _project(db_session)

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="1000000.01"
            ),
        )

    assert exc.value.code == "advance_ceiling_exceeded"


def test_advances_cannot_be_stacked_past_the_quote_total(db_session):
    """Each request counts what is already committed, so the bound cannot be
    evaded by splitting a request in two."""
    installation, vendor = _project(db_session)
    vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="900000"
        ),
    )
    db_session.commit()

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="200000"
            ),
        )

    assert exc.value.code == "advance_ceiling_exceeded"


def test_a_configured_percentage_lowers_the_ceiling(db_session):
    """Where an operator sets a policy, it stops an out-of-policy request
    before it reaches an approver."""
    installation, vendor = _project(db_session)
    _set_advance_cap(db_session, 30)

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="300000.01"
            ),
        )
    assert exc.value.code == "advance_ceiling_exceeded"

    allowed = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="300000"
        ),
    )
    db_session.commit()
    assert allowed.amount == Decimal("300000.00")


def test_a_configured_percentage_can_never_raise_the_hard_bound(db_session):
    """A misconfigured 100% must not authorise advancing more than the quote."""
    installation, vendor = _project(db_session)
    _set_advance_cap(db_session, 100)

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="1000000.01"
            ),
        )

    assert exc.value.code == "advance_ceiling_exceeded"


def test_a_rejected_advance_releases_the_ceiling_it_reserved(db_session):
    installation, vendor = _project(db_session)
    _set_advance_cap(db_session, 40)
    first = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="400000"
        ),
    )
    db_session.commit()
    vendor_advances.reject(db_session, first.id, actor_id=ACTOR, reason="Too early")
    db_session.commit()

    second = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="400000"
        ),
    )
    db_session.commit()

    assert second.status == VendorAdvanceStatus.requested.value


def test_an_advance_is_not_available_to_an_unassigned_vendor(db_session):
    installation, _vendor = _project(db_session)
    intruder = Vendor(name="Intruder", code=f"I-{uuid4().hex[:8]}")
    db_session.add(intruder)
    db_session.commit()

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=intruder.id, amount="1000"
            ),
        )

    assert exc.value.code == "project_not_assigned"


def test_an_advance_is_not_available_once_work_is_verified(db_session):
    """Verified work is complete and invoiceable — that is payment, not an
    advance."""
    installation, vendor = _project(
        db_session, status=InstallationProjectStatus.verified.value
    )

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.request_advance(
            db_session,
            vendor_advances.RequestVendorAdvance(
                project_id=installation.id, vendor_id=vendor.id, amount="1000"
            ),
        )

    assert exc.value.code == "project_not_advanceable"


def test_settlement_is_observed_never_decided(db_session):
    """Sub records that it asked; the payables provider records that it paid.
    Sub never marks itself paid and never nets against an invoice."""
    installation, vendor = _project(db_session)
    advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="100000"
        ),
    )
    vendor_advances.approve(db_session, advance.id, actor_id=ACTOR)
    db_session.commit()
    assert advance.status == VendorAdvanceStatus.approved.value

    vendor_advances.apply_payables_observation(
        db_session,
        advance.id,
        payables_system="dotmac_erp",
        payables_reference="ADV-4",
        payables_status="paid",
    )
    db_session.commit()

    assert advance.status == VendorAdvanceStatus.settled.value
    assert advance.payables_reference == "ADV-4"


def test_a_pending_payables_status_leaves_the_advance_approved(db_session):
    installation, vendor = _project(db_session)
    advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="100000"
        ),
    )
    vendor_advances.approve(db_session, advance.id, actor_id=ACTOR)
    db_session.commit()

    vendor_advances.apply_payables_observation(
        db_session,
        advance.id,
        payables_system="dotmac_erp",
        payables_reference="ADV-5",
        payables_status="scheduled",
    )
    db_session.commit()

    assert advance.status == VendorAdvanceStatus.approved.value
    assert advance.payables_status == "scheduled"


def test_rejection_requires_a_reason(db_session):
    installation, vendor = _project(db_session)
    advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id, vendor_id=vendor.id, amount="1000"
        ),
    )
    db_session.commit()

    with pytest.raises(vendor_advances.VendorAdvanceError) as exc:
        vendor_advances.reject(db_session, advance.id, actor_id=ACTOR, reason="  ")

    assert exc.value.code == "reason_required"


def test_only_the_owner_role_may_ask_dotmac_for_money(db_session):
    """A supervisor runs the site and can draw material; committing the
    organisation to a financial ask stays with the owner."""
    from app.services.field import vendor_capabilities as caps

    assert caps.ADVANCE_REQUEST in caps.capabilities_for_role("owner")
    assert caps.ADVANCE_REQUEST not in caps.capabilities_for_role("supervisor")
    assert caps.ADVANCE_REQUEST not in caps.capabilities_for_role("field")

    assert caps.MATERIAL_REQUEST in caps.capabilities_for_role("supervisor")
    assert caps.MATERIAL_REQUEST not in caps.capabilities_for_role("field")
