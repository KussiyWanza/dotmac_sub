"""Admin payment-proof back office: page registration + receipt file auth."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.db import get_db
from app.models.system_user import SystemUser
from app.services import payment_proofs as svc
from app.services import web_billing_payment_proofs as web_payment_proofs
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context(action: str) -> CommandContext:
    return CommandContext.system(
        actor="test:payment-proof-admin",
        scope=(svc.SUBMISSION_SCOPE if action == "submit" else svc.REVIEW_SCOPE),
        reason=f"Payment-proof admin {action} behavior test",
    )


def _submit(db_session, *args, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.submit_proof(
        db_session,
        *args,
        context=_context("submit"),
        **kwargs,
    ).to_dict()


def _verify(db_session, proof_id, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.verify_proof(
        db_session,
        proof_id,
        context=_context("verify"),
        **kwargs,
    ).to_dict()


def _reject(db_session, proof_id, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.reject_proof(
        db_session,
        proof_id,
        context=_context("reject"),
        **kwargs,
    ).to_dict()


def _routes(router) -> set[tuple[str, str]]:
    return {
        (getattr(route, "path", ""), method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }


def _clean_session_mock() -> MagicMock:
    db = MagicMock()
    db.in_transaction.return_value = False
    return db


class TestAdminWebRouteRegistration:
    def test_list_detail_file_and_actions_registered(self) -> None:
        from app.web.admin.billing_payment_proofs import router

        routes = _routes(router)
        assert ("/billing/payment-proofs", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}/file", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}/verify", "POST") in routes
        assert ("/billing/payment-proofs/{proof_id}/reject", "POST") in routes
        assert (
            "/billing/payment-proofs/{proof_id}/duplicate-correction/preview",
            "POST",
        ) in routes
        assert (
            "/billing/payment-proofs/{proof_id}/duplicate-correction",
            "POST",
        ) in routes

    def test_router_is_mounted_on_admin(self) -> None:
        from app.web.admin import router as admin_router

        paths = {getattr(route, "path", "") for route in admin_router.routes}
        assert "/admin/billing/payment-proofs" in paths
        assert "/admin/billing/payment-proofs/{proof_id}" in paths

    def test_every_route_declares_a_billing_permission_guard(self) -> None:
        from app.web.admin.billing_payment_proofs import router

        for route in router.routes:
            names = set()
            stack = [route.dependant]
            while stack:
                dep = stack.pop()
                call = getattr(dep, "call", None)
                if call is not None:
                    names.add(getattr(call, "__name__", ""))
                stack.extend(getattr(dep, "dependencies", []) or [])
            assert "_require_permission" in names, (
                f"{route.path} has no permission guard"
            )


class TestAdminWebVerifyRejectHandlers:
    def test_verify_redirects_to_detail_on_success(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_verify

        proof_id = uuid.uuid4()
        with patch(
            "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
        ) as service:
            response = payment_proofs_verify(
                request=MagicMock(),
                proof_id=proof_id,
                amount="4500.00",
                auto_allocate="no",
                review_notes="checked",
                db=_clean_session_mock(),
                auth={"principal_id": "admin-1"},
            )
        kwargs = service.verify_proof.call_args.kwargs
        assert kwargs["amount"] == "4500.00"
        assert kwargs["auto_allocate"] is False
        assert kwargs["verified_by"] == "admin-1"
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            f"/admin/billing/payment-proofs/{proof_id}"
        )
        assert "message=" in response.headers["location"]

    def test_verify_rerenders_with_structured_error_when_service_rejects(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_verify

        proof_id = uuid.uuid4()
        rendered = MagicMock(status_code=409)
        with (
            patch(
                "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
            ) as service,
            patch(
                "app.web.admin.billing_payment_proofs._detail_response",
                return_value=rendered,
            ) as detail_response,
        ):
            service.verify_proof.side_effect = svc.PaymentProofReviewError(
                code="financial.payment_proofs.duplicate_transfer_reference",
                message="Reference already verified",
            )
            response = payment_proofs_verify(
                request=MagicMock(),
                proof_id=proof_id,
                amount="",
                auto_allocate="yes",
                review_notes="",
                db=_clean_session_mock(),
                auth={"principal_id": "admin-1"},
            )
        assert response.status_code == 409
        service.review_error_submission.assert_called_once()
        assert detail_response.call_args.kwargs["status_code"] == 409
        assert detail_response.call_args.kwargs["submission"] is not None

    def test_reject_passes_reason_through(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_reject

        proof_id = uuid.uuid4()
        with patch(
            "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
        ) as service:
            response = payment_proofs_reject(
                request=MagicMock(),
                proof_id=proof_id,
                review_notes="No matching transfer",
                db=_clean_session_mock(),
                auth={"principal_id": "admin-2"},
            )
        kwargs = service.reject_proof.call_args.kwargs
        assert kwargs["review_notes"] == "No matching transfer"
        assert kwargs["verified_by"] == "admin-2"
        assert response.status_code == 303


class TestDuplicateCorrectionHandlers:
    def test_preview_requires_payment_update_permission(self) -> None:
        from app.web.admin.billing_payment_proofs import (
            payment_proof_duplicate_correction_preview,
        )

        with (
            patch(
                "app.web.admin.billing_payment_proofs.has_permission",
                return_value=False,
            ),
            pytest.raises(HTTPException) as exc,
        ):
            payment_proof_duplicate_correction_preview(
                request=MagicMock(),
                proof_id=uuid.uuid4(),
                original_proof_id=uuid.uuid4(),
                reason="Duplicate receipt confirmed against bank evidence.",
                db=_clean_session_mock(),
                auth={"principal_id": str(uuid.uuid4())},
            )

        assert exc.value.status_code == 403

    def test_confirmation_delegates_to_owner_and_redirects(self) -> None:
        from app.web.admin.billing_payment_proofs import (
            payment_proof_duplicate_correction,
        )

        proof_id = uuid.uuid4()
        original_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        result = MagicMock(payment_reversal_id=uuid.uuid4())
        db = _clean_session_mock()
        with (
            patch(
                "app.web.admin.billing_payment_proofs.has_permission",
                return_value=True,
            ),
            patch(
                "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
            ) as service,
            patch(
                "app.web.admin.billing_payment_proofs.db_session_adapter"
            ) as session_adapter,
        ):
            service.correct_duplicate_payment_proof.return_value = result
            response = payment_proof_duplicate_correction(
                request=MagicMock(),
                proof_id=proof_id,
                original_proof_id=original_id,
                reason="Duplicate receipt confirmed against bank evidence.",
                preview_fingerprint="a" * 64,
                idempotency_key="proof-correction-handler-test",
                db=db,
                auth={"principal_id": str(actor_id)},
            )

        session_adapter.release_read_transaction.assert_called_once_with(db)
        service.correct_duplicate_payment_proof.assert_called_once()
        call = service.correct_duplicate_payment_proof.call_args.kwargs
        assert call["proof_id"] == proof_id
        assert call["original_proof_id"] == original_id
        assert call["actor_id"] == str(actor_id)
        assert response.status_code == 303


# ---------------------------------------------------------------------------
# Receipt file endpoint auth (API): admin any, customer own only.
# ---------------------------------------------------------------------------


def _subscriber(db_session, email: str):
    from app.models.subscriber import Subscriber

    sub = Subscriber(first_name="Proof", last_name="Owner", email=email)
    db_session.add(sub)
    db_session.commit()
    return sub


def _system_user(
    db_session,
    *,
    display_name: str | None = "Ada Reviewer",
    first_name: str = "Ada",
    last_name: str = "Reviewer",
    email: str | None = None,
    is_active: bool = True,
) -> SystemUser:
    user = SystemUser(
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        email=email or f"reviewer-{uuid.uuid4().hex[:8]}@example.com",
        is_active=is_active,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def proof_env(db_session, tmp_path, monkeypatch):
    """Two subscribers; the first owns a proof whose receipt really exists."""
    monkeypatch.setattr(svc, "_UPLOAD_DIR", tmp_path)
    owner = _subscriber(db_session, "proof.owner@example.com")
    other = _subscriber(db_session, "proof.other@example.com")
    receipt = tmp_path / "receipt.png"
    receipt.write_bytes(b"\x89PNG-not-really-but-fine")
    proof = _submit(
        db_session,
        str(owner.id),
        submitted_by=str(owner.id),
        amount="5000",
        reference="TRF-FILE",
        file_path=str(receipt),
    )
    return {"owner": owner, "other": other, "proof": proof, "receipt": receipt}


def _client(db_session, principal: dict) -> TestClient:
    from app.api.payment_proofs import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user_auth] = lambda: principal
    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        for dependency in getattr(dependant, "dependencies", []) or []:
            call = getattr(dependency, "call", None)
            if getattr(call, "__name__", "") != "_require_permission":
                continue

            def _permission_guard(principal=principal):
                if "admin" not in set(principal.get("roles") or []):
                    raise HTTPException(status_code=403, detail="Forbidden")
                return principal

            app.dependency_overrides[call] = _permission_guard
    return TestClient(app)


def _admin_principal() -> dict:
    admin_id = str(uuid.uuid4())
    return {
        "roles": ["admin"],
        "scopes": [],
        "principal_type": "system_user",
        "principal_id": admin_id,
        "subscriber_id": admin_id,
    }


def _customer_principal(sub) -> dict:
    return {
        "roles": [],
        "scopes": [],
        "principal_type": "subscriber",
        "principal_id": str(sub.id),
        "subscriber_id": str(sub.id),
    }


class TestProofFileEndpointAuth:
    def test_admin_can_fetch_any_proof_file(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import payment_proof_file

        resp = payment_proof_file(
            proof_id=proof_env["proof"]["id"],
            db=db_session,
        )
        assert resp.status_code == 200
        assert resp.media_type == "image/png"
        assert resp.body == proof_env["receipt"].read_bytes()

    def test_owner_can_fetch_their_own_proof_file(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import my_payment_proof_file

        resp = my_payment_proof_file(
            proof_id=proof_env["proof"]["id"],
            db=db_session,
            principal=_customer_principal(proof_env["owner"]),
        )
        assert resp.status_code == 200
        assert resp.body == proof_env["receipt"].read_bytes()

    def test_other_customer_cannot_fetch_someone_elses_file(
        self, db_session, proof_env
    ) -> None:
        from app.api.payment_proofs import my_payment_proof_file

        with pytest.raises(HTTPException) as exc:
            my_payment_proof_file(
                proof_id=proof_env["proof"]["id"],
                db=db_session,
                principal=_customer_principal(proof_env["other"]),
            )
        assert exc.value.status_code == 404

    def test_customer_cannot_use_admin_file_route(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import router

        route = next(
            route
            for route in router.routes
            if getattr(route, "path", "") == "/payment-proofs/admin/{proof_id}/file"
        )
        dependency_names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        assert "_require_permission" in dependency_names

    def test_unknown_proof_is_404(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=str(uuid.uuid4()), db=db_session)
        assert exc.value.status_code == 404

    def test_missing_file_on_disk_is_404(self, db_session, proof_env, tmp_path):
        proof_env["receipt"].unlink()
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=proof_env["proof"]["id"], db=db_session)
        assert exc.value.status_code == 404

    def test_path_traversal_outside_upload_dir_is_404(
        self, db_session, proof_env
    ) -> None:
        """A file_path pointing outside uploads/payment_proofs must never be
        served, even to an admin."""
        evil = _submit(
            db_session,
            str(proof_env["owner"].id),
            submitted_by=str(proof_env["owner"].id),
            amount="5000",
            reference="TRF-EVIL",
            file_path="/etc/passwd",
        )
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=evil["id"], db=db_session)
        assert exc.value.status_code == 404


class TestAdminPagesRender:
    def _request(self, path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": b"",
                "headers": [],
            }
        )

    def _text(self, response) -> str:
        return response.body.decode()

    def test_detail_projection_preserves_uuid_but_resolves_display_name(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(db_session, display_name="Ada Reviewer")
        _verify(db_session, proof_env["proof"]["id"], verified_by=str(reviewer.id))

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["proof"].verified_by == str(reviewer.id)
        reviewer_projection = state["reviewer"]
        assert reviewer_projection.actor_id == str(reviewer.id)
        assert reviewer_projection.display_name == "Ada Reviewer"
        assert reviewer_projection.display_name != state["proof"].verified_by
        assert reviewer_projection.identity_resolved is True

    def test_detail_projection_falls_back_to_first_and_last_name(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(
            db_session,
            display_name="   ",
            first_name="Grace",
            last_name="Hopper",
        )
        _verify(db_session, proof_env["proof"]["id"], verified_by=str(reviewer.id))

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["reviewer"].display_name == "Grace Hopper"
        assert state["reviewer"].identity_resolved is True

    def test_detail_projection_falls_back_to_email_when_names_are_blank(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(
            db_session,
            display_name=" ",
            first_name=" ",
            last_name=" ",
            email="email-fallback@example.com",
        )
        _verify(db_session, proof_env["proof"]["id"], verified_by=str(reviewer.id))

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["reviewer"].display_name == "email-fallback@example.com"
        assert state["reviewer"].identity_resolved is True

    def test_detail_projection_resolves_inactive_staff_for_history(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(
            db_session,
            display_name="Former Reviewer",
            is_active=False,
        )
        _verify(db_session, proof_env["proof"]["id"], verified_by=str(reviewer.id))

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["reviewer"].display_name == "Former Reviewer"
        assert state["reviewer"].identity_resolved is True

    def test_detail_projection_uses_raw_non_uuid_actor_safely(
        self, db_session, proof_env
    ) -> None:
        _verify(db_session, proof_env["proof"]["id"], verified_by="legacy-reviewer")

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["reviewer"].actor_id == "legacy-reviewer"
        assert state["reviewer"].display_name == "legacy-reviewer"
        assert state["reviewer"].identity_resolved is False

    def test_detail_projection_uses_raw_uuid_when_system_user_is_missing(
        self, db_session, proof_env
    ) -> None:
        missing_user_id = str(uuid.uuid4())
        _verify(db_session, proof_env["proof"]["id"], verified_by=missing_user_id)

        state = web_payment_proofs.detail_data(
            db_session,
            proof_id=proof_env["proof"]["id"],
            can_review=False,
        )

        assert state is not None
        assert state["reviewer"].actor_id == missing_user_id
        assert state["reviewer"].display_name == missing_user_id
        assert state["reviewer"].identity_resolved is False

    def test_verified_outcome_card_displays_reviewer_name_and_actor_id(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(db_session, display_name="Ada Reviewer")
        _verify(db_session, proof_env["proof"]["id"], verified_by=str(reviewer.id))

        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )

        text = self._text(detail)
        assert detail.status_code == 200
        assert "Review Outcome" in text
        assert "Ada Reviewer" in text
        assert str(reviewer.id) in text
        assert "Reviewer Actor ID" in text

    def test_rejected_outcome_card_displays_reviewer_name_and_actor_id(
        self, db_session, proof_env
    ) -> None:
        reviewer = _system_user(
            db_session,
            display_name=" ",
            first_name="Linus",
            last_name="Torvalds",
        )
        _reject(
            db_session,
            proof_env["proof"]["id"],
            verified_by=str(reviewer.id),
            review_notes="Amount does not match statement",
        )

        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )

        text = self._text(detail)
        assert detail.status_code == 200
        assert "Review Outcome" in text
        assert "Linus Torvalds" in text
        assert str(reviewer.id) in text

    def test_list_and_detail_pages_render(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import (
            payment_proofs_detail,
            payment_proofs_list,
        )

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            resp = payment_proofs_list(
                request=self._request("/admin/billing/payment-proofs"),
                page=1,
                per_page=25,
                db=db_session,
            )
            text = self._text(resp)
            assert resp.status_code == 200
            assert "Bank Transfer Proofs" in text
            assert "TRF-FILE" in text
            assert "sticky left-0 z-30 min-w-56" in text
            assert "sticky left-0 z-20 min-w-56" in text
            assert (
                f'href="/admin/billing/payment-proofs/{proof_env["proof"]["id"]}"'
                in text
            )

            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )
            detail_text = self._text(detail)
            assert detail.status_code == 200
            # Claimed amount prefills the verify form; both review forms render.
            assert 'name="amount"' in detail_text
            assert 'value="5000.00"' in detail_text
            assert "/verify" in detail_text
            assert "/reject" in detail_text

    def test_detail_page_shows_duplicate_warning_and_error(
        self, db_session, proof_env
    ) -> None:
        _submit(
            db_session,
            str(proof_env["owner"].id),
            submitted_by=str(proof_env["owner"].id),
            amount="5000",
            reference="TRF-FILE",
            file_path=str(proof_env["receipt"]),
        )
        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                error="Reference already verified",
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )
            detail_text = self._text(detail)
            assert detail.status_code == 200
            assert "Possible duplicate submission" in detail_text
            assert "Reference already verified" in detail_text

    def test_detail_disables_verify_after_duplicate_reference_was_paid(
        self, db_session, proof_env
    ) -> None:
        duplicate = _submit(
            db_session,
            str(proof_env["owner"].id),
            submitted_by=str(proof_env["owner"].id),
            amount="5000",
            reference="TRF-FILE",
            file_path=str(proof_env["receipt"]),
        )
        _verify(db_session, duplicate["id"], verified_by="admin")
        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )

        text = self._text(detail)
        assert detail.status_code == 200
        assert 'data-action-form="payment_proof.verify"' in text
        assert f"verified proof {duplicate['id']}" in text
        assert 'data-action-form="payment_proof.reject"' in text
        assert 'disabled aria-disabled="true"' in text

    def test_unknown_proof_renders_404_page(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            proof_id = uuid.uuid4()
            resp = payment_proofs_detail(
                request=self._request(f"/admin/billing/payment-proofs/{proof_id}"),
                proof_id=proof_id,
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )
            assert resp.status_code == 404

    def test_detail_hides_review_actions_without_verify_permission(
        self, db_session, proof_env
    ) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_detail

        with (
            patch("app.web.admin.get_current_user", return_value={"id": "reader"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
            patch(
                "app.web.admin.billing_payment_proofs.has_permission",
                return_value=False,
            ),
        ):
            detail = payment_proofs_detail(
                request=self._request(
                    f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                ),
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                db=db_session,
                auth={"principal_id": "reader", "roles": []},
            )

        text = self._text(detail)
        assert detail.status_code == 200
        assert 'data-action-form="payment_proof.verify"' not in text
        assert 'data-action-form="payment_proof.reject"' not in text

    def test_invalid_verify_rerenders_value_and_accessible_field_error(
        self, db_session, proof_env
    ) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_verify

        request = self._request(
            f"/admin/billing/payment-proofs/{proof_env['proof']['id']}/verify"
        )
        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            response = payment_proofs_verify(
                request=request,
                proof_id=uuid.UUID(proof_env["proof"]["id"]),
                amount="not-a-number",
                auto_allocate="no",
                review_notes="checked statement",
                db=db_session,
                auth={"principal_id": "admin", "roles": ["admin"]},
            )

        text = self._text(response)
        assert response.status_code == 400
        assert 'value="not-a-number"' in text
        assert '<option value="no" selected>' in text
        assert "checked statement" in text
        assert "Invalid verified amount" in text
        assert 'aria-invalid="true"' in text
        assert 'role="alert"' in text


class TestWebAdminFileRoute:
    def test_streams_receipt_for_staff(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_file

        response = payment_proofs_file(
            proof_id=uuid.UUID(proof_env["proof"]["id"]), db=db_session
        )
        assert response.media_type == "image/png"
        assert str(response.path) == str(proof_env["receipt"].resolve())

    def test_unknown_proof_raises_404(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_file

        with pytest.raises(HTTPException) as exc:
            payment_proofs_file(proof_id=uuid.uuid4(), db=db_session)
        assert exc.value.status_code == 404
