from typing import cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.auth import MFAMethodRead
from app.schemas.auth_flow import (
    AvatarUploadResponse,
    CredentialEnrollmentRequest,
    CredentialEnrollmentResponse,
    ErrorResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    MeUpdateRequest,
    MfaConfirmRequest,
    MfaSetupRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    PasswordChangeRequest,
    PasswordChangeResponse,
    RefreshRequest,
    ResendVerificationEmailResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SessionListResponse,
    SessionRevokeResponse,
    TokenResponse,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery, customer_credential_enrollment
from app.services import session_manager as session_manager_service
from app.services import user_profile as user_profile_service
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/auth", tags=["auth"])


def _credential_recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:public-auth-api",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _credential_recovery_http_error(exc: DomainError) -> HTTPException:
    status_code = {
        "auth.credential_recovery.invalid_password": status.HTTP_400_BAD_REQUEST,
        "auth.credential_recovery.credential_not_found": status.HTTP_404_NOT_FOUND,
        "auth.credential_recovery.invalid_reset_capability": (
            status.HTTP_401_UNAUTHORIZED
        ),
    }.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    return HTTPException(status_code=status_code, detail=exc.message)


def _credential_enrollment_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="public:customer-credential-enrollment",
        scope=customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE,
        reason=reason,
    )


def _credential_enrollment_http_error(exc: DomainError) -> HTTPException:
    status_code = {
        "auth.customer_credential_enrollment.invalid_password": (
            status.HTTP_400_BAD_REQUEST
        ),
        "auth.customer_credential_enrollment.invalid_capability": (
            status.HTTP_401_UNAUTHORIZED
        ),
        "auth.customer_credential_enrollment.context_not_found": (
            status.HTTP_401_UNAUTHORIZED
        ),
        "auth.customer_credential_enrollment.stale_context": (
            status.HTTP_401_UNAUTHORIZED
        ),
        "auth.customer_credential_enrollment.inactive_account": (
            status.HTTP_409_CONFLICT
        ),
        "auth.customer_credential_enrollment.username_unavailable": (
            status.HTTP_409_CONFLICT
        ),
        "auth.customer_credential_enrollment.invalid_username": (
            status.HTTP_422_UNPROCESSABLE_ENTITY
        ),
    }.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    return HTTPException(status_code=status_code, detail=exc.message)


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        428: {
            "model": ErrorResponse,
            "description": "Password reset required",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "code": "PASSWORD_RESET_REQUIRED",
                            "message": "Password reset required",
                        }
                    }
                }
            },
        }
    },
)
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> Response | LoginResponse:
    result = auth_flow_service.auth_flow.login_response(
        db,
        payload.username,
        payload.password,
        request,
        payload.provider.value if payload.provider else None,
    )
    if isinstance(result, Response):
        return result
    return LoginResponse.model_validate(result)


@router.post(
    "/mfa/setup",
    response_model=MfaSetupResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_setup(
    payload: MfaSetupRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> MfaSetupResponse:
    if str(payload.subscriber_id) != str(auth["subscriber_id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    result = auth_flow_service.auth_flow.mfa_setup(
        db, str(payload.subscriber_id), payload.label
    )
    return MfaSetupResponse.model_validate(result)


@router.post(
    "/mfa/confirm",
    response_model=MFAMethodRead,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def mfa_confirm(
    payload: MfaConfirmRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> MFAMethodRead:
    result = auth_flow_service.auth_flow.mfa_confirm(
        db, str(payload.method_id), payload.code, auth["subscriber_id"]
    )
    return MFAMethodRead.model_validate(result)


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def mfa_verify(
    payload: MfaVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.mfa_verify_response(
            db, payload.mfa_token, payload.code, request
        ),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def refresh(
    payload: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.refresh_response(
            db, payload.refresh_token, request
        ),
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"model": ErrorResponse},
    },
)
def logout(
    payload: LogoutRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return cast(
        Response,
        auth_flow_service.auth_flow.logout_response(db, payload.refresh_token, request),
    )


@router.get(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def get_me(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> MeResponse:
    return user_profile_service.get_me(
        db,
        principal_id=auth["principal_id"],
        principal_type=auth.get("principal_type", "subscriber"),
        roles=auth.get("roles", []),
        scopes=auth.get("scopes", []),
    )


@router.patch(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def update_me(
    payload: MeUpdateRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> MeResponse:
    return user_profile_service.update_me(
        db,
        principal_id=auth["principal_id"],
        principal_type=auth.get("principal_type", "subscriber"),
        payload=payload,
        roles=auth.get("roles", []),
        scopes=auth.get("scopes", []),
    )


@router.post(
    "/me/avatar",
    response_model=AvatarUploadResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
    },
)
async def upload_avatar(
    file: UploadFile,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> AvatarUploadResponse:
    return await user_profile_service.upload_avatar(
        db,
        subscriber_id=auth["subscriber_id"],
        file=file,
    )


@router.delete(
    "/me/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse},
    },
)
def delete_avatar(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> None:
    user_profile_service.delete_avatar(
        db,
        subscriber_id=auth["subscriber_id"],
    )


@router.get(
    "/me/sessions",
    response_model=SessionListResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def list_sessions(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> SessionListResponse:
    return session_manager_service.list_sessions(
        db,
        subscriber_id=auth["subscriber_id"],
        current_session_id=auth.get("session_id"),
        principal_type=auth.get("principal_type", "subscriber"),
    )


@router.delete(
    "/me/sessions/{session_id}",
    response_model=SessionRevokeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def revoke_session(
    session_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> SessionRevokeResponse:
    return session_manager_service.revoke_session(
        db,
        session_id=session_id,
        subscriber_id=auth["subscriber_id"],
        principal_type=auth.get("principal_type", "subscriber"),
    )


@router.delete(
    "/me/sessions",
    response_model=SessionRevokeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def revoke_all_other_sessions(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> SessionRevokeResponse:
    return session_manager_service.revoke_all_other_sessions(
        db,
        subscriber_id=auth["subscriber_id"],
        current_session_id=auth.get("session_id"),
        principal_type=auth.get("principal_type", "subscriber"),
    )


@router.post(
    "/me/password",
    response_model=PasswordChangeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def change_password(
    payload: PasswordChangeRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> PasswordChangeResponse:
    changed_at = auth_flow_service.change_password(
        db,
        subscriber_id=str(auth["subscriber_id"]),
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_session_id=auth.get("session_id"),
    )
    return PasswordChangeResponse(changed_at=changed_at)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> ForgotPasswordResponse:
    """
    Request a password reset email.
    Always returns success to prevent email enumeration.
    """
    credential_recovery.request_password_recovery(
        db,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=_credential_recovery_context(
                "Public API password recovery request"
            ),
            email=payload.email,
        ),
    )
    return ForgotPasswordResponse()


@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def reset_password_endpoint(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> ResetPasswordResponse:
    """
    Reset password using the token from forgot-password email.
    """
    try:
        outcome = credential_recovery.complete_password_reset(
            db,
            credential_recovery.CompletePasswordResetCommand(
                context=_credential_recovery_context(
                    "Public API password reset capability redemption"
                ),
                token=payload.token,
                new_password=payload.new_password,
            ),
        )
    except DomainError as exc:
        raise _credential_recovery_http_error(exc) from exc
    return ResetPasswordResponse(reset_at=outcome.reset_at)


@router.post(
    "/credential-enrollment",
    response_model=CredentialEnrollmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def credential_enrollment_endpoint(
    payload: CredentialEnrollmentRequest,
    db: Session = Depends(get_db),
) -> CredentialEnrollmentResponse:
    """Create a local credential from an emailed referral capability."""

    try:
        db_session_adapter.release_read_transaction(db)
        result = customer_credential_enrollment.complete_referral_enrollment(
            db,
            customer_credential_enrollment.CompleteReferralEnrollmentCommand(
                context=_credential_enrollment_context(
                    "Redeem referral customer credential enrollment capability"
                ),
                token=payload.token,
                new_password=payload.new_password,
                username=payload.username,
            ),
        )
    except DomainError as exc:
        raise _credential_enrollment_http_error(exc) from exc
    return CredentialEnrollmentResponse.model_validate(result, from_attributes=True)


@router.post(
    "/verify-email",
    response_model=VerifyEmailResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
    },
)
def verify_email_endpoint(
    payload: VerifyEmailRequest,
    db: Session = Depends(get_db),
) -> VerifyEmailResponse:
    """
    Verify the caller's email address using the token from the verification email.
    """
    auth_flow_service.verify_email(db, payload.token)
    return VerifyEmailResponse(email_verified=True)


@router.post(
    "/resend-verification-email",
    response_model=ResendVerificationEmailResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
def resend_verification_email_endpoint(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> ResendVerificationEmailResponse:
    """
    Resend the email-verification email to the authenticated caller's own address.
    """
    from app.services.rate_limiter_adapter import allow_operation

    principal_id = auth["principal_id"]
    decision = allow_operation(
        f"auth:resend-verification:{principal_id}",
        limit=3,
        window_seconds=900,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many verification email requests. Please try again later.",
        )
    sent = auth_flow_service.send_email_verification(db, principal_id)
    return ResendVerificationEmailResponse(sent=sent)
