"""Server administration: users and usage."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from via import timeutil
from via.context import Context
from via.db.models import OPEN_STATES, AuthSession, Delivery, Device, Push, User
from via.errors import APIError, not_found
from via.schemas import AdminUserOut, ServerStats
from via.services.common import commit, delete_blob_after_commit
from via.services.devices import expire_deliveries


async def list_users(db: AsyncSession) -> list[AdminUserOut]:
    active = Device.revoked_at.is_(None)
    device_stats = (
        select(
            Device.user_id,
            func.count().label("devices"),
            func.max(Device.last_seen_at).label("last_seen_at"),
        )
        .where(active)
        .group_by(Device.user_id)
        .subquery()
    )
    storage = (
        select(Push.user_id, func.sum(Push.file_size).label("stored"))
        .where(Push.file_stored.is_(True))
        .group_by(Push.user_id)
        .subquery()
    )
    rows = await db.execute(
        select(User, device_stats.c.devices, device_stats.c.last_seen_at, storage.c.stored)
        .outerjoin(device_stats, device_stats.c.user_id == User.id)
        .outerjoin(storage, storage.c.user_id == User.id)
        .order_by(User.username)
    )
    return [
        AdminUserOut(
            id=user.id,
            username=user.username,
            is_admin=user.is_admin,
            created_at=user.created_at,
            disabled_at=user.disabled_at,
            devices=devices or 0,
            stored_bytes=int(stored or 0),
            last_seen_at=last_seen,
        )
        for user, devices, last_seen, stored in rows
    ]


async def get_user(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("User")
    return user


def forbid_self(admin: User, user: User, action: str) -> None:
    if admin.id == user.id:
        raise APIError(409, "cannot_modify_self", f"You can't {action} your own account")


async def revoke_sessions(db: AsyncSession, user: User) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))


async def delete_user(ctx: Context, db: AsyncSession, user: User) -> None:
    """Delete an account with its devices, tokens and pushes (including stored files)."""
    now = timeutil.utcnow()
    # Items waiting for this user's devices (e.g. shared by contacts) are dropped properly,
    # so the senders' copies get purged instead of lingering until they expire.
    device_ids = list(await db.scalars(select(Device.id).where(Device.user_id == user.id)))
    await expire_deliveries(ctx, db, device_ids, now)
    for push_id in await db.scalars(
        select(Push.id).where(Push.user_id == user.id, Push.file_stored.is_(True))
    ):
        delete_blob_after_commit(db, push_id)
    for device_id in device_ids:
        ctx.hub.publish(device_id, {"type": "revoked"})
    await db.delete(user)
    await commit(ctx, db)


async def stats(db: AsyncSession) -> ServerStats:
    async def count(query: object) -> int:
        return int(await db.scalar(query) or 0)  # type: ignore[call-overload]

    return ServerStats(
        users=await count(select(func.count()).select_from(User)),
        devices=await count(
            select(func.count()).select_from(Device).where(Device.revoked_at.is_(None))
        ),
        pushes=await count(select(func.count()).select_from(Push).where(Push.meta.is_not(None))),
        pending_deliveries=await count(
            select(func.count()).select_from(Delivery).where(Delivery.state.in_(OPEN_STATES))
        ),
        stored_bytes=await count(
            select(func.coalesce(func.sum(Push.file_size), 0)).where(Push.file_stored.is_(True))
        ),
    )
