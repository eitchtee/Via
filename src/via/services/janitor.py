"""Periodic cleanup: expiry, history and tombstone removal, orphaned blobs."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from via import timeutil
from via.context import Context
from via.db.models import EXPIRED, OPEN_STATES, AuthSession, Delivery, Invite, Push
from via.services import pushes
from via.services.common import commit

log = logging.getLogger(__name__)

# Blobs younger than this are never treated as orphans: an upload may be about to commit.
_ORPHAN_GRACE = 600


async def run_once(ctx: Context, now: datetime | None = None) -> None:
    now = now or timeutil.utcnow()
    async with ctx.sessionmaker() as db:
        # 1. Expire deliveries of pushes past their TTL, then purge those pushes.
        expired = list(
            await db.scalars(
                select(Push.id).where(Push.purged_at.is_(None), Push.expires_at <= now)
            )
        )
        if expired:
            await db.execute(
                update(Delivery)
                .where(Delivery.push_id.in_(expired), Delivery.state.in_(OPEN_STATES))
                .values(state=EXPIRED)
            )
            await pushes.maybe_purge(ctx, db, expired, now)

        # 2. Drop history entries past their retention.
        for push in await db.scalars(select(Push).where(Push.history_until <= now)):
            pushes.drop_content(db, push)

        # 3. Delete tombstones (content-free pushes kept for delivery status).
        cutoff = now - timedelta(days=ctx.settings.tombstone_days)
        await db.execute(
            delete(Push).where(
                Push.purged_at <= cutoff, Push.history_until.is_(None), Push.meta.is_(None)
            )
        )

        # 4. Expired sessions and invites.
        await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        await db.execute(delete(Invite).where(Invite.expires_at <= now, Invite.used_at.is_(None)))
        await commit(ctx, db)

        # 5. Blobs on disk that no push references (crashed uploads, failed deletes).
        stored = set(await db.scalars(select(Push.id).where(Push.file_stored.is_(True))))
    await asyncio.to_thread(_delete_orphans, ctx, stored)
    if expired:
        log.info("expired %d push(es)", len(expired))


def _delete_orphans(ctx: Context, stored: set[str]) -> None:
    threshold = time.time() - _ORPHAN_GRACE
    for push_id, path, partial, mtime in ctx.blobs.scan():
        if mtime < threshold and (partial or push_id not in stored):
            log.info("deleting orphaned blob %s", path.name)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete %s", path, exc_info=True)


async def run_forever(ctx: Context) -> None:
    interval = ctx.settings.janitor_interval
    while True:
        try:
            await run_once(ctx)
        except Exception:
            log.exception("janitor run failed")
        await asyncio.sleep(interval)
