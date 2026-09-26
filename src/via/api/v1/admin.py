from fastapi import APIRouter, Response
from sqlalchemy import select

from via import timeutil
from via.config import parse_duration
from via.db.models import Invite
from via.deps import DB, AdminAuth, Ctx
from via.errors import APIError, not_found
from via.schemas import (
    AdminPasswordIn,
    AdminUserIn,
    AdminUserOut,
    AdminUserUpdateIn,
    InviteCreated,
    InviteIn,
    InviteOut,
    ServerStats,
)
from via.services import accounts, admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/invites", status_code=201)
async def create_invite(body: InviteIn, principal: AdminAuth, ctx: Ctx, db: DB) -> InviteCreated:
    """Create a single-use invite code for `POST /v1/auth/register`."""
    try:
        ttl = parse_duration(body.expires_in) if body.expires_in else ctx.settings.invite_ttl
    except ValueError as e:
        raise APIError(422, "validation_error", str(e)) from e
    code, invite = await accounts.create_invite(db, principal.user, ttl)
    await db.commit()
    return InviteCreated(
        id=invite.id, created_at=invite.created_at, expires_at=invite.expires_at, code=code
    )


@router.get("/invites")
async def list_invites(principal: AdminAuth, db: DB) -> list[InviteOut]:
    """Unused, unexpired invites."""
    result = await db.scalars(
        select(Invite)
        .where(Invite.used_at.is_(None), Invite.expires_at > timeutil.utcnow())
        .order_by(Invite.id)
    )
    return [InviteOut.model_validate(i) for i in result]


@router.delete("/invites/{invite_id}", status_code=204)
async def delete_invite(invite_id: str, principal: AdminAuth, db: DB) -> Response:
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise not_found("Invite")
    await db.delete(invite)
    await db.commit()
    return Response(status_code=204)


@router.get("/stats")
async def server_stats(principal: AdminAuth, db: DB) -> ServerStats:
    return await admin.stats(db)


@router.get("/users")
async def list_users(principal: AdminAuth, db: DB) -> list[AdminUserOut]:
    """All accounts, with device count, stored bytes and when a device was last seen."""
    return await admin.list_users(db)


async def _user_out(db: DB, user_id: str) -> AdminUserOut:
    return next(u for u in await admin.list_users(db) if u.id == user_id)


@router.post("/users", status_code=201)
async def create_user(body: AdminUserIn, principal: AdminAuth, db: DB) -> AdminUserOut:
    user = await accounts.create_user(db, body.username, body.password, is_admin=body.is_admin)
    await db.commit()
    return await _user_out(db, user.id)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, body: AdminUserUpdateIn, principal: AdminAuth, db: DB
) -> AdminUserOut:
    """Grant or remove admin rights; disable or re-enable an account. A disabled account's
    tokens stop working and contacts can't share with it; its data is kept."""
    user = await admin.get_user(db, user_id)
    if body.is_admin is not None and body.is_admin != user.is_admin:
        admin.forbid_self(principal.user, user, "change admin rights of")
        user.is_admin = body.is_admin
    if body.disabled is not None and body.disabled != (user.disabled_at is not None):
        admin.forbid_self(principal.user, user, "disable")
        user.disabled_at = timeutil.utcnow() if body.disabled else None
    await db.commit()
    return await _user_out(db, user.id)


@router.post("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: str, body: AdminPasswordIn, principal: AdminAuth, db: DB
) -> Response:
    """Set a new password. All of the user's sessions are signed out."""
    user = await admin.get_user(db, user_id)
    accounts.check_password_strength(body.password)
    user.password_hash = await accounts.hash_password(body.password)
    await admin.revoke_sessions(db, user)
    await db.commit()
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, principal: AdminAuth, ctx: Ctx, db: DB) -> Response:
    """Delete an account and everything it owns: devices, tokens, contacts, pushes and files."""
    user = await admin.get_user(db, user_id)
    admin.forbid_self(principal.user, user, "delete")
    await admin.delete_user(ctx, db, user)
    return Response(status_code=204)
