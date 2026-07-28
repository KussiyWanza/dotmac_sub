"""Read-only admin portfolio for one field vendor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
)
from app.services import vendor_advances, vendor_material_release
from app.services.project_vendor_delivery import ProjectVendorDeliveryVisibility
from app.services.vendor_delivery_portfolio import (
    VendorPortfolioQuery,
    get_vendor_delivery_portfolio,
)


def _vendor(db_session, *, name: str = "Portfolio Vendor") -> Vendor:
    row = Vendor(name=name, code=f"VP-{uuid4().hex[:8]}")
    db_session.add(row)
    db_session.flush()
    return row


def _assignment(
    db_session,
    *,
    vendor: Vendor,
    name: str,
    number: str,
    status: InstallationProjectStatus,
    quote_total: Decimal | None = None,
    is_active: bool = True,
) -> InstallationProject:
    project = Project(
        name=name,
        number=number,
        code=f"CODE-{number}",
        is_active=is_active,
    )
    db_session.add(project)
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        status=status.value,
    )
    db_session.add(installation)
    db_session.flush()
    if quote_total is not None:
        quote = ProjectQuote(
            project_id=installation.id,
            vendor_id=vendor.id,
            status=ProjectQuoteStatus.approved.value,
            currency="NGN",
            total=quote_total,
        )
        db_session.add(quote)
        db_session.flush()
        installation.approved_quote_id = quote.id
    return installation


def _query(
    vendor: Vendor,
    *,
    operations: bool = True,
    routes: bool = False,
    financials: bool = False,
    search: str | None = None,
    status: InstallationProjectStatus | None = None,
    limit: int = 25,
    offset: int = 0,
) -> VendorPortfolioQuery:
    return VendorPortfolioQuery(
        vendor_id=vendor.id,
        visibility=ProjectVendorDeliveryVisibility(
            can_read_operations=operations,
            can_read_routes=routes,
            can_read_financials=financials,
        ),
        search=search,
        status=status,
        limit=limit,
        offset=offset,
    )


def test_portfolio_is_vendor_scoped_and_kpis_match_active_cohorts(db_session):
    vendor = _vendor(db_session)
    other = _vendor(db_session, name="Other Vendor")
    _assignment(
        db_session,
        vendor=vendor,
        name="Approved Metro",
        number="PRJ-APPROVED",
        status=InstallationProjectStatus.approved,
    )
    _assignment(
        db_session,
        vendor=vendor,
        name="Live Backbone",
        number="PRJ-LIVE",
        status=InstallationProjectStatus.in_progress,
    )
    _assignment(
        db_session,
        vendor=vendor,
        name="Completed Spur",
        number="PRJ-DONE",
        status=InstallationProjectStatus.completed,
    )
    _assignment(
        db_session,
        vendor=vendor,
        name="Inactive Project",
        number="PRJ-INACTIVE",
        status=InstallationProjectStatus.completed,
        is_active=False,
    )
    _assignment(
        db_session,
        vendor=other,
        name="Other Vendor Project",
        number="PRJ-OTHER",
        status=InstallationProjectStatus.in_progress,
    )
    db_session.commit()

    portfolio = get_vendor_delivery_portfolio(db_session, _query(vendor))

    assert portfolio.total == 3
    assert {item.name for item in portfolio.items} == {
        "Approved Metro",
        "Live Backbone",
        "Completed Spur",
    }
    kpis = {item.label: item for item in portfolio.kpis}
    assert kpis["Assigned projects"].value.value == 3
    assert kpis["Approved projects"].value.value == 1
    assert kpis["In progress projects"].value.value == 1
    assert kpis["Completed projects"].value.value == 1
    assert kpis["In progress projects"].cohort_url.endswith(
        "?project_status=in_progress#delivery-portfolio"
    )


def test_portfolio_has_an_explicit_empty_result(db_session):
    vendor = _vendor(db_session)
    db_session.commit()

    portfolio = get_vendor_delivery_portfolio(db_session, _query(vendor))

    assert portfolio.items == ()
    assert portfolio.total == 0
    assert portfolio.has_previous is False
    assert portfolio.has_next is False
    assert portfolio.kpis[0].value.value == 0


def test_portfolio_applies_exact_filter_search_and_stable_pagination(db_session):
    vendor = _vendor(db_session)
    for index in range(12):
        row = _assignment(
            db_session,
            vendor=vendor,
            name=f"Metro Build {index:02d}",
            number=f"METRO-{index:02d}",
            status=(
                InstallationProjectStatus.in_progress
                if index % 2
                else InstallationProjectStatus.completed
            ),
        )
        row.updated_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    db_session.commit()

    first = get_vendor_delivery_portfolio(
        db_session,
        _query(
            vendor,
            status=InstallationProjectStatus.in_progress,
            limit=10,
        ),
    )
    searched = get_vendor_delivery_portfolio(
        db_session,
        _query(vendor, search="METRO-03"),
    )
    second = get_vendor_delivery_portfolio(
        db_session,
        _query(vendor, limit=10, offset=10),
    )

    assert first.total == 6
    assert all(item.status.value == "in_progress" for item in first.items)
    assert searched.total == 1
    assert searched.items[0].code == "METRO-03"
    assert len(second.items) == 2
    assert second.has_previous is True
    assert second.has_next is False
    assert second.items[0].code == "METRO-01"
    assert second.items[1].code == "METRO-00"


def test_portfolio_visibility_omits_finance_and_route_facts(db_session):
    vendor = _vendor(db_session)
    installation = _assignment(
        db_session,
        vendor=vendor,
        name="Permissioned Delivery",
        number="PERM-01",
        status=InstallationProjectStatus.in_progress,
        quote_total=Decimal("250000.00"),
    )
    db_session.commit()

    operations = get_vendor_delivery_portfolio(
        db_session,
        _query(vendor, operations=True),
    ).items[0]
    financials = get_vendor_delivery_portfolio(
        db_session,
        _query(vendor, operations=True, routes=True, financials=True),
    ).items[0]

    assert operations.installation_project_id == installation.id
    assert operations.delivery.quote is not None
    assert operations.delivery.quote.total is None
    assert operations.delivery.route is None
    assert operations.delivery.invoice is None
    assert operations.latest_advance is None
    assert financials.delivery.quote is not None
    assert financials.delivery.quote.total == Decimal("250000.00")
    assert financials.delivery.invoice is not None


def test_portfolio_selects_latest_active_supply_records_in_bulk(db_session):
    vendor = _vendor(db_session)
    installation = _assignment(
        db_session,
        vendor=vendor,
        name="Supply Delivery",
        number="SUPPLY-01",
        status=InstallationProjectStatus.in_progress,
        quote_total=Decimal("500000.00"),
    )
    db_session.commit()
    older_release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=uuid4(),
            items=(
                {
                    "description": "Older cable request",
                    "quantity": 10,
                    "unit": "m",
                },
            ),
        ),
    )
    newer_release = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=uuid4(),
            items=(
                {
                    "description": "Latest cable request",
                    "quantity": 20,
                    "unit": "m",
                },
            ),
        ),
    )
    older_advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id,
            vendor_id=vendor.id,
            amount=Decimal("50000.00"),
            reason="Earlier mobilisation",
        ),
    )
    newer_advance = vendor_advances.request_advance(
        db_session,
        vendor_advances.RequestVendorAdvance(
            project_id=installation.id,
            vendor_id=vendor.id,
            amount=Decimal("75000.00"),
            reason="Latest mobilisation",
        ),
    )
    older_release.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer_release.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    older_advance.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer_advance.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    db_session.commit()

    item = get_vendor_delivery_portfolio(
        db_session,
        _query(vendor, operations=True, financials=True),
    ).items[0]

    assert item.latest_material_release is not None
    assert item.latest_material_release.id == newer_release.id
    assert item.latest_advance is not None
    assert item.latest_advance.id == newer_advance.id
