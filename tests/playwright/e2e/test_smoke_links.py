from __future__ import annotations

import os
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import Error as PlaywrightError

EXCLUDED_PATH_SNIPPETS = {
    "/logout",
    "/stop-impersonation",
    "/auth/refresh",
}

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:")


def _normalize_url(base_url: str, current_url: str, href: str | None) -> str | None:
    if not href:
        return None

    href = href.strip()
    if not href or href.startswith("#"):
        return None

    if href.startswith(SKIP_SCHEMES):
        return None

    if href.startswith(("http://", "https://")):
        absolute = href
    else:
        absolute = urljoin(current_url, href)

    if not absolute.startswith(base_url):
        return None

    absolute = absolute.split("#", 1)[0]
    parsed = urlparse(absolute)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    normalized = urlunparse(parsed._replace(path=path, fragment=""))
    if any(snippet in parsed.path for snippet in EXCLUDED_PATH_SNIPPETS):
        return None
    if parsed.path.startswith("/api"):
        return None

    return normalized


def _extract_links(page, base_url: str) -> set[str]:
    hrefs: list[str | None] = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(el => el.getAttribute('href'))",
    )
    links: set[str] = set()
    for href in hrefs:
        normalized = _normalize_url(base_url, page.url, href)
        if normalized:
            links.add(normalized)
    return links


def _crawl_links(page, base_url: str, start_paths: list[str], max_pages: int) -> None:
    queue = deque([f"{base_url}{path}" for path in start_paths])
    visited: set[str] = set()

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue

        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            if "net::ERR_ABORTED" in str(exc):
                # Navigations that start a download abort the page load;
                # verify the link over plain HTTP instead.
                fetched = page.request.get(url)
                if fetched.status >= 400:
                    raise AssertionError(
                        f"{url} returned HTTP {fetched.status}"
                    ) from exc
                visited.add(url)
                continue
            raise
        visited.add(url)

        if response is None:
            raise AssertionError(f"No response for {url}")

        status = response.status
        if status >= 400:
            raise AssertionError(f"{url} returned HTTP {status}")

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            continue

        for link in _extract_links(page, base_url):
            if link not in visited:
                queue.append(link)


def _max_pages_from_env(default: int = 200) -> int:
    value = os.getenv("PLAYWRIGHT_LINK_CHECK_MAX")
    if not value:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def test_admin_internal_links_smoke(admin_page, settings):
    max_pages = _max_pages_from_env()
    _crawl_links(admin_page, settings.base_url, ["/admin/dashboard"], max_pages)


def test_customer_portal_internal_links_smoke(user_page, settings):
    max_pages = _max_pages_from_env()
    _crawl_links(
        user_page,
        settings.base_url,
        ["/portal/dashboard", "/portal/billing", "/portal/support"],
        max_pages,
    )


def test_public_internal_links_smoke(admin_page, settings):
    max_pages = _max_pages_from_env()
    _crawl_links(
        admin_page,
        settings.base_url,
        [
            "/",
            "/auth/login",
            "/auth/forgot-password",
            # Only routes the app actually serves. /web/catalog, /web/projects
            # and /web/tickets were listed here but have never existed in app
            # code, so this spec asserted a 404 on every run. E2E is nightly and
            # not a PR gate, so that failure sat unnoticed.
            "/web/network",
            "/web/usage",
            "/web/billing",
        ],
        max_pages,
    )
