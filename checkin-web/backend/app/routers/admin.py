import asyncio
from sqlite3 import Connection

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.dependencies import get_db
from app.dependencies_admin import AdminContext, require_admin, require_admin_csrf, require_admin_stream
from app.schemas.admin import (
    AdminLogin,
    AdminSessionRead,
    CloudOcrUpdate,
    InviteCreate,
    InviteCreated,
    ScheduleUpdate,
    UserDeleteConfirm,
    UserNoteUpdate,
)
from core.config import get_settings
from core.rate_limit import enforce_rate_limit
from core.security import hash_token
from services.admin_event_service import admin_event_broker
from services.admin_service import (
    create_invite,
    dashboard,
    get_invite,
    list_invites,
    list_runs,
    list_system_logs,
    login_admin,
    reveal_invite_token,
    revoke_invite,
)
from services.auth_service import (
    delete_user,
    get_user,
    list_users,
    reset_user_login,
    set_user_active,
    update_user_note,
)
from services.checkin_service import run_checkin_for_user, set_auto_checkin
from services.cloud_ocr_service import get_cloud_ocr_settings, list_cloud_ocr_calls, update_cloud_ocr_settings

router = APIRouter()


@router.post("/auth/login", response_model=AdminSessionRead)
def login(payload: AdminLogin, request: Request, response: Response, db: Connection = Depends(get_db)) -> AdminSessionRead:
    enforce_rate_limit(request, "admin-login", 8, 15 * 60)
    try:
        token, session = login_admin(db, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return AdminSessionRead(**session)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> None:
    token = request.cookies.get(get_settings().session_cookie_name)
    if token:
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (hash_token(token),))
        db.commit()
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/auth/me", response_model=AdminSessionRead)
def me(admin: AdminContext = Depends(require_admin)) -> AdminSessionRead:
    return AdminSessionRead(username=admin.username, csrf_token=admin.csrf_token, expires_at=admin.expires_at)


@router.get("/dashboard")
def get_dashboard(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> dict:
    return dashboard(db)


@router.get("/events")
async def admin_events(
    request: Request,
    admin: AdminContext = Depends(require_admin_stream),
) -> StreamingResponse:
    raw_token = request.cookies.get(get_settings().session_cookie_name, "")
    try:
        subscription = await admin_event_broker.subscribe(raw_token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "30"},
        ) from exc

    async def stream():
        try:
            yield "retry: 3000\n\n"
            last_auth_check = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                now = asyncio.get_running_loop().time()
                if now - last_auth_check >= 60:
                    if not admin_event_broker.session_is_valid(subscription.token_hash):
                        return
                    last_auth_check = now
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event == "update":
                    yield "event: update\ndata: {}\n\n"
        finally:
            await admin_event_broker.unsubscribe(subscription.subscription_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/users")
def get_users(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> list[dict]:
    return list_users(db)


@router.get("/users/{user_id}")
def get_user_detail(user_id: int, admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> dict:
    try:
        return get_user(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/users/{user_id}/active")
def update_user_active(user_id: int, payload: ScheduleUpdate, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> dict:
    try:
        return set_user_active(db, user_id, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/users/{user_id}/schedule")
def update_schedule(user_id: int, payload: ScheduleUpdate, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> dict:
    try:
        return set_auto_checkin(db, user_id, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/users/{user_id}/reset-login")
def reset_login(user_id: int, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> dict:
    try:
        return reset_user_login(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/users/{user_id}/note")
def update_note(
    user_id: int,
    payload: UserNoteUpdate,
    admin: AdminContext = Depends(require_admin_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    try:
        return update_user_note(db, user_id, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/users/{user_id}/delete")
def remove_user(
    user_id: int,
    payload: UserDeleteConfirm,
    admin: AdminContext = Depends(require_admin_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="必须明确确认删除用户")
    try:
        return delete_user(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/cloud-ocr")
def get_cloud_ocr(
    admin: AdminContext = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> dict:
    return get_cloud_ocr_settings(db)


@router.post("/cloud-ocr")
def save_cloud_ocr(
    payload: CloudOcrUpdate,
    admin: AdminContext = Depends(require_admin_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    try:
        return update_cloud_ocr_settings(
            db,
            admin.id,
            enabled=payload.enabled,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            clear_api_key=payload.clear_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{user_id}/run")
def run_user_now(user_id: int, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> dict:
    try:
        return run_checkin_for_user(db, user_id, "admin")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs")
def get_runs(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> list[dict]:
    return list_runs(db)


@router.get("/cloud-ocr/calls")
def get_cloud_ocr_calls(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> list[dict]:
    return list_cloud_ocr_calls(db)


@router.get("/logs")
def get_logs(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> list[dict]:
    return list_system_logs(db)


@router.get("/invites")
def get_invites(admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> list[dict]:
    return list_invites(db)


@router.get("/invites/{invite_id}")
def get_invite_detail(invite_id: int, admin: AdminContext = Depends(require_admin), db: Connection = Depends(get_db)) -> dict:
    try:
        return get_invite(db, invite_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/invites/{invite_id}/reveal")
def post_reveal_invite(
    invite_id: int,
    request: Request,
    response: Response,
    admin: AdminContext = Depends(require_admin_csrf),
    db: Connection = Depends(get_db),
) -> dict:
    enforce_rate_limit(request, "invite-reveal", 30, 15 * 60)
    try:
        result = reveal_invite_token(db, invite_id)
        response.headers["Cache-Control"] = "no-store"
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
def post_invite(payload: InviteCreate, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> InviteCreated:
    return InviteCreated(**create_invite(db, admin.id, payload.label, payload.expires_in_hours))


@router.post("/invites/{invite_id}/revoke")
def post_revoke_invite(invite_id: int, admin: AdminContext = Depends(require_admin_csrf), db: Connection = Depends(get_db)) -> dict:
    try:
        return revoke_invite(db, invite_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
