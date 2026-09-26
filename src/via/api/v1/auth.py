from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, select

from via import timeutil
from via.db.models import AuthSession, User
from via.deps import DB, Ctx, SessionAuth, client_ip
from via.errors import APIError
from via.schemas import LoginIn, RegisterIn, SessionOut, UserOut
from via.services import accounts

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginIn, request: Request, ctx: Ctx, db: DB) -> SessionOut:
    """Exchange username and password for a session token (account management)."""
    ctx.login_limiter.check(f"ip:{client_ip(request)}")
    user = await db.scalar(
        select(User).where(User.username == accounts.normalize_username(body.username))
    )
    ok = await accounts.verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or user.disabled_at is not None:
        raise APIError(401, "invalid_credentials", "Invalid username or password")
    token, session = await accounts.create_session(
        db, user, ctx.settings.session_ttl, request.headers.get("user-agent")
    )
    await db.commit()
    return SessionOut(token=token, expires_at=session.expires_at, user=UserOut.model_validate(user))


@router.post("/logout", status_code=204)
async def logout(principal: SessionAuth, db: DB) -> Response:
    """Revoke the current session token."""
    await db.execute(delete(AuthSession).where(AuthSession.id == principal.this_session.id))
    await db.commit()
    return Response(status_code=204)


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request, ctx: Ctx, db: DB) -> SessionOut:
    """Create an account. Needs `VIA_SIGNUP=open`, or `invite` plus a valid invite code."""
    ctx.login_limiter.check(f"ip:{client_ip(request)}")
    mode = ctx.settings.signup
    if mode == "closed":
        raise APIError(403, "signup_closed", "Signup is disabled on this server")
    invite = None
    if mode == "invite":
        if not body.invite:
            raise APIError(403, "invite_required", "An invite code is required")
        invite = await accounts.find_valid_invite(db, body.invite)
    user = await accounts.create_user(db, body.username, body.password)
    if invite is not None:
        invite.used_by = user.id
        invite.used_at = timeutil.utcnow()
    token, session = await accounts.create_session(
        db, user, ctx.settings.session_ttl, request.headers.get("user-agent")
    )
    await db.commit()
    return SessionOut(token=token, expires_at=session.expires_at, user=UserOut.model_validate(user))
