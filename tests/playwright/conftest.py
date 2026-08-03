from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

# The root tests/conftest.py deliberately poisons DATABASE_URL so unit tests
# can't touch a real deployment DB — but these e2e fixtures DO need the live
# (disposable) database the app under test uses. Honor TEST_DATABASE_URL here;
# app.db.SessionLocal is already bound to the poisoned URL by import order.
# Same un-poisoning for Redis: the customer-session store reads REDIS_URL at
# call time, so restoring the env here lets the e2e fixtures write sessions the
# app under test can validate.
_e2e_redis_url = os.getenv("E2E_REDIS_URL")
if _e2e_redis_url:
    os.environ["REDIS_URL"] = _e2e_redis_url
    os.environ["SESSION_REDIS_URL"] = _e2e_redis_url

_e2e_database_url = os.getenv("TEST_DATABASE_URL")
if _e2e_database_url:
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    SessionLocal = _sessionmaker(
        bind=_create_engine(_e2e_database_url), expire_on_commit=False
    )
else:
    from app.db import SessionLocal
from app.models.catalog import (
    CatalogOffer,
    OfferStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.schemas.catalog import SubscriptionCreate
from app.services import catalog as catalog_service
from app.services import customer_portal, reseller_portal
from tests.playwright.helpers.api import api_post_form, bearer_headers
from tests.playwright.helpers.auth import (
    ensure_person,
    ensure_person_role,
    ensure_role_id,
    ensure_user_credential,
    login_for_token,
)
from tests.playwright.helpers.config import E2ESettings
from tests.playwright.helpers.data import ensure_person_subscriber_account

CUSTOMER_PORTAL_PASSWORD = "CustomerPass123!"
RESELLER_PORTAL_USERNAME = "e2e.reseller@example.com"
RESELLER_PORTAL_PASSWORD = "ResellerPass123!"

# --- Known-failure quarantine (ratchet) -----------------------------------
#
# tests/playwright/quarantine.json lists specs with a known, tracked failure.
# Quarantined specs run as strict xfail: a failure is reported as xfailed (the
# suite stays green), while a PASS is reported as a failure (XPASS strict) so
# a repaired spec MUST be removed from the file. Any spec not listed fails the
# run normally — the failure count can only go down. Entries whose review_by
# date has passed lose their protection and fail the run again until
# re-reviewed. Setup errors cannot be xfailed; entries may set
# "action": "skip" for those, which skips the spec entirely. Specs that
# pass and fail intermittently may set "action": "flaky" (non-strict
# xfail: neither outcome breaks the run) until repaired — expiry still
# forces review.

_QUARANTINE_PATH = Path(__file__).with_name("quarantine.json")
_QUARANTINE_REQUIRED_FIELDS = (
    "spec",
    "reason",
    "issue",
    "category",
    "owner",
    "review_by",
)


def _load_quarantine() -> dict[str, dict[str, str]]:
    if not _QUARANTINE_PATH.exists():
        return {}
    entries = json.loads(_QUARANTINE_PATH.read_text(encoding="utf-8"))
    quarantine: dict[str, dict[str, str]] = {}
    for entry in entries:
        missing = [
            field for field in _QUARANTINE_REQUIRED_FIELDS if not entry.get(field)
        ]
        if missing:
            raise pytest.UsageError(
                f"quarantine.json entry {entry.get('spec', '<no spec>')!r} "
                f"is missing required fields: {missing}"
            )
        _dt.date.fromisoformat(entry["review_by"])
        if entry.get("action", "xfail") not in ("xfail", "skip", "flaky"):
            raise pytest.UsageError(
                f"quarantine.json entry {entry['spec']!r} has unknown action "
                f"{entry['action']!r} (expected 'xfail', 'skip', or 'flaky')"
            )
        if entry["spec"] in quarantine:
            raise pytest.UsageError(f"quarantine.json lists {entry['spec']!r} twice")
        quarantine[entry["spec"]] = entry
    return quarantine


def pytest_collection_modifyitems(config, items) -> None:
    quarantine = _load_quarantine()
    if not quarantine:
        return
    today = _dt.date.today()
    for item in items:
        entry = quarantine.get(item.nodeid)
        if entry is None:
            continue
        review_by = _dt.date.fromisoformat(entry["review_by"])
        if review_by < today:
            # Protection lapsed: the spec fails the run again until the entry
            # is re-reviewed (new date) or the spec is repaired and removed.
            continue
        reason = (
            f"quarantined ({entry['category']}, {entry['issue']}, "
            f"review by {entry['review_by']}): {entry['reason']}"
        )
        if entry.get("action") == "skip":
            item.add_marker(pytest.mark.skip(reason=reason))
        elif entry.get("action") == "flaky":
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
        else:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))


def _latest_subscription_id(db, subscriber_id: str) -> str | None:
    subscription = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if not subscription:
        return None
    return str(subscription.id)


def _ensure_active_customer_subscription(subscriber_id: str) -> str | None:
    db = SessionLocal()
    try:
        existing = (
            db.query(Subscription)
            .filter(
                Subscription.subscriber_id == subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
            .order_by(Subscription.created_at.desc())
            .first()
        )
        if existing is not None:
            return str(existing.id)

        offer = (
            db.query(CatalogOffer)
            .filter(
                CatalogOffer.status == OfferStatus.active,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(CatalogOffer.created_at.asc())
            .first()
        )
        if offer is None:
            return None

        subscription = catalog_service.subscriptions.create(
            db,
            SubscriptionCreate(
                subscriber_id=UUID(subscriber_id),
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                billing_mode=offer.billing_mode,
                contract_term=offer.contract_term,
            ),
        )
        return str(subscription.id)
    finally:
        db.close()


def _ensure_reseller_setup(
    reseller_subscriber_id: str, customer_subscriber_id: str
) -> dict[str, str]:
    db = SessionLocal()
    try:
        reseller_subscriber = db.get(Subscriber, reseller_subscriber_id)
        customer_subscriber = db.get(Subscriber, customer_subscriber_id)
        assert reseller_subscriber is not None
        assert customer_subscriber is not None

        reseller = (
            db.query(Reseller)
            .filter(Reseller.contact_email == reseller_subscriber.email)
            .first()
        )
        if reseller is None:
            reseller = Reseller(
                name="E2E Reseller",
                code="E2E-RESELLER",
                contact_email=reseller_subscriber.email,
                is_active=True,
            )
            db.add(reseller)
            db.flush()

        reseller_subscriber.user_type = UserType.reseller
        reseller_subscriber.reseller_id = reseller.id
        customer_subscriber.reseller_id = reseller.id

        # The reseller portal validates sessions via the reseller_users link
        # table (reseller_portal._get_reseller_user). Without this row, every
        # authenticated reseller page redirects to login.
        reseller_user = (
            db.query(ResellerUser)
            .filter(ResellerUser.subscriber_id == reseller_subscriber.id)
            .first()
        )
        if reseller_user is None:
            db.add(
                ResellerUser(
                    subscriber_id=reseller_subscriber.id,
                    reseller_id=reseller.id,
                    is_active=True,
                )
            )
        else:
            reseller_user.reseller_id = reseller.id
            reseller_user.is_active = True

        db.commit()
        return {
            "reseller_id": str(reseller.id),
            "subscriber_id": str(reseller_subscriber.id),
        }
    finally:
        db.close()


# Production hosts this destructive E2E suite must never run against. The tests
# create and modify real data (offers, customers, subscriptions) through the live
# UI/API, and have polluted prod before (the "E2E Phase 1" catalog offers + e2e
# test subscribers on selfcare.dotmac.io). Configurable via env; override the
# block only deliberately with PLAYWRIGHT_ALLOW_PRODUCTION=1.
_PRODUCTION_HOSTS = {
    h.strip().lower()
    for h in os.getenv(
        "PLAYWRIGHT_PRODUCTION_HOSTS", "selfcare.dotmac.io,selfcare1.dotmac.io"
    ).split(",")
    if h.strip()
}


def _guard_not_production(base_url: str) -> None:
    host = (urlparse(base_url).hostname or "").lower()
    if host in _PRODUCTION_HOSTS and os.getenv("PLAYWRIGHT_ALLOW_PRODUCTION") != "1":
        pytest.fail(
            f"Refusing to run the E2E suite against production host {host!r}: these "
            "tests create and modify real data (offers, customers, subscriptions). "
            "Point PLAYWRIGHT_BASE_URL at a disposable test/staging environment. "
            "Override only deliberately with PLAYWRIGHT_ALLOW_PRODUCTION=1.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def settings() -> E2ESettings:
    if os.getenv("PLAYWRIGHT_BASE_URL") is None:
        pytest.skip("Set PLAYWRIGHT_BASE_URL to run Playwright E2E tests.")
    settings = E2ESettings.from_env()
    _guard_not_production(settings.base_url)
    expect.set_options(timeout=settings.expect_timeout_ms)
    return settings


@pytest.fixture()
def e2e_db():
    """Open the explicitly configured disposable E2E database for test setup."""

    if not _e2e_database_url:
        pytest.skip("Set TEST_DATABASE_URL for E2E fixtures that prepare DB state.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance, settings: E2ESettings):
    browser_name = settings.browser
    browser_type = getattr(playwright_instance, browser_name, None)
    if browser_type is None:
        browser_type = playwright_instance.firefox

    launch_kwargs: dict[str, Any] = {
        "headless": settings.headless,
        "slow_mo": settings.slow_mo_ms,
        "timeout": settings.navigation_timeout_ms,
    }
    if browser_type is playwright_instance.chromium:
        launch_kwargs["args"] = [
            "--headless=new",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-crash-reporter",
        ]

    browser = browser_type.launch(**launch_kwargs)
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def api_context(playwright_instance, settings: E2ESettings):
    context = playwright_instance.request.new_context(base_url=settings.base_url)
    yield context
    context.dispose()


def _require_admin_credentials(settings: E2ESettings) -> tuple[str, str]:
    if not settings.admin_username or not settings.admin_password:
        pytest.skip("Set E2E_ADMIN_USERNAME and E2E_ADMIN_PASSWORD to run E2E tests.")
    return settings.admin_username, settings.admin_password


def _login_for_token_via_browser(
    browser,
    settings: E2ESettings,
    username: str,
    password: str,
) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        context = browser.new_context()
        context.set_default_timeout(settings.action_timeout_ms)
        context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        page = context.new_page()
        try:
            page.goto(f"{settings.base_url}/auth/login", wait_until="domcontentloaded")
            result = page.evaluate(
                """async ({ username, password }) => {
                    const response = await fetch('/api/v1/auth/login', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ username, password }),
                        credentials: 'same-origin',
                    });
                    let payload = null;
                    try {
                        payload = await response.json();
                    } catch (error) {
                        payload = null;
                    }
                    return { status: response.status, payload };
                }""",
                {"username": username, "password": password},
            )
            if result["status"] != 200:
                raise RuntimeError(f"Browser API login failed: {result['status']}")
            payload = result.get("payload") or {}
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Browser API login missing access_token")
            return token
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                continue
        finally:
            context.close()
    raise RuntimeError(f"Browser API login failed after retries: {last_error}")


@pytest.fixture(scope="session")
def admin_token(settings: E2ESettings, api_context, browser) -> str:
    username, password = _require_admin_credentials(settings)
    try:
        return login_for_token(api_context, username, password)
    except Exception:
        return _login_for_token_via_browser(browser, settings, username, password)


@pytest.fixture(scope="session")
def test_identities(settings: E2ESettings, api_context, admin_token: str) -> dict:
    headers = bearer_headers(admin_token)

    agent_email = _email_for_username(settings.agent_username)
    agent_person = ensure_person(
        api_context, admin_token, "Agent", "Support", agent_email
    )
    ensure_user_credential(
        api_context,
        admin_token,
        agent_person["id"],
        settings.agent_username,
        settings.agent_password,
    )
    support_role_id = ensure_role_id(api_context, admin_token, "support")
    ensure_person_role(api_context, admin_token, agent_person["id"], support_role_id)

    user_email = _email_for_username(settings.user_username)
    user_person = ensure_person(api_context, admin_token, "Portal", "User", user_email)
    ensure_user_credential(
        api_context,
        admin_token,
        user_person["id"],
        settings.user_username,
        settings.user_password,
    )

    customer_profile = ensure_person_subscriber_account(
        api_context,
        admin_token,
        "Customer",
        "Tester",
        "e2e.customer@example.com",
    )
    ensure_user_credential(
        api_context,
        admin_token,
        customer_profile["person"]["id"],
        customer_profile["person"]["email"],
        CUSTOMER_PORTAL_PASSWORD,
    )
    customer_profile["subscription_id"] = _ensure_active_customer_subscription(
        customer_profile["account"]["id"]
    )

    reseller_person = ensure_person(
        api_context,
        admin_token,
        "Reseller",
        "Tester",
        RESELLER_PORTAL_USERNAME,
    )
    ensure_user_credential(
        api_context,
        admin_token,
        reseller_person["id"],
        RESELLER_PORTAL_USERNAME,
        RESELLER_PORTAL_PASSWORD,
    )
    reseller_profile = {
        "person": reseller_person,
        **_ensure_reseller_setup(
            reseller_person["id"], customer_profile["person"]["id"]
        ),
    }

    return {
        "agent": agent_person,
        "user": user_person,
        "customer": customer_profile,
        "reseller": reseller_profile,
    }


@pytest.fixture(scope="session")
def agent_token(settings: E2ESettings, api_context, test_identities: dict) -> str:
    return login_for_token(
        api_context, settings.agent_username, settings.agent_password
    )


@pytest.fixture(scope="session")
def user_token(settings: E2ESettings, api_context, test_identities: dict) -> str:
    return login_for_token(api_context, settings.user_username, settings.user_password)


def _storage_state_path(role: str) -> Path:
    return Path(__file__).parent / ".auth" / f"{role}.json"


def _write_storage_state(
    _browser, settings: E2ESettings, token: str, role: str
) -> Path:
    path = _storage_state_path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(settings.base_url)
    cookie = {
        "name": "session_token",
        "value": token,
        "domain": parsed.hostname or "127.0.0.1",
        "path": "/",
        "httpOnly": True,
        "secure": parsed.scheme == "https",
        "sameSite": "Lax",
    }
    if parsed.port:
        cookie["domain"] = parsed.hostname or "127.0.0.1"
    path.write_text(
        json.dumps(
            {
                "cookies": [cookie],
                "origins": [],
            }
        )
    )
    return path


@pytest.fixture(scope="session")
def admin_storage_state(browser, settings: E2ESettings) -> Path:
    username, password = _require_admin_credentials(settings)
    path = _storage_state_path("admin")
    path.parent.mkdir(parents=True, exist_ok=True)
    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    page = context.new_page()
    try:
        page.goto(f"{settings.base_url}/auth/login", wait_until="domcontentloaded")
        page.get_by_label("Email or Username").fill(username)
        page.get_by_label("Password").fill(password)
        with page.expect_response(
            lambda response: (
                response.url.endswith("/auth/login")
                and response.request.method == "POST"
            ),
            timeout=min(settings.navigation_timeout_ms, 60_000),
        ) as login_response:
            page.get_by_role("button", name="Sign in").click(no_wait_after=True)
        response = login_response.value
        if response.status != 303:
            page.wait_for_load_state("domcontentloaded")
            error = page.locator("div.bg-red-50 p").first
            detail = error.inner_text() if error.count() else "no rendered error"
            pytest.fail(f"Admin web login returned HTTP {response.status}: {detail}")
        page.wait_for_url(
            "**/admin/dashboard**",
            wait_until="commit",
            timeout=min(settings.navigation_timeout_ms, 60_000),
        )
        context.storage_state(path=path)
        return path
    finally:
        context.close()


@pytest.fixture(scope="session")
def agent_storage_state(browser, settings: E2ESettings, agent_token: str) -> Path:
    return _write_storage_state(browser, settings, agent_token, "agent")


@pytest.fixture(scope="session")
def user_storage_state(browser, settings: E2ESettings, user_token: str) -> Path:
    return _write_storage_state(browser, settings, user_token, "user")


@pytest.fixture()
def admin_auth_api_context(
    playwright_instance, settings: E2ESettings, admin_storage_state: Path
):
    context = playwright_instance.request.new_context(
        base_url=settings.base_url,
        storage_state=str(admin_storage_state),
    )
    yield context
    context.dispose()


_TOUR_SUPPRESS_SCRIPT = "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"


def _suppress_admin_tour(context) -> None:
    """The first-login quick tour opens an aria-modal dialog that removes the
    whole page from the accessibility tree; specs must see the page, not the
    tour."""

    context.add_init_script(_TOUR_SUPPRESS_SCRIPT)


@pytest.fixture()
def admin_context(browser, settings: E2ESettings, admin_storage_state: Path):
    context = browser.new_context(storage_state=admin_storage_state)
    _suppress_admin_tour(context)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    try:
        context.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def agent_context(browser, settings: E2ESettings, agent_storage_state: Path):
    context = browser.new_context(storage_state=agent_storage_state)
    _suppress_admin_tour(context)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    try:
        context.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def user_context(browser, settings: E2ESettings, user_storage_state: Path):
    context = browser.new_context(storage_state=user_storage_state)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    try:
        context.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def admin_page(admin_context):
    page = admin_context.new_page()
    yield page
    try:
        if not page.is_closed():
            page.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def agent_page(agent_context):
    page = agent_context.new_page()
    yield page
    try:
        if not page.is_closed():
            page.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def user_page(user_context):
    page = user_context.new_page()
    yield page
    try:
        if not page.is_closed():
            page.close()
    except PlaywrightError:
        pass


@pytest.fixture()
def admin_impersonate_response(api_context, admin_token: str, test_identities: dict):
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id},
        headers=bearer_headers(admin_token),
    )
    return response


@pytest.fixture()
def agent_impersonate_response(api_context, agent_token: str, test_identities: dict):
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id},
        headers=bearer_headers(agent_token),
    )
    return response


def _email_for_username(username: str) -> str:
    if "@" in username:
        return username
    return f"{username}@example.com"


# Customer Portal Fixtures
# Customer portal uses impersonation from admin since RADIUS auth setup is complex


@pytest.fixture()
def customer_context(browser, settings: E2ESettings, test_identities: dict):
    """Browser context with a direct customer portal session cookie."""
    customer = test_identities["customer"]
    account_id = customer["account"]["id"]
    username = customer["person"]["email"]
    db = SessionLocal()
    try:
        customer_session = customer_portal.create_customer_session(
            username=username,
            account_id=account_id,
            subscriber_id=account_id,
            subscription_id=_latest_subscription_id(db, account_id),
            db=db,
        )
    finally:
        db.close()

    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)

    parsed = urlparse(settings.base_url)
    context.add_cookies(
        [
            {
                "name": "customer_session",
                "value": customer_session,
                "url": settings.base_url,
                "httpOnly": True,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        ]
    )
    yield context
    context.close()


@pytest.fixture()
def customer_page(customer_context):
    """Page for customer portal testing."""
    page = customer_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def customer_api_context(
    playwright_instance, settings: E2ESettings, test_identities: dict
):
    """Request context with a real customer portal session cookie."""
    customer = test_identities["customer"]
    account_id = customer["account"]["id"]
    username = customer["person"]["email"]
    db = SessionLocal()
    try:
        customer_session = customer_portal.create_customer_session(
            username=username,
            account_id=account_id,
            subscriber_id=account_id,
            subscription_id=_latest_subscription_id(db, account_id),
            db=db,
        )
    finally:
        db.close()

    context = playwright_instance.request.new_context(
        base_url=settings.base_url,
        extra_http_headers={
            "Cookie": f"{customer_portal.SESSION_COOKIE_NAME}={customer_session}"
        },
    )
    yield context
    context.dispose()


@pytest.fixture()
def customer_token(api_context, admin_token: str, test_identities: dict):
    """Get a subscriber access token for the seeded customer credential."""
    customer = test_identities["customer"]
    return login_for_token(
        api_context,
        customer["person"]["email"],
        CUSTOMER_PORTAL_PASSWORD,
    )


# Anonymous/unauthenticated context for testing login flows


@pytest.fixture()
def anon_context(browser, settings: E2ESettings):
    """Browser context without any authentication - for login flow tests."""
    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    context.close()


@pytest.fixture()
def anon_page(anon_context):
    """Page without authentication - for testing login pages."""
    page = anon_context.new_page()
    yield page
    page.close()


# Vendor portal fixtures


@pytest.fixture()
def vendor_context(browser, settings: E2ESettings, api_context, admin_token: str):
    """Browser context with vendor portal session.

    Note: Vendor portal may require specific vendor credentials.
    This fixture skips if vendor auth is not available.
    """
    # Try to authenticate as a vendor through admin impersonation or vendor login
    # For now, skip as vendor auth setup is required
    pytest.skip("Vendor portal fixtures require vendor auth setup")


@pytest.fixture()
def vendor_page(vendor_context):
    """Page for vendor portal testing."""
    page = vendor_context.new_page()
    yield page
    page.close()


# Reseller portal fixtures


@pytest.fixture()
def reseller_context(browser, settings: E2ESettings, test_identities: dict):
    """Browser context with a direct reseller portal session cookie."""
    reseller = test_identities["reseller"]
    db = SessionLocal()
    try:
        reseller_session = reseller_portal._create_session(
            username=RESELLER_PORTAL_USERNAME,
            reseller_id=reseller["reseller_id"],
            subscriber_id=reseller["subscriber_id"],
            remember=False,
            db=db,
        )
    finally:
        db.close()

    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    parsed = urlparse(settings.base_url)
    context.add_cookies(
        [
            {
                "name": reseller_portal.SESSION_COOKIE_NAME,
                "value": reseller_session,
                "url": settings.base_url,
                "httpOnly": True,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        ]
    )
    yield context
    context.close()


@pytest.fixture()
def reseller_page(reseller_context):
    """Page for reseller portal testing."""
    page = reseller_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def reseller_credentials(test_identities: dict) -> tuple[str, str]:
    """Username/password for the seeded reseller portal login."""
    return RESELLER_PORTAL_USERNAME, RESELLER_PORTAL_PASSWORD


@pytest.fixture()
def reseller_account_id(test_identities: dict) -> str:
    """A customer account id linked to the seeded reseller."""
    return test_identities["customer"]["account"]["id"]
