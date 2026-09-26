from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from via import security, timeutil
from via.context import Context
from via.db.models import (
    EXPIRED,
    OPEN_STATES,
    PROVIDER_NONE,
    SHARE_BY_DEFAULT,
    Delivery,
    Device,
    User,
)
from via.errors import not_found
from via.notify.dispatcher import push_target_aad
from via.services import pushes
from via.services.common import commit


async def register_device(
    db: AsyncSession, user: User, name: str, type_: str, accepts_shares: bool | None = None
) -> tuple[str, Device]:
    token = security.new_token(security.DEVICE)
    device = Device(
        user_id=user.id,
        name=name,
        type=type_,
        token_hash=security.hash_token(token),
        accepts_shares=type_ in SHARE_BY_DEFAULT if accepts_shares is None else accepts_shares,
    )
    db.add(device)
    await db.flush()
    return token, device


async def get_device(db: AsyncSession, user: User, device_id: str) -> Device:
    device = await db.scalar(
        select(Device).where(
            Device.id == device_id, Device.user_id == user.id, Device.revoked_at.is_(None)
        )
    )
    if device is None:
        raise not_found("Device")
    return device


async def list_devices(db: AsyncSession, user: User) -> list[Device]:
    result = await db.scalars(
        select(Device)
        .where(Device.user_id == user.id, Device.revoked_at.is_(None))
        .order_by(Device.id)
    )
    return list(result)


async def revoke_device(ctx: Context, db: AsyncSession, device: Device) -> None:
    """Revoke a device: its token stops working and its pending deliveries expire."""
    now = timeutil.utcnow()
    device.revoked_at = now
    device.token_hash = None
    device.push_provider = PROVIDER_NONE
    device.push_target = None
    await expire_deliveries(ctx, db, [device.id], now)
    await commit(ctx, db)
    ctx.hub.publish(device.id, {"type": "revoked"})


async def expire_deliveries(
    ctx: Context, db: AsyncSession, device_ids: list[str], now: datetime
) -> None:
    """Expire open deliveries to these devices and purge pushes left with none open."""
    if not device_ids:
        return
    open_ids = list(
        await db.scalars(
            select(Delivery.push_id).where(
                Delivery.device_id.in_(device_ids), Delivery.state.in_(OPEN_STATES)
            )
        )
    )
    await db.execute(
        update(Delivery)
        .where(Delivery.device_id.in_(device_ids), Delivery.state.in_(OPEN_STATES))
        .values(state=EXPIRED)
    )
    await pushes.maybe_purge(ctx, db, open_ids, now)


def set_push_target(ctx: Context, device: Device, provider: str, target: str | None) -> None:
    device.push_provider = provider
    device.push_target = (
        ctx.master.seal(target.encode(), push_target_aad(device.id)) if target else None
    )


def rotate_token(device: Device) -> str:
    token = security.new_token(security.DEVICE)
    device.token_hash = security.hash_token(token)
    return token
