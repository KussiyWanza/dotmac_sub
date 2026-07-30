"""Admin billing bank-transfer payment-proof routes."""

from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.payment_proof_errors import payment_proof_http_status
from app.db import get_db
from app.services import web_billing_payment_proofs as web_payment_proofs_service
from app.services.action_forms import ActionFormSubmission
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _command_context(
    auth: dict,
    *,
    scope: str,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/payment-proofs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:proof:read"))],
)
def payment_proofs_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    state = web_payment_proofs_service.list_data(
        db,
        status=status,
        page=page,
        per_page=per_page,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_proofs.html",
        {
            "request": request,
            **state,
            "active_page": "payment_proofs",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payment-proofs/{proof_id}",
    response_class=HTMLResponse,
)
def payment_proofs_detail(
    request: Request,
    proof_id: UUID,
    error: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:proof:read")),
):
    return _detail_response(
        request,
        proof_id,
        db=db,
        auth=auth,
        error=error,
        message=message,
    )


def _detail_response(
    request: Request,
    proof_id: UUID,
    *,
    db: Session,
    auth: dict,
    error: str | None = None,
    message: str | None = None,
    submission: ActionFormSubmission | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    can_review = has_permission(auth, db, "billing:proof:verify")
    can_correct = can_review and has_permission(auth, db, "billing:payment:update")
    state = web_payment_proofs_service.detail_data(
        db,
        proof_id=str(proof_id),
        can_review=can_review,
        can_correct=can_correct,
        submission=submission,
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment proof not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_proof_detail.html",
        {
            "request": request,
            **state,
            "error": error,
            "message": message,
            "active_page": "payment_proofs",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


@router.get(
    "/payment-proofs/{proof_id}/file",
    dependencies=[Depends(require_permission("billing:proof:read"))],
)
def payment_proofs_file(
    proof_id: UUID,
    db: Session = Depends(get_db),
):
    try:
        resolved = web_payment_proofs_service.file_response_args(
            db, proof_id=str(proof_id)
        )
    except DomainError as exc:
        raise HTTPException(
            status_code=payment_proof_http_status(exc),
            detail=exc.message,
        ) from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="Payment proof not found")
    path, media_type = resolved
    return FileResponse(path, media_type=media_type)


@router.post(
    "/payment-proofs/{proof_id}/verify",
    response_class=HTMLResponse,
)
def payment_proofs_verify(
    request: Request,
    proof_id: UUID,
    amount: str = Form(""),
    auto_allocate: str = Form("yes"),
    review_notes: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:proof:verify")),
):
    try:
        from app.services import payment_proofs

        db_session_adapter.release_read_transaction(db)
        web_payment_proofs_service.verify_proof(
            db,
            context=_command_context(
                auth,
                scope=payment_proofs.REVIEW_SCOPE,
                reason="Staff verified bank-transfer evidence",
                idempotency_key=f"payment-proof:verify:{proof_id}",
            ),
            proof_id=str(proof_id),
            verified_by=str(auth.get("principal_id")),
            amount=amount,
            auto_allocate=auto_allocate == "yes",
            review_notes=review_notes,
        )
    except DomainError as exc:
        submission = web_payment_proofs_service.review_error_submission(
            action_key=web_payment_proofs_service.VERIFY_ACTION_KEY,
            values={
                "amount": amount,
                "auto_allocate": auto_allocate,
                "review_notes": review_notes,
            },
            error=exc,
        )
        return _detail_response(
            request,
            proof_id,
            db=db,
            auth=auth,
            submission=submission,
            status_code=payment_proof_http_status(exc),
        )
    return _redirect(proof_id, message="Proof verified and payment recorded")


@router.post(
    "/payment-proofs/{proof_id}/reject",
    response_class=HTMLResponse,
)
def payment_proofs_reject(
    request: Request,
    proof_id: UUID,
    review_notes: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:proof:verify")),
):
    try:
        from app.services import payment_proofs

        db_session_adapter.release_read_transaction(db)
        web_payment_proofs_service.reject_proof(
            db,
            context=_command_context(
                auth,
                scope=payment_proofs.REVIEW_SCOPE,
                reason="Staff rejected bank-transfer evidence",
                idempotency_key=f"payment-proof:reject:{proof_id}",
            ),
            proof_id=str(proof_id),
            verified_by=str(auth.get("principal_id")),
            review_notes=review_notes,
        )
    except DomainError as exc:
        submission = web_payment_proofs_service.review_error_submission(
            action_key=web_payment_proofs_service.REJECT_ACTION_KEY,
            values={"review_notes": review_notes},
            error=exc,
        )
        return _detail_response(
            request,
            proof_id,
            db=db,
            auth=auth,
            submission=submission,
            status_code=payment_proof_http_status(exc),
        )
    return _redirect(proof_id, message="Proof rejected")


def _require_correction_permissions(auth: dict, db: Session) -> None:
    if not has_permission(auth, db, "billing:payment:update"):
        raise HTTPException(
            status_code=403,
            detail="Payment update permission is required for proof correction.",
        )


@router.post(
    "/payment-proofs/{proof_id}/duplicate-correction/preview",
    response_class=HTMLResponse,
)
def payment_proof_duplicate_correction_preview(
    request: Request,
    proof_id: UUID,
    original_proof_id: UUID = Form(...),
    reason: str = Form(..., min_length=10, max_length=500),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:proof:verify")),
):
    _require_correction_permissions(auth, db)
    try:
        preview = web_payment_proofs_service.preview_duplicate_correction(
            db,
            proof_id=proof_id,
            original_proof_id=original_proof_id,
            reason=reason,
        )
    except DomainError as exc:
        return _detail_response(
            request,
            proof_id,
            db=db,
            auth=auth,
            error=exc.message,
            status_code=payment_proof_http_status(exc),
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_proof_duplicate_correction_confirm.html",
        {
            "request": request,
            "preview": preview,
            "idempotency_key": (
                f"proof-correction:{proof_id}:{preview.fingerprint[:16]}"
            ),
            "active_page": "payment_proofs",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-proofs/{proof_id}/duplicate-correction",
    response_class=HTMLResponse,
)
def payment_proof_duplicate_correction(
    request: Request,
    proof_id: UUID,
    original_proof_id: UUID = Form(...),
    reason: str = Form(..., min_length=10, max_length=500),
    preview_fingerprint: str = Form(..., min_length=64, max_length=64),
    idempotency_key: str = Form(..., min_length=16, max_length=120),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:proof:verify")),
):
    _require_correction_permissions(auth, db)
    try:
        from app.services import payment_proofs

        db_session_adapter.release_read_transaction(db)
        result = web_payment_proofs_service.correct_duplicate_payment_proof(
            db,
            context=_command_context(
                auth,
                scope=payment_proofs.REVIEW_SCOPE,
                reason="Staff corrected a duplicate payment-proof verification",
                idempotency_key=idempotency_key,
            ),
            proof_id=proof_id,
            original_proof_id=original_proof_id,
            actor_id=str(auth.get("principal_id")),
            reason=reason,
            preview_fingerprint=preview_fingerprint,
        )
    except DomainError as exc:
        return _detail_response(
            request,
            proof_id,
            db=db,
            auth=auth,
            error=exc.message,
            status_code=payment_proof_http_status(exc),
        )
    return _redirect(
        proof_id,
        message=(
            "Duplicate verification corrected; payment reversal "
            f"{result.payment_reversal_id} recorded"
        ),
    )


def _redirect(
    proof_id: UUID, *, error: str | None = None, message: str | None = None
) -> RedirectResponse:
    url = f"/admin/billing/payment-proofs/{proof_id}"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)
