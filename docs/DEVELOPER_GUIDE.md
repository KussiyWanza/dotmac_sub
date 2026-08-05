# DotMac Sub Developer Guide

A comprehensive guide for developers working on the DotMac Subscription Management platform.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Technology Stack](#technology-stack)
3. [Project Structure](#project-structure)
4. [User Portals & Journeys](#user-portals--journeys)
5. [UI Design Governance](#ui-design-governance)
6. [Implementation Guide](#implementation-guide)
7. [Code Patterns & Best Practices](#code-patterns--best-practices)
8. [Working with Templates](#working-with-templates)
9. [Database & Models](#database--models)
10. [Authentication & Authorization](#authentication--authorization)
11. [Testing](#testing)
12. [DEM Data (SRTM 30m)](#dem-data-srtm-30m)

---

## Architecture Overview

DotMac Sub is a multi-portal subscription management system built with a clean separation between:

- **Web Layer** (`app/web/`) - HTML responses using Jinja2 templates
- **API Layer** (`app/api/`) - RESTful JSON endpoints
- **Service Layer** (`app/services/`) - Business logic
- **Data Layer** (`app/models/`) - SQLAlchemy ORM models

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client (Browser)                          │
├─────────────────────────────────────────────────────────────────┤
│  HTMX (Dynamic Updates)  │  Alpine.js (Client State)            │
├─────────────────────────────────────────────────────────────────┤
│                        FastAPI Application                       │
├──────────────────┬──────────────────┬───────────────────────────┤
│   Web Routes     │    API Routes    │      Static Files         │
│   (HTML)         │    (JSON)        │   (CSS/JS/Images)         │
├──────────────────┴──────────────────┴───────────────────────────┤
│                      Service Layer                               │
│  (Business Logic, Validation, Orchestration)                     │
├─────────────────────────────────────────────────────────────────┤
│                      Data Layer (SQLAlchemy)                     │
├─────────────────────────────────────────────────────────────────┤
│                PostgreSQL + Redis + Celery                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| Backend Framework | FastAPI | Async web framework with OpenAPI |
| Database | PostgreSQL + PostGIS | Primary data store with geo-spatial |
| ORM | SQLAlchemy 2.0 | Database abstraction |
| Templates | Jinja2 | Server-side HTML rendering |
| CSS Framework | Tailwind CSS v4 | Utility-first styling |
| Interactivity | HTMX + Alpine.js | Dynamic UI without heavy JS |
| Task Queue | Celery + Redis | Background job processing |
| Charts | Chart.js | Data visualization |
| Authentication | JWT + Cookies | Session management |

---

## Project Structure

```
dotmac_sub/
├── app/
│   ├── main.py                 # FastAPI app initialization
│   ├── config.py               # Configuration settings
│   ├── db.py                   # Database session management
│   │
│   ├── api/                    # REST API endpoints (JSON)
│   │   ├── customers.py
│   │   ├── subscribers.py
│   │   ├── billing.py
│   │   └── ...
│   │
│   ├── web/                    # Web routes (HTML)
│   │   ├── admin/              # Admin portal routes
│   │   ├── customer/           # Customer portal routes
│   │   ├── reseller/           # Reseller portal routes
│   │   ├── auth/               # Authentication routes
│   │   └── public/             # Public pages
│   │
│   ├── models/                 # SQLAlchemy models
│   │   ├── auth.py
│   │   ├── subscriber.py
│   │   ├── billing.py
│   │   └── ...
│   │
│   ├── services/               # Business logic
│   │   ├── subscriber.py
│   │   ├── billing.py
│   │   └── ...
│   │
│   ├── schemas/                # Pydantic schemas
│   └── tasks/                  # Celery tasks
│
├── templates/                  # Jinja2 templates
│   ├── base.html
│   ├── layouts/
│   ├── components/
│   ├── admin/
│   ├── customer/
│   ├── reseller/
│   └── public/
│
├── static/
│   ├── css/
│   ├── js/
│   └── fonts/
│
└── docs/                       # Documentation
```

---

## User Portals & Journeys

### Portal Overview

| Portal | URL Prefix | Target User | Primary Functions |
|--------|------------|-------------|-------------------|
| Admin | `/admin` | Staff, Administrators | Full system management |
| Customer | `/portal` | End customers | Self-service portal |
| Reseller | `/reseller` | Partner resellers | Multi-account management |

---

### Admin Portal Journey

**Entry Point:** `/admin/dashboard`

```
Login (/auth/login)
    │
    ▼
Dashboard (/admin/dashboard)
    │
    ├── Subscribers (/admin/subscribers)
    │   ├── List all subscribers
    │   ├── View subscriber details
    │   ├── Create new subscriber
    │   ├── Edit subscriber
    │   └── Manage subscriptions
    │
    ├── Customers (/admin/customers)
    │   ├── List accounts
    │   ├── View account details
    │   ├── Manage contacts
    │   └── Impersonate customer
    │
    ├── Billing (/admin/billing)
    │   ├── Invoices
    │   ├── Payments
    │   ├── AR Aging
    │   └── Billing runs
    │
    ├── Network (/admin/network)
    │   ├── OLTs (Optical Line Terminals)
    │   ├── ONTs (Customer devices)
    │   ├── CPEs (Equipment)
    │   ├── VLANs
    │   ├── RADIUS accounts
    │   └── POP Sites
    │
    ├── Catalog (/admin/catalog)
    │   ├── Offers
    │   ├── Subscriptions
    │   └── Usage rating
    │
    ├── Reports (/admin/reports)
    │   ├── Revenue reports
    │   ├── Subscriber reports
    │   ├── Churn analysis
    │   ├── Network reports
    │   └── Technician reports
    │
    └── System (/admin/system)
        ├── Users & roles
        ├── Settings
        └── Audit logs
```

**Key Admin Workflows:**

1. **New Subscriber Onboarding**
   ```
   Create Account → Add Subscriber → Assign Subscription →
   Create Service Order → Schedule Installation → Provision Network
   ```

2. **Billing Cycle**
   ```
   Generate Invoices → Review AR Aging → Process Payments →
   Update Ledger → Send Notifications
   ```

---

### Customer Portal Journey

**Entry Point:** `/portal/dashboard`

```
Login (/portal/auth/login)
    │
    ▼
Dashboard (/portal/dashboard)
    │
    ├── Services (/portal/services)
    │   ├── View active services
    │   ├── Service details
    │   └── Usage statistics
    │
    ├── Billing (/portal/billing)
    │   ├── View invoices
    │   ├── Payment history
    │   └── Download statements
    │
    ├── Installations (/portal/installations)
    │   ├── View installation status
    │   └── Schedule appointment
    │
    ├── Service Orders (/portal/service-orders)
    │   └── Track order progress
    │
    └── Profile (/portal/profile)
        ├── Update contact info
        └── Change password
```

**Key Customer Workflows:**

1. **View Invoice and Track Arrangement**
   ```
   Dashboard → Billing → Select Invoice → View Details →
   Billing Arrangements
   ```

2. **Check Service Status**
   ```
   Dashboard → Services → Select Service → View Usage →
   Check Connection Status
   ```

---

### Reseller Portal Journey

**Entry Point:** `/reseller/dashboard`

```
Login (/reseller/auth/login)
    │
    ▼
Dashboard (/reseller/dashboard)
    │
    ├── Accounts (/reseller/accounts)
    │   ├── List managed accounts
    │   ├── View account details
    │   └── Impersonate customer
    │
    └── Reports
        └── Commission reports
```

**Key Reseller Workflows:**

1. **Manage Customer Account**
   ```
   Dashboard → Accounts → Select Account → View Details →
   Impersonate → Access Customer Portal as Customer
   ```

---

## UI Design Governance

UI work in this repo is governed by the design docs, not by ad hoc taste.

Read these before making substantial UI changes:

- `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`
- `docs/PRODUCTION_UI_BRIEF.md`
- `docs/FRONTEND_SPEC.md`
- `docs/DESIGN_REVIEW_CHECKLIST.md`

Use them in this order:

1. `UI_INFORMATION_AND_ACTION_STANDARD.md`
   This defines information relevance, progressive depth, state provenance, table contracts, and action ownership.
2. `PRODUCTION_UI_BRIEF.md`
   This defines density, page anatomy, semantic color use, dashboard rules, HTMX stability rules, and visual policy.
3. `FRONTEND_SPEC.md`
   This is the implementation layer. It documents context shapes, route/template contracts, macro usage, and module-specific UI expectations.
4. `DESIGN_REVIEW_CHECKLIST.md`
   This is the merge gate. Use it before submitting or approving UI-heavy changes.

### Required Workflow For UI Changes

For any admin UI change, redesign, shared macro update, or new template:

1. Define or review the page contract in `UI_INFORMATION_AND_ACTION_STANDARD.md`.
   Name the audience, decision, data owner, action owner, first-viewport information, and required depth.
2. Check the page type in `PRODUCTION_UI_BRIEF.md`.
   Determine whether the page is a dashboard, list page, detail page, or form-driven work surface.
3. Reuse shared macros first.
   Prefer `page_header`, `card`, `filter_bar`, `stats_card`, `data_table`, `status_badge`, and related macros over page-local patterns.
4. Keep the first viewport task-oriented.
   Do not let decorative treatments push the work surface below the fold.
5. Run the design checklist.
   Use `docs/DESIGN_REVIEW_CHECKLIST.md` and mark non-applicable items as `N/A`.
6. Reflect shared changes in docs.
   If you introduce a new standard UI pattern, update `docs/FRONTEND_SPEC.md`.

### Pull Request Expectation

UI-facing pull requests should complete the design section in:

- `.github/PULL_REQUEST_TEMPLATE.md`

That template is intentionally short. The detailed reasoning belongs in:

- `docs/DESIGN_REVIEW_CHECKLIST.md`

### Practical Rules

- One primary action per screen header.
- Use module accent colors for orientation, not state.
- Use semantic colors for status and risk.
- Keep dashboard KPI strips compact.
- Keep filters directly above tables on list pages.
- Preserve layout stability for HTMX-polled regions.
- Prefer denser operational layouts over showcase-style hero compositions.

---

## Implementation Guide

### Adding a New Feature

Follow this step-by-step guide to add a new feature to the application.

If the feature includes UI work, complete the UI design governance steps above before opening the PR.

#### Step 1: Create the Database Model

Create a new model file or add to an existing one in `app/models/`:

```python
# app/models/equipment.py
from sqlalchemy import Column, String, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
import enum

from app.db import Base

class EquipmentStatus(enum.Enum):
    AVAILABLE = "available"
    DEPLOYED = "deployed"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"

class Equipment(Base):
    __tablename__ = "equipment"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    serial_number = Column(String(100), unique=True, nullable=False)
    model = Column(String(100), nullable=False)
    status = Column(Enum(EquipmentStatus), default=EquipmentStatus.AVAILABLE)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id"))

    # Relationships
    location = relationship("Location", back_populates="equipment")
```

**Register the model** in `app/models/__init__.py`:

```python
from app.models.equipment import Equipment, EquipmentStatus
```

#### Step 2: Create the Service Layer

Create a registered query/command owner with typed contracts. The public
command owns the business transaction; helpers below it only flush:

```python
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.equipment import Equipment, EquipmentStatus


class EquipmentNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class CreateEquipment:
    serial_number: str
    model: str
    status: EquipmentStatus


@dataclass(frozen=True)
class EquipmentCreated:
    equipment_id: UUID


@dataclass(frozen=True)
class EquipmentPage:
    items: tuple[Equipment, ...]
    total: int


def list_page(
    db: Session,
    *,
    status: EquipmentStatus | None,
    limit: int,
    offset: int,
) -> EquipmentPage:
    filters = () if status is None else (Equipment.status == status,)
    items = tuple(
        db.scalars(
            select(Equipment)
            .where(*filters)
            .order_by(Equipment.serial_number)
            .limit(limit)
            .offset(offset)
        )
    )
    total = db.scalar(select(func.count()).select_from(Equipment).where(*filters))
    return EquipmentPage(items=items, total=total or 0)


def create(db: Session, command: CreateEquipment) -> EquipmentCreated:
    with db.begin():
        equipment = Equipment(
            serial_number=command.serial_number,
            model=command.model,
            status=command.status,
        )
        db.add(equipment)
        db.flush()
        # Stage the versioned domain event/outbox through its registered owner.
        outcome = EquipmentCreated(equipment_id=equipment.id)
    return outcome
```

Register the owner, its concerns, dependencies, entrypoints, and migration
state in its ownership-aligned module under
`app/services/sot_registry/domains/`. The canonical aggregate in
`app/services/sot_registry/registry.py`, relationship map, and architecture
tests must agree in the same change.

#### Step 3: Create Web Routes

Create a route file for the admin portal:

```python
# app/web/admin/equipment.py
from uuid import UUID
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import equipment as equipment_service
from app.models.equipment import EquipmentStatus
from app.web.admin import templates

router = APIRouter(prefix="/equipment", tags=["Equipment"])

# Pagination settings
PER_PAGE = 20


def _base_context(request: Request, active_page: str = "equipment"):
    """Build base template context."""
    return {
        "request": request,
        "active_page": active_page,
    }


@router.get("", response_class=HTMLResponse)
def list_equipment(
    request: Request,
    page: int = 1,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List all equipment."""
    offset = (page - 1) * PER_PAGE

    # Parse status filter
    status_filter = None
    if status:
        try:
            status_filter = EquipmentStatus(status)
        except ValueError:
            pass

    result = equipment_service.list_page(
        db,
        status=status_filter,
        limit=PER_PAGE,
        offset=offset
    )
    total_pages = (result.total + PER_PAGE - 1) // PER_PAGE

    context = _base_context(request)
    context.update({
        "equipment_list": result.items,
        "page": page,
        "total_pages": total_pages,
        "status_filter": status,
        "statuses": [s.value for s in EquipmentStatus],
    })

    return templates.TemplateResponse("admin/equipment/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def new_equipment_form(request: Request, db: Session = Depends(get_db)):
    """Show create equipment form."""
    context = _base_context(request)
    context.update({
        "statuses": [s.value for s in EquipmentStatus],
        "equipment": None,  # Empty for new
    })
    return templates.TemplateResponse("admin/equipment/form.html", context)


@router.post("", response_class=HTMLResponse)
def create_equipment(
    request: Request,
    serial_number: str = Form(...),
    model: str = Form(...),
    status: str = Form("available"),
    db: Session = Depends(get_db),
):
    """Create new equipment."""
    result = equipment_service.create(
        db,
        equipment_service.CreateEquipment(
            serial_number=serial_number,
            model=model,
            status=EquipmentStatus(status),
        ),
    )

    return RedirectResponse(
        url=f"/admin/equipment/{result.equipment_id}",
        status_code=303
    )


@router.get("/{equipment_id}", response_class=HTMLResponse)
def view_equipment(
    request: Request,
    equipment_id: UUID,
    db: Session = Depends(get_db),
):
    """View equipment details."""
    equipment = equipment_service.equipment.get(db, equipment_id)

    if not equipment:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Equipment not found"},
            status_code=404
        )

    context = _base_context(request)
    context["equipment"] = equipment

    return templates.TemplateResponse("admin/equipment/detail.html", context)


@router.get("/{equipment_id}/edit", response_class=HTMLResponse)
def edit_equipment_form(
    request: Request,
    equipment_id: UUID,
    db: Session = Depends(get_db),
):
    """Show edit equipment form."""
    equipment = equipment_service.equipment.get(db, equipment_id)

    if not equipment:
        return RedirectResponse(url="/admin/equipment", status_code=303)

    context = _base_context(request)
    context.update({
        "equipment": equipment,
        "statuses": [s.value for s in EquipmentStatus],
    })

    return templates.TemplateResponse("admin/equipment/form.html", context)


@router.post("/{equipment_id}", response_class=HTMLResponse)
def update_equipment(
    request: Request,
    equipment_id: UUID,
    serial_number: str = Form(...),
    model: str = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    """Update equipment."""
    data = {
        "serial_number": serial_number,
        "model": model,
        "status": EquipmentStatus(status),
    }

    equipment_service.equipment.update(db, equipment_id, data)

    return RedirectResponse(
        url=f"/admin/equipment/{equipment_id}",
        status_code=303
    )


@router.post("/{equipment_id}/delete", response_class=HTMLResponse)
def delete_equipment(
    request: Request,
    equipment_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete equipment."""
    equipment_service.equipment.delete(db, equipment_id)

    return RedirectResponse(url="/admin/equipment", status_code=303)
```

#### Step 4: Register Routes

Add the router to `app/web/admin/__init__.py`:

```python
from app.web.admin.equipment import router as equipment_router

# In the router setup section:
router.include_router(equipment_router)
```

#### Step 5: Create Templates

**List View** - `templates/admin/equipment/index.html`:

```html
{% extends "layouts/admin.html" %}

{% block breadcrumbs %}
<a href="/admin/dashboard" class="text-slate-500 hover:text-slate-700 dark:text-slate-400">Dashboard</a>
<span class="text-slate-400 dark:text-slate-600 mx-2">/</span>
<span class="text-slate-900 dark:text-white">Equipment</span>
{% endblock %}

{% block page_header %}
<div class="flex items-center justify-between mb-6">
    <div>
        <h1 class="text-2xl font-bold text-slate-900 dark:text-white">Equipment</h1>
        <p class="text-slate-500 dark:text-slate-400">Manage network equipment inventory</p>
    </div>
    <a href="/admin/equipment/new"
       class="inline-flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700">
        <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
        </svg>
        Add Equipment
    </a>
</div>
{% endblock %}

{% block content %}
<!-- Filters -->
<div class="mb-6 flex items-center gap-4">
    <form method="GET" class="flex items-center gap-2">
        <select name="status"
                class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800"
                onchange="this.form.submit()">
            <option value="">All Statuses</option>
            {% for s in statuses %}
            <option value="{{ s }}" {{ 'selected' if status_filter == s else '' }}>{{ s | title }}</option>
            {% endfor %}
        </select>
    </form>
</div>

<!-- Table -->
<div class="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800">
    <table class="min-w-full divide-y divide-slate-200 dark:divide-slate-700">
        <thead class="bg-slate-50 dark:bg-slate-900">
            <tr>
                <th class="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    Serial Number
                </th>
                <th class="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    Model
                </th>
                <th class="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    Status
                </th>
                <th class="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    Actions
                </th>
            </tr>
        </thead>
        <tbody class="divide-y divide-slate-200 dark:divide-slate-700">
            {% for item in equipment_list %}
            <tr class="hover:bg-slate-50 dark:hover:bg-slate-700/50">
                <td class="px-6 py-4 whitespace-nowrap">
                    <a href="/admin/equipment/{{ item.id }}"
                       class="text-sm font-medium text-primary-600 hover:text-primary-700 dark:text-primary-400">
                        {{ item.serial_number }}
                    </a>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-slate-900 dark:text-white">
                    {{ item.model }}
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                    {% set status_colors = {
                        'available': 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
                        'deployed': 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
                        'maintenance': 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
                        'retired': 'bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-400'
                    } %}
                    <span class="inline-flex rounded-full px-2 py-1 text-xs font-medium {{ status_colors.get(item.status.value, '') }}">
                        {{ item.status.value | title }}
                    </span>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-right text-sm">
                    <a href="/admin/equipment/{{ item.id }}/edit"
                       class="text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white">
                        Edit
                    </a>
                </td>
            </tr>
            {% else %}
            <tr>
                <td colspan="4" class="px-6 py-12 text-center text-slate-500 dark:text-slate-400">
                    No equipment found
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>

<!-- Pagination -->
{% if total_pages > 1 %}
<div class="mt-6 flex items-center justify-between">
    <p class="text-sm text-slate-500 dark:text-slate-400">
        Page {{ page }} of {{ total_pages }}
    </p>
    <div class="flex gap-2">
        {% if page > 1 %}
        <a href="?page={{ page - 1 }}{% if status_filter %}&status={{ status_filter }}{% endif %}"
           class="rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-700">
            Previous
        </a>
        {% endif %}
        {% if page < total_pages %}
        <a href="?page={{ page + 1 }}{% if status_filter %}&status={{ status_filter }}{% endif %}"
           class="rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-700">
            Next
        </a>
        {% endif %}
    </div>
</div>
{% endif %}
{% endblock %}
```

**Form Template** - `templates/admin/equipment/form.html`:

```html
{% extends "layouts/admin.html" %}

{% set is_edit = equipment is not none %}

{% block breadcrumbs %}
<a href="/admin/dashboard" class="text-slate-500 hover:text-slate-700 dark:text-slate-400">Dashboard</a>
<span class="text-slate-400 dark:text-slate-600 mx-2">/</span>
<a href="/admin/equipment" class="text-slate-500 hover:text-slate-700 dark:text-slate-400">Equipment</a>
<span class="text-slate-400 dark:text-slate-600 mx-2">/</span>
<span class="text-slate-900 dark:text-white">{{ 'Edit' if is_edit else 'New' }}</span>
{% endblock %}

{% block page_header %}
<div class="mb-6">
    <h1 class="text-2xl font-bold text-slate-900 dark:text-white">
        {{ 'Edit Equipment' if is_edit else 'Add Equipment' }}
    </h1>
</div>
{% endblock %}

{% block content %}
<div class="max-w-2xl">
    <form method="POST"
          action="{{ '/admin/equipment/' ~ equipment.id if is_edit else '/admin/equipment' }}"
          class="space-y-6">

        <!-- Serial Number -->
        <div>
            <label for="serial_number" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                Serial Number
            </label>
            <input type="text"
                   id="serial_number"
                   name="serial_number"
                   value="{{ equipment.serial_number if equipment else '' }}"
                   required
                   class="w-full rounded-lg border border-slate-300 px-4 py-2 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-slate-600 dark:bg-slate-800 dark:text-white">
        </div>

        <!-- Model -->
        <div>
            <label for="model" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                Model
            </label>
            <input type="text"
                   id="model"
                   name="model"
                   value="{{ equipment.model if equipment else '' }}"
                   required
                   class="w-full rounded-lg border border-slate-300 px-4 py-2 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-slate-600 dark:bg-slate-800 dark:text-white">
        </div>

        <!-- Status -->
        <div>
            <label for="status" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                Status
            </label>
            <select id="status"
                    name="status"
                    class="w-full rounded-lg border border-slate-300 px-4 py-2 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-slate-600 dark:bg-slate-800 dark:text-white">
                {% for s in statuses %}
                <option value="{{ s }}" {{ 'selected' if equipment and equipment.status.value == s else '' }}>
                    {{ s | title }}
                </option>
                {% endfor %}
            </select>
        </div>

        <!-- Actions -->
        <div class="flex items-center gap-4 pt-4">
            <button type="submit"
                    class="rounded-lg bg-primary-600 px-6 py-2 text-sm font-medium text-white hover:bg-primary-700">
                {{ 'Update' if is_edit else 'Create' }} Equipment
            </button>
            <a href="/admin/equipment"
               class="text-sm text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white">
                Cancel
            </a>
        </div>
    </form>

    {% if is_edit %}
    <div class="mt-12 pt-6 border-t border-slate-200 dark:border-slate-700">
        <h3 class="text-lg font-medium text-red-600 dark:text-red-400 mb-4">Danger Zone</h3>
        <form method="POST" action="/admin/equipment/{{ equipment.id }}/delete"
              onsubmit="return confirm('Are you sure you want to delete this equipment?')">
            <button type="submit"
                    class="rounded-lg border border-red-300 px-4 py-2 text-sm text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-900/20">
                Delete Equipment
            </button>
        </form>
    </div>
    {% endif %}
</div>
{% endblock %}
```

#### Step 6: Add to Sidebar Navigation

Update `templates/components/navigation/admin_sidebar.html`:

```html
<!-- Add in the appropriate section -->
<a href="/admin/equipment"
   class="flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium {{ 'bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-white' if active_page == 'equipment' else 'text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white' }}">
    <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z"/>
    </svg>
    <span x-show="!sidebarCollapsed">Equipment</span>
</a>
```

---

## Code Patterns & Best Practices

### Route Patterns

#### 1. Standard CRUD Pattern

```python
@router.get("")                    # List
@router.get("/new")               # Create form
@router.post("")                  # Create action
@router.get("/{id}")              # View detail
@router.get("/{id}/edit")         # Edit form
@router.post("/{id}")             # Update action
@router.post("/{id}/delete")      # Delete action
```

#### 2. HTMX Partial Pattern

For dynamic updates without full page refresh:

```python
@router.get("/dashboard/stats", response_class=HTMLResponse)
def dashboard_stats(request: Request, db: Session = Depends(get_db)):
    """Return stats partial for HTMX."""
    stats = get_dashboard_stats(db)
    return templates.TemplateResponse(
        "admin/dashboard/_stats.html",
        {"request": request, "stats": stats}
    )
```

In template:
```html
<div hx-get="/admin/dashboard/stats"
     hx-trigger="load, every 30s"
     hx-swap="innerHTML">
    Loading...
</div>
```

#### 3. POST-Redirect-GET Pattern

Always redirect after POST to prevent form resubmission:

```python
@router.post("")
def create_item(db: Session = Depends(get_db)):
    item = service.create(db, data)
    return RedirectResponse(
        url=f"/admin/items/{item.id}",
        status_code=303  # See Other
    )
```

### Service Patterns

#### 1. Command and Query Owner Pattern

```python
def list_items(db: Session, query: ListItems) -> ItemPage:
    """Read-only query owner: filters, count, ordering, and page agree."""
    ...


def create_item(db: Session, command: CreateItem) -> ItemCreated:
    """Public command owner: typed intent and one atomic transaction."""
    with db.begin():
        item = Item(name=command.name, item_type=command.item_type)
        db.add(item)
        db.flush()
        # Stage audit evidence and the versioned domain event here.
        outcome = ItemCreated(item_id=item.id)
    return outcome
```

#### 2. Transaction Pattern

```python
def complex_operation(db: Session, command: LinkItems) -> ItemsLinked:
    with db.begin():
        item1 = _create_item(db, command.first)  # nested helper: flush only
        item2 = _create_item(db, command.second)
        _link_items(db, item1, item2)
        outcome = ItemsLinked(first_id=item1.id, second_id=item2.id)
    return outcome
```

Routes, tasks, event handlers, and CLI adapters create/close the session and
map domain outcomes or errors. They do not call `commit()`, `rollback()`,
`flush()`, `begin()`, or ORM mutation methods.

### Template Patterns

#### 1. Layout Inheritance

```
base.html
    └── layouts/admin.html
            └── admin/equipment/index.html
```

#### 2. Component Inclusion

```html
{% include "components/forms/text_input.html" with context %}
{% include "components/data/pagination.html" %}
```

#### 3. Alpine.js State Management

```html
<!-- Local state -->
<div x-data="{ open: false }">
    <button @click="open = !open">Toggle</button>
    <div x-show="open">Content</div>
</div>

<!-- Global store -->
<button @click="$store.darkMode.toggle()">
    Toggle Dark Mode
</button>
```

---

## Working with Templates

### Template Context

Every template receives a context dictionary:

```python
context = {
    "request": request,           # FastAPI Request object
    "active_page": "equipment",   # For sidebar highlighting
    "current_user": user,         # Authenticated user
    # ... page-specific data
}
```

### Common Jinja2 Filters

```html
{{ date | format_date }}
{{ amount | currency }}
{{ status.value | title }}
{{ text | truncate(50) }}
{{ items | length }}
```

### Conditional Classes

```html
<div class="{{ 'bg-green-100' if item.active else 'bg-red-100' }}">
```

### Loops with Index

```html
{% for item in items %}
<div class="stagger-in" style="animation-delay: {{ loop.index0 * 75 }}ms">
    {{ item.name }}
</div>
{% endfor %}
```

---

## Database & Models

### Running Migrations

```bash
# Create migration
alembic revision --autogenerate -m "Add equipment table"

# Run migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

### Model Relationships

```python
# One-to-Many
class Account(Base):
    subscribers = relationship("Subscriber", back_populates="account")

class Subscriber(Base):
    account_id = Column(UUID, ForeignKey("accounts.id"))
    account = relationship("Account", back_populates="subscribers")

# Many-to-Many
class User(Base):
    roles = relationship("Role", secondary=user_roles, back_populates="users")
```

---

## Authentication & Authorization

### Web Authentication

```python
from app.web.auth.dependencies import require_web_auth

@router.get("/protected")
def protected_route(
    request: Request,
    user = Depends(require_web_auth)
):
    # user is authenticated
    pass
```

### Portal Sessions

```python
# Customer portal
from app.services.customer_portal import get_current_customer_from_request

@router.get("/dashboard")
def customer_dashboard(request: Request, db: Session = Depends(get_db)):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse("/portal/auth/login")
```

### Role-Based Access

```python
from app.services.auth_dependencies import require_permission

@router.post("/admin/users")
def create_user(
    request: Request,
    _auth: dict = Depends(require_permission("system:write"))
):
    # User has permission
    pass
```

---

## Testing

### Running Tests

```bash
# All tests
pytest

# Specific module
pytest tests/test_equipment.py

# With coverage
pytest --cov=app
```

### Test Structure

```python
# tests/test_equipment.py
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_list_equipment():
    response = client.get("/admin/equipment")
    assert response.status_code == 200

def test_create_equipment():
    response = client.post("/admin/equipment", data={
        "serial_number": "TEST-001",
        "model": "Test Model",
        "status": "available"
    })
    assert response.status_code == 303  # Redirect
```

---

## DEM Data (SRTM 30m)

The GIS elevation endpoint reads SRTM 30m tiles stored locally in this repo/environment.

- **Default path**: `data/dem/srtm`
- **Override**: set `DEM_DATA_DIR` in the environment
- **Format**: raw `.hgt` files (or `.hgt.zip`) named by tile, e.g. `N06E003.hgt`
- **API**: `GET /gis/elevation?latitude=<lat>&longitude=<lon>`

Tile naming uses the lower-left corner of each 1x1 degree tile:

- `N06E003.hgt` covers lat 6-7, lon 3-4
- `S02E005.hgt` covers lat -2 to -1, lon 5-6

---

## Quick Reference

### File Locations

| What | Where |
|------|-------|
| Models | `app/models/` |
| Services | `app/services/` |
| Web Routes | `app/web/{portal}/` |
| API Routes | `app/api/` |
| Templates | `templates/{portal}/` |
| Components | `templates/components/` |
| Static Files | `static/` |
| CSS Source | `static/css/src/main.css` |

### Common Commands

```bash
# Start development server
docker compose up

# Rebuild CSS
npm run css:build

# Watch CSS changes
npm run css:watch

# Run tests
pytest

# Database migrations
alembic upgrade head
```

### URL Patterns

| Portal | Base URL | Auth URL |
|--------|----------|----------|
| Admin | `/admin` | `/auth/login` |
| Customer | `/portal` | `/portal/auth/login` |
| Reseller | `/reseller` | `/reseller/auth/login` |

---

## Need Help?

- Check existing implementations in `app/web/admin/` for examples
- Review `templates/admin/` for template patterns
- Look at `app/services/` for service layer patterns
