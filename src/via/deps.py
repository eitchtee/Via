"""FastAPI dependencies: server context, database session and authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from via import security, timeutil
from via.context import Context
from via.db.models import AppToken, AuthSession, Device, User
from via.errors import APIError

PrincipalKind = Literal["session", "device", "app"]
# Don't write "last used" timestamps on every request.
_TOUCH_INTERVAL = timedelta(seconds=60)


def get_ctx(request: Request) -> Context:
    ctx: Context = request.app.state.ctx
    return ctx


Ctx = Annotated[Context, Depends(get_ctx)]


async def get_db(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    async with ctx.sessionmaker() as db:
        yield db


DB = Annotated[AsyncSession, Depends(get_db)]


@dataclass
class Principal:
    """Who is making the request."""

    kind: PrincipalKind
    user: User
    session: AuthSession | None = None
    device: Device | None = None
    app_token: AppToken | None = None

    @property
    def this_device(self) -> Device:
        assert self.device is not None
        return self.device

    @property
    def this_session(self) -> AuthSession:
        assert self.session is not None
        return self.session


_bearer = HTTPBearer(
    auto_error=False,
    description="A `via_s_…` session token, `via_d_…` device token or `via_a_…` app token.",
)


def _stale(value: datetime | None, now: datetime) -> bool:
    return value is None or now - value > _TOUCH_INTERVAL


async def get_principal(
    db: DB, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    unauthorized = {"WWW-Authenticate": "Bearer"}
    if creds is None:
        raise APIError(401, "unauthorized", "Missing bearer token", headers=unauthorized)
    token = creds.credentials
    digest = security.hash_token(token)
    now = timeutil.utcnow()

    principal: Principal | None = None
    match security.token_kind(token):
        case security.SESSION:
            session = await db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
            if session is not None and session.expires_at > now:
                if _stale(session.last_used_at, now):
                    session.last_used_at = now
                principal = await _principal(db, "session", session.user_id, session=session)
        case security.DEVICE:
            device = await db.scalar(
                select(Device).where(Device.token_hash == digest, Device.revoked_at.is_(None))
            )
            if device is not None:
                if _stale(device.last_seen_at, now):
                    device.last_seen_at = now
                principal = await _principal(db, "device", device.user_id, device=device)
        case security.APP:
            app_token = await db.scalar(select(AppToken).where(AppToken.token_hash == digest))
            if app_token is not None:
                if _stale(app_token.last_used_at, now):
                    app_token.last_used_at = now
                principal = await _principal(db, "app", app_token.user_id, app_token=app_token)

    if principal is None:
        await db.rollback()
        raise APIError(401, "invalid_token", "Invalid or expired token", headers=unauthorized)
    # Commit to persist "last used" and, just as importantly, end the read transaction so
    # long-polls and SSE streams don't hold a connection (and a stale snapshot) open.
    await db.commit()
    return principal


async def _principal(
    db: AsyncSession,
    kind: PrincipalKind,
    user_id: str,
    *,
    session: AuthSession | None = None,
    device: Device | None = None,
    app_token: AppToken | None = None,
) -> Principal | None:
    user = await db.get(User, user_id)
    if user is None or user.disabled_at is not None:
        return None
    return Principal(kind, user, session=session, device=device, app_token=app_token)


def require(*kinds: PrincipalKind, admin: bool = False) -> Callable[..., Awaitable[Principal]]:
    names = " or ".join(kinds)

    async def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.kind not in kinds:
            raise APIError(403, "forbidden", f"This endpoint requires a {names} token")
        if admin and not principal.user.is_admin:
            raise APIError(403, "forbidden", "Admin only")
        return principal

    return dependency


AnyAuth = Annotated[Principal, Depends(get_principal)]
SessionAuth = Annotated[Principal, Depends(require("session"))]
DeviceAuth = Annotated[Principal, Depends(require("device"))]
AdminAuth = Annotated[Principal, Depends(require("session", admin=True))]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
