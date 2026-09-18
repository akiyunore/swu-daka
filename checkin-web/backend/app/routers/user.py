import logging
from sqlite3 import Connection

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.dependencies import get_db
from app.dependencies_user import UserContext, require_user, require_user_csrf
from app.schemas.user import ScheduleUpdate, UserLogin, UserRegister
from app.schemas.notification import NotificationEmailUpdate, NotificationSettingsUpdate, NotificationVerify
from core.config import get_settings
from core.rate_limit import enforce_rate_limit
from core.security import hash_token
from services.checkin_service import set_auto_checkin, verify_school_login
from services.public_service import read_invite
from services.notification_service import (
    EmailResendCooldown,
    get_notification_settings,
    queue_registration_verification,
    set_failure_notification,
    update_email_and_queue,
    verify_email_code,
)
from services.user_service import (
    create_user_session,
    get_user_profile,
    login_user,
    register_user,
    validate_registration_target,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.user_session_cookie_name,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


@router.post("/auth/register")
def register(
    payload: UserRegister,
    request: Request,
    response: Response,
    db: Connection = Depends(get_db),
) -> dict:
    enforce_rate_limit(request, "user-register", 5, 15 * 60)
    if not payload.has_agreed_terms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须先阅读并同意用户须知")
    try:
        read_invite(db, payload.invitation_token)
        validate_registration_target(db, payload.school_username)
        verification = verify_school_login(payload.school_username, payload.school_password)
        if verification["status"] != "success":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=verification["detail"])
        user_id, created = register_user(
            db,
            payload.invitation_token,
            payload.school_username,
            payload.school_password,
            payload.display_name,
            payload.email,
        )
        token, session = create_user_session(db, user_id)
        try:
            verification_queued = queue_registration_verification(db, user_id)
        except Exception:
            verification_queued = False
            logger.error("Failed to enqueue registration email verification for user_id=%s", user_id)
        _set_session_cookie(response, token)
        return {
            "status": "registered" if created else "activated",
            "email_verification_queued": verification_queued,
            **session,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/auth/login")
def login(payload: UserLogin, request: Request, response: Response, db: Connection = Depends(get_db)) -> dict:
    enforce_rate_limit(request, "user-login", 8, 15 * 60)
    try:
        token, session = login_user(db, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    _set_session_cookie(response, token)
    return session


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    user: UserContext = Depends(require_user_csrf),
    db: Connection = Depends(get_db),
) -> None:
    token = request.cookies.get(get_settings().user_session_cookie_name)
    if token:
        db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (hash_token(token),))
        db.commit()
    response.delete_cookie(get_settings().user_session_cookie_name, path="/")


@router.get("/auth/me")
def me(user: UserContext = Depends(require_user), db: Connection = Depends(get_db)) -> dict:
    return {
        "csrf_token": user.csrf_token,
        "expires_at": user.expires_at,
        "user": get_user_profile(db, user.id),
    }


@router.post("/schedule")
def update_schedule(
    payload: ScheduleUpdate,
    user: UserContext = Depends(require_user_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    try:
        return set_auto_checkin(db, user.id, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/notifications")
def notifications(user: UserContext = Depends(require_user), db: Connection = Depends(get_db)) -> dict:
    return get_notification_settings(db, user.id)


@router.post("/notifications/email")
def update_notification_email(
    payload: NotificationEmailUpdate,
    request: Request,
    user: UserContext = Depends(require_user_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    enforce_rate_limit(request, "notification-email", 5, 15 * 60)
    try:
        return update_email_and_queue(db, user.id, payload.email)
    except EmailResendCooldown as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/notifications/verify")
def verify_notification_email(
    payload: NotificationVerify,
    request: Request,
    user: UserContext = Depends(require_user_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    enforce_rate_limit(request, "notification-verify", 10, 15 * 60)
    try:
        return verify_email_code(db, user.id, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/notifications/settings")
def update_notification_settings(
    payload: NotificationSettingsUpdate,
    user: UserContext = Depends(require_user_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    try:
        return set_failure_notification(db, user.id, payload.notify_on_failure)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
