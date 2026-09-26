"""Master key rotation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from via.crypto import DataKey, MasterKey
from via.db.models import Device, Push
from via.notify.dispatcher import push_target_aad


async def rewrap(db: AsyncSession, old: MasterKey, new: MasterKey) -> tuple[int, int]:
    """Re-encrypt everything the master key protects under ``new``. Doesn't commit.

    File contents and metadata are encrypted with per-push data keys, so only those keys (and
    devices' push targets) need rewrapping; blobs on disk are untouched.
    Returns ``(pushes, devices)`` rewrapped.
    """
    pushes = devices = 0
    for push in await db.scalars(select(Push).where(Push.wrapped_key.is_not(None))):
        assert push.wrapped_key is not None
        push.wrapped_key = DataKey.unwrap(old, push.wrapped_key, push.id).wrap(new)
        push.key_id = new.key_id
        pushes += 1
    for device in await db.scalars(select(Device).where(Device.push_target.is_not(None))):
        assert device.push_target is not None
        aad = push_target_aad(device.id)
        device.push_target = new.seal(old.open(device.push_target, aad), aad)
        devices += 1
    return pushes, devices
