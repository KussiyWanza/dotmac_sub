"""HTTP client for DotMac ERP (erp.dotmac.io).

Ported from ``dotmac_crm/app/services/dotmac_erp/client.py`` for the ERP re-home:
sub becomes an ``X-API-Key`` client of ERP's neutral ``/sync/sub/*`` API.

This module owns the database-free HTTP substrate: a generic ``post``/``get`` surface plus
the ``DotMacERPTransientError`` / permanent-error split the outbox keys its
retry-vs-dead-letter decision off. Flow-specific methods (expense claims, etc.)
belong to their owning flow modules. Config (base URL, token, timeout, retries) is resolved
from the version-pinned ``dotmac.erp`` connector runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

import httpx
from dotmac_integration import IntegrationHttpClient

from app.services.dotmac_erp.operational_contracts import (
    ErpOperationalSyncCommand,
    ErpOperationalSyncOutcome,
)

logger = logging.getLogger(__name__)


class DotMacERPError(Exception):
    """Base exception for DotMac ERP client errors (permanent unless subtyped)."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class DotMacERPAuthError(DotMacERPError):
    """Authentication error (401/403)."""


class DotMacERPNotFoundError(DotMacERPError):
    """Resource not found (404)."""


class DotMacERPRateLimitError(DotMacERPError):
    """Rate limit exceeded (429)."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class DotMacERPTransientError(DotMacERPError):
    """Retryable/transient ERP error (5xx, timeouts, network issues).

    The outbox keys its retry-vs-dead-letter decision off this type: a transient
    error leaves the row pending for the next worker pass; a plain
    ``DotMacERPError`` dead-letters it.
    """


class DotMacERPClient:
    """HTTP client for DotMac ERP REST API.

    Features:
    - API key authentication (``X-API-Key``)
    - Automatic retry with exponential backoff (shared transport engine)
    - Idempotency-key header support for safe retries
    - Rate-limit handling
    - Transient (5xx/timeout) vs permanent (4xx) error split
    """

    DEFAULT_TIMEOUT = 30
    DEFAULT_RETRIES = 3
    DEFAULT_RETRY_DELAY = 1.0

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """Initialize the DotMac ERP client.

        Args:
            base_url: Base URL for ERP API (e.g. ``https://erp.dotmac.io``).
            token: API key for authentication (``X-API-Key``).
            timeout: Request timeout in seconds.
            retries: Number of retry attempts.
            retry_delay: Initial delay between retries (exponential backoff).
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._client: httpx.Client | None = None
        self._transport: IntegrationHttpClient | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create the pooled HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "X-API-Key": self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "DotMac-Sub/1.0",
                },
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> DotMacERPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        expected_status_codes: Collection[int] | None = None,
    ) -> dict | list | None:
        """Map an API response to a parsed body or raise the edge's typed error."""
        try:
            data = response.json() if response.content else None
        except Exception:
            data = None

        # Auth failures keep their dedicated error type even when the caller
        # narrows expected_status_codes — callers classify on it.
        if response.status_code in (401, 403):
            raise DotMacERPAuthError(
                f"Authentication failed: {response.status_code}",
                status_code=response.status_code,
                response=data if isinstance(data, dict) else None,
            )

        if response.status_code == 204:
            return None

        if response.status_code == 404:
            raise DotMacERPNotFoundError(
                "Resource not found",
                status_code=404,
                response=data if isinstance(data, dict) else None,
            )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise DotMacERPRateLimitError(
                "Rate limit exceeded",
                retry_after=int(retry_after) if retry_after else None,
            )

        if response.status_code >= 400:
            if isinstance(data, dict):
                error_msg = (
                    data.get("detail")
                    or data.get("message")
                    or data.get("error")
                    or str(data)
                )
            else:
                error_msg = str(data)
            logger.warning(
                "ERP API error: status=%s body=%s", response.status_code, data
            )
            # 5xx are transient (proxy/gateway/unavailable/timeout or a passing
            # ERP blip) — raise the retryable type so the transport retries
            # instead of failing on a momentary hiccup. Idempotent natural-key
            # upserts on the ERP side make the retry safe.
            if response.status_code in (500, 502, 503, 504):
                raise DotMacERPTransientError(
                    f"ERP transient error ({response.status_code}): {error_msg}",
                    status_code=response.status_code,
                    response=data if isinstance(data, dict) else None,
                )
            raise DotMacERPError(
                f"API error ({response.status_code}): {error_msg}",
                status_code=response.status_code,
                response=data if isinstance(data, dict) else None,
            )

        if expected_status_codes and response.status_code not in expected_status_codes:
            raise DotMacERPError(
                f"API unexpected status ({response.status_code}), "
                f"expected {sorted(expected_status_codes)}",
                status_code=response.status_code,
                response=data if isinstance(data, dict) else None,
            )

        return data

    def _get_transport(self) -> IntegrationHttpClient:
        """The shared retry/transport engine, configured with this edge's policy.

        The retry semantics (Retry-After honouring, 5xx transient retry,
        connect/timeout retry, no retry on auth/4xx, and the give-up wrapping)
        live in ``IntegrationHttpClient``; this only declares which exception
        types mean what.
        """
        if self._transport is None:
            self._transport = IntegrationHttpClient(
                client_factory=self._get_client,
                response_handler=self._handle_response,
                backoff=lambda attempt: self.retry_delay * (2**attempt),
                max_attempts=self.retries + 1,
                rate_limit_exc=DotMacERPRateLimitError,
                retryable_excs=(DotMacERPTransientError,),
                non_retryable_excs=(DotMacERPError,),
                transport_exhausted_factory=lambda exc, retries: (
                    DotMacERPTransientError(
                        f"Connection error after {retries} retries: {exc}"
                    )
                ),
                loop_exhausted_factory=lambda exc, retries: DotMacERPError(
                    f"Request failed after {retries} retries: {exc}"
                ),
                unexpected_error_factory=lambda exc: DotMacERPError(
                    f"Unexpected error: {exc}"
                ),
            )
        return self._transport

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | list | None = None,
        idempotency_key: str | None = None,
        expected_status_codes: Collection[int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list | None:
        """Make an HTTP request with retry logic (delegated to the shared engine)."""
        return self._get_transport().request(
            method,
            path,
            params=params,
            json_data=json_data,
            headers=headers,
            idempotency_key=idempotency_key,
            handler_kwargs={"expected_status_codes": expected_status_codes},
        )

    # ============ Generic transport surface ============

    def post(
        self,
        path: str,
        payload: dict | list | None,
        idempotency_key: str | None = None,
        expected_status_codes: Collection[int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """POST ``payload`` to ``path`` with an optional idempotency key.

        Returns the parsed JSON body (``{}`` when the body is not a dict).
        Raises ``DotMacERPTransientError`` on retry-worthy failures and
        ``DotMacERPError`` (or a subtype) on permanent ones.
        """
        result = self._request(
            "POST",
            path,
            json_data=payload,
            idempotency_key=idempotency_key,
            expected_status_codes=expected_status_codes,
            headers=headers,
        )
        return result if isinstance(result, dict) else {}

    def get(
        self,
        path: str,
        params: dict | None = None,
        expected_status_codes: Collection[int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """GET ``path``. Returns the parsed JSON body (``{}`` when not a dict)."""
        result = self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            expected_status_codes=expected_status_codes,
        )
        return result if isinstance(result, dict) else {}

    # ============ Workforce attendance surface ============

    def get_attendance_today(self, subject: str, request_id: str) -> dict:
        return self.get(
            "/api/v1/sync/sub/attendance/today",
            expected_status_codes={200},
            headers={
                "X-Selfcare-Subject": subject,
                "X-Request-ID": request_id,
            },
        )

    def punch_attendance(
        self,
        action: str,
        subject: str,
        location: dict,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> dict:
        if action not in {"check-in", "check-out"}:
            raise DotMacERPError("Unsupported attendance action")
        return self.post(
            f"/api/v1/sync/sub/attendance/{action}",
            location,
            idempotency_key=idempotency_key,
            expected_status_codes={200},
            headers={
                "X-Selfcare-Subject": subject,
                "X-Request-ID": request_id,
            },
        )

    # ============ Expense claim surface ============
    #
    # Thin flow-specific wrappers over the generic post/get, mirroring the paths
    # and shapes of ``dotmac_crm/app/services/dotmac_erp/client.py`` verbatim so
    # ERP sees an identical client. The money write path is ``push_expense_claim``
    # (idempotent create-and-submit); the two GETs are read-only reconcile aids.

    def push_expense_claim(
        self, payload: dict, idempotency_key: str | None = None
    ) -> dict:
        """POST a field expense request to ERP as an expense claim.

        ``POST /sync/sub/expense-claims`` — idempotent create-and-submit: an
        identical resend of the same ``omni_id`` returns the existing claim
        (200); a first create returns 201. The ``idempotency_key`` is sent as the
        ``Idempotency-Key`` header so re-delivery of a row ERP already saw is a
        no-op on the ERP side. Returns the ERP body
        (``claim_id``/``claim_number``/``status``/``omni_id``).
        """
        return self.post(
            "/api/v1/sync/sub/expense-claims",
            payload,
            idempotency_key=idempotency_key,
            expected_status_codes={200, 201},
        )

    def get_expense_claim_status(self, omni_id: str) -> dict | None:
        """Poll ERP for an expense claim's approval/payment status.

        ``GET /sync/sub/expense-claims/{omni_id}`` where ``omni_id`` is sub's
        ``FieldExpenseRequest.id``. Returns the status body, or ``None`` when the
        claim is not (yet) known to ERP (404) — callers degrade rather than error.
        """
        try:
            return self.get(f"/api/v1/sync/sub/expense-claims/{omni_id}")
        except DotMacERPNotFoundError:
            return None

    def get_expense_categories(self) -> list[dict]:
        """List active ERP expense categories for field expense capture.

        ``GET /sync/sub/expense-categories`` unwraps the ``items`` envelope to a
        plain list (empty when absent).
        """
        result = self.get("/api/v1/sync/sub/expense-categories")
        items = result.get("items")
        return items if isinstance(items, list) else []

    # ============ Material request surface ============
    #
    # Thin flow-specific wrappers over the generic post/get, mirroring the paths
    # and shapes of ``dotmac_crm/app/services/dotmac_erp/client.py`` verbatim so
    # ERP sees an identical client. The money write path is
    # ``push_material_request`` (idempotent create-and-issue); the two GETs are
    # read-only reconcile / serial-pick aids.

    def push_material_request(
        self, payload: dict, idempotency_key: str | None = None
    ) -> dict:
        """POST an approved field material request to ERP as an ISSUE.

        ``POST /sync/sub/material-requests`` — idempotent create-and-issue: an
        identical resend of the same ``omni_id`` returns the existing request
        (200); a first create returns 201. The ``idempotency_key`` is sent as the
        ``Idempotency-Key`` header so re-delivery of a row ERP already saw is a
        no-op on the ERP side. Returns the ERP body
        (``request_id``/``request_number``/``status``/``omni_id``).
        """
        return self.post(
            "/api/v1/sync/sub/material-requests",
            payload,
            idempotency_key=idempotency_key,
            expected_status_codes={200, 201},
        )

    def get_material_request_status(self, omni_id: str) -> dict | None:
        """Poll ERP for a material request's fulfillment/stock status.

        ``GET /sync/sub/material-requests/{omni_id}`` where ``omni_id`` is sub's
        ``FieldMaterialRequest.id``. Returns the status body, or ``None`` when the
        request is not (yet) known to ERP (404) — callers degrade rather than
        error.
        """
        try:
            return self.get(f"/api/v1/sync/sub/material-requests/{omni_id}")
        except DotMacERPNotFoundError:
            return None

    def list_available_serials(
        self,
        *,
        item_code: str,
        warehouse_code: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List available ERP serials for one item in one warehouse.

        ``GET /sync/sub/inventory/serials/available`` is the serial-pick aid
        for building a serialized ISSUE payload. Ports CRM's
        ``list_available_serials``. Returns the ERP body (``{}`` when absent).
        """
        return self.get(
            "/api/v1/sync/sub/inventory/serials/available",
            params={
                "item_code": item_code,
                "warehouse_code": warehouse_code,
                "limit": limit,
                "offset": offset,
            },
        )

    # ============ Purchase invoice surface ============

    def create_purchase_invoice(
        self, payload: dict, idempotency_key: str | None = None
    ) -> dict:
        return self.post(
            "/api/v1/sync/sub/purchase-invoices",
            payload,
            idempotency_key=idempotency_key,
            expected_status_codes={200, 201},
        )

    def get_purchase_invoice_status(self, source_invoice_id: str) -> dict | None:
        """Read ERP's current AP settlement state for a Sub invoice."""
        try:
            return self.get(f"/api/v1/sync/sub/purchase-invoices/{source_invoice_id}")
        except DotMacERPNotFoundError:
            return None

    def upload_purchase_invoice_attachment(
        self,
        purchase_invoice_id: str,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> dict:
        return self.post(
            f"/api/v1/sync/sub/purchase-invoices/{purchase_invoice_id}/attachments",
            payload,
            idempotency_key=idempotency_key,
            expected_status_codes={200, 201},
        )

    # ============ Inventory read surface (ERP remains stock SoR) ============

    def list_inventory(
        self,
        *,
        search: str | None = None,
        category_code: str | None = None,
        warehouse_id: str | None = None,
        include_zero_stock: bool = False,
        only_below_reorder: bool = False,
        only_with_available_serials: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        optional = {
            "search": search,
            "category_code": category_code,
            "warehouse_id": warehouse_id,
        }
        params.update({key: value for key, value in optional.items() if value})
        if include_zero_stock:
            params["include_zero_stock"] = True
        if only_below_reorder:
            params["only_below_reorder"] = True
        if only_with_available_serials:
            params["only_with_available_serials"] = True
        result = self._request("GET", "/api/v1/sync/sub/inventory", params=params)
        if isinstance(result, dict):
            return result
        return {"items": result if isinstance(result, list) else []}

    def get_inventory_item(self, item_id: str) -> dict | None:
        try:
            result = self._request("GET", f"/api/v1/sync/sub/inventory/{item_id}")
        except DotMacERPNotFoundError:
            return None
        return result if isinstance(result, dict) else None

    def list_inventory_warehouses(self) -> list[dict]:
        result = self._request("GET", "/api/v1/sync/sub/inventory/meta/warehouses")
        if isinstance(result, dict):
            rows = result.get("warehouses") or result.get("items") or []
            return rows if isinstance(rows, list) else []
        return result if isinstance(result, list) else []

    def list_inventory_categories(self) -> list[dict]:
        result = self._request("GET", "/api/v1/sync/sub/inventory/meta/categories")
        if isinstance(result, dict):
            rows = result.get("categories") or result.get("items") or []
            return rows if isinstance(rows, list) else []
        return result if isinstance(result, list) else []

    def sync_operational_domains(
        self, command: ErpOperationalSyncCommand
    ) -> ErpOperationalSyncOutcome:
        """Push native Sub project/ticket/task/work-order context to ERP."""
        response = self.post(
            "/api/v1/sync/sub/bulk",
            command.model_dump(mode="json"),
            expected_status_codes={200},
        )
        return ErpOperationalSyncOutcome.model_validate(response)

    # ============ NCC regulatory read surface (ERP owns finance + HR) ============
    #
    # The NCC annual year-end return's Section F (financials) and Section G
    # (staff head-count) are ERP data. Sub reads them over the API for the
    # regulatory pack and keeps no copy — ERP stays the owner.
    #
    # The paths keep ERP's existing ``/sync/crm/ncc/*`` names rather than sub's
    # usual ``/sync/sub/*``: those are the endpoints ERP exposes today (a legacy
    # of the CRM-era caller). The data is not CRM's; renaming belongs on ERP's
    # side of the contract, not guessed at from here.

    def get_ncc_financials(
        self,
        *,
        year: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of_date: str | None = None,
    ) -> dict:
        """Fetch the NCC year-end return's Section F financials from ERP.

        Composed by ERP from its income-statement / balance-sheet /
        expense-summary services. Pass ``year`` for the annual return, or an
        explicit ``start_date``/``end_date``/``as_of_date`` range.

        Returns:
            dict with keys: period, summary, detail, note
        """
        params: dict[str, object] = {}
        if year is not None:
            params["year"] = year
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if as_of_date:
            params["as_of_date"] = as_of_date
        return self.get("/api/v1/sync/crm/ncc/financials", params=params or None)

    def get_ncc_staff_headcount(self) -> dict:
        """Fetch the NCC year-end return's Section G staff head-count from ERP.

        Returns:
            dict with keys: total_active, by_category
            (category -> nationality -> gender -> count)
        """
        return self.get("/api/v1/sync/crm/ncc/staff-headcount")
