from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from via.context import Context

_BLOB_DELETES = "via_blob_deletes"


def delete_blob_after_commit(db: AsyncSession, push_id: str) -> None:
    db.info.setdefault(_BLOB_DELETES, set()).add(push_id)


async def commit(ctx: Context, db: AsyncSession) -> None:
    """Commit, then delete the blobs of purged pushes (never before the commit succeeds)."""
    await db.commit()
    for push_id in db.info.pop(_BLOB_DELETES, ()):
        ctx.blobs.delete(push_id)
