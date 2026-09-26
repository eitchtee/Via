"""Pushes and their deliveries: the core store-and-forward logic.

Lifecycle: a push is created with one delivery per target device (``pending``). Listing a
device's inbox marks its delivery ``delivered``; the device then acks it (``acked``). Once
every delivery is acked or expired, the push is *purged*: its content and file are deleted
and only a content-free tombstone stays (for delivery status) until the janitor removes it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from via import timeutil
from via.config import parse_duration
from via.context import Context
from via.crypto import DataKey, EncryptingWriter
from via.db.models import (
    ACKED,
    DELIVERED,
    EXPIRED,
    OPEN_STATES,
    PENDING,
    Delivery,
    Device,
    Push,
    User,
)
from via.deps import Principal
from via.errors import APIError, not_found
from via.ids import new_id
from via.schemas import (
    DeliveryOut,
    FileInfo,
    HistoryItem,
    InboxItem,
    PushKind,
    PushStatus,
    Targets,
)
from via.services import contacts
from via.services.common import commit, delete_blob_after_commit

TextKind = Literal["link", "note"]

# --- sending ------------------------------------------------------------------------------


def parse_targets(value: str) -> Targets:
    """Parse the ``to`` query parameter: ``all``, or comma-separated device ids and
    ``@username`` contacts."""
    if value.strip().lower() == "all":
        return "all"
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise APIError(
            422, "validation_error", "`to` must be `all` or a list of device ids and @contacts"
        )
    return ids


async def resolve_targets(db: AsyncSession, principal: Principal, to: Targets) -> list[Device]:
    """Resolve targets to devices.

    ``all`` means every device of the sender's account except the sending device. A list may
    mix the sender's own device ids and ``@username`` entries, which expand to the contact's
    devices that accept shares.
    """
    own = list(
        await db.scalars(
            select(Device)
            .where(Device.user_id == principal.user.id, Device.revoked_at.is_(None))
            .order_by(Device.id)
        )
    )
    if to == "all":
        sender = principal.device.id if principal.device else None
        targets = [d for d in own if d.id != sender]
    else:
        by_id = {d.id: d for d in own}
        found: dict[str, Device] = {}
        unknown = []
        for entry in dict.fromkeys(to):
            if entry.startswith("@"):
                for device in await contacts.sharing_devices(db, principal.user, entry[1:]):
                    found[device.id] = device
            elif entry in by_id:
                found[entry] = by_id[entry]
            else:
                unknown.append(entry)
        if unknown:
            raise APIError(422, "unknown_device", f"Unknown device(s): {', '.join(unknown)}")
        targets = list(found.values())
    if not targets:
        raise APIError(422, "no_targets", "There are no devices to send this to")
    return targets


def resolve_ttl(ctx: Context, ttl: int | str | None) -> int:
    if ttl is None:
        return ctx.settings.default_ttl
    try:
        seconds = parse_duration(ttl)
    except ValueError as e:
        raise APIError(422, "invalid_ttl", str(e)) from e
    if seconds <= 0:
        raise APIError(422, "invalid_ttl", "ttl must be positive")
    return min(seconds, ctx.settings.max_ttl)


def check_text_length(ctx: Context, *values: str | None) -> None:
    limit = ctx.settings.max_text_length
    if any(v is not None and len(v) > limit for v in values):
        raise APIError(413, "text_too_long", f"Text is limited to {limit} characters")


def check_push_rate(ctx: Context, principal: Principal) -> None:
    ctx.push_limiter.check(f"user:{principal.user.id}")


async def stored_bytes(db: AsyncSession, user_id: str) -> int:
    total = await db.scalar(
        select(func.coalesce(func.sum(Push.file_size), 0)).where(
            Push.user_id == user_id, Push.file_stored.is_(True)
        )
    )
    return int(total or 0)


@dataclass(frozen=True)
class NewPush:
    """Identity and data key of a push being created; they always go together."""

    id: str
    key: DataKey

    @classmethod
    def create(cls) -> NewPush:
        push_id = new_id()
        return cls(push_id, DataKey.generate(push_id))


@dataclass(frozen=True)
class UploadAllowance:
    """How many bytes the user may upload right now, and why that's the limit."""

    max_bytes: int
    limited_by_quota: bool

    def check(self, size: int) -> None:
        if size <= self.max_bytes:
            return
        if self.limited_by_quota:
            raise APIError(413, "quota_exceeded", "Not enough storage quota left for this file")
        raise APIError(413, "file_too_large", f"Files are limited to {self.max_bytes} bytes")


async def upload_allowance(ctx: Context, db: AsyncSession, user: User) -> UploadAllowance:
    remaining = ctx.settings.user_quota - await stored_bytes(db, user.id)
    await db.commit()  # don't hold the read transaction during the upload
    if remaining < ctx.settings.max_file_size:
        return UploadAllowance(max(remaining, 0), limited_by_quota=True)
    return UploadAllowance(ctx.settings.max_file_size, limited_by_quota=False)


async def receive_file(
    ctx: Context, new: NewPush, stream: AsyncIterator[bytes], allowance: UploadAllowance
) -> tuple[int, str]:
    """Encrypt an upload stream to the blob store. Returns ``(size, sha256)``."""
    try:
        with ctx.blobs.open_partial(new.id) as fh:
            writer = EncryptingWriter(fh, new.key)
            async for chunk in stream:
                writer.write(chunk)
                allowance.check(writer.size)
            writer.finish()
        ctx.blobs.commit_partial(new.id)
    except BaseException:
        ctx.blobs.delete(new.id)
        raise
    return writer.size, writer.sha256


async def send_text(
    ctx: Context,
    db: AsyncSession,
    principal: Principal,
    *,
    kind: TextKind,
    to: Targets,
    title: str | None = None,
    body: str | None = None,
    url: str | None = None,
    ttl: int | str | None = None,
) -> PushStatus:
    """Send a link or a note."""
    check_push_rate(ctx, principal)
    check_text_length(ctx, title, body, url)
    ttl_seconds = resolve_ttl(ctx, ttl)
    targets = await resolve_targets(db, principal, to)
    return await create_push(
        ctx,
        db,
        principal,
        new=NewPush.create(),
        kind=kind,
        targets=targets,
        meta={"title": title, "body": body, "url": url},
        ttl=ttl_seconds,
    )


async def send_file(
    ctx: Context,
    db: AsyncSession,
    principal: Principal,
    stream: AsyncIterator[bytes],
    *,
    to: Targets,
    filename: str,
    mime: str,
    declared_size: int | None = None,
    title: str | None = None,
    body: str | None = None,
    ttl: int | str | None = None,
) -> PushStatus:
    """Send a file, encrypting the upload stream as it arrives."""
    check_push_rate(ctx, principal)
    check_text_length(ctx, title, body)
    ttl_seconds = resolve_ttl(ctx, ttl)
    targets = await resolve_targets(db, principal, to)
    allowance = await upload_allowance(ctx, db, principal.user)
    if declared_size is not None:
        allowance.check(declared_size)  # fail before receiving anything
    new = NewPush.create()
    size, sha256 = await receive_file(ctx, new, stream, allowance)
    return await create_push(
        ctx,
        db,
        principal,
        new=new,
        kind="file",
        targets=targets,
        meta={"title": title, "body": body, "filename": filename, "mime": mime, "sha256": sha256},
        ttl=ttl_seconds,
        file_size=size,
    )


async def create_push(
    ctx: Context,
    db: AsyncSession,
    principal: Principal,
    *,
    new: NewPush,
    kind: PushKind,
    targets: list[Device],
    meta: dict[str, Any],
    ttl: int,
    file_size: int | None = None,
) -> PushStatus:
    now = timeutil.utcnow()
    push = Push(
        id=new.id,
        user_id=principal.user.id,
        source_device_id=principal.device.id if principal.device else None,
        source_app_token_id=principal.app_token.id if principal.app_token else None,
        kind=kind,
        key_id=ctx.master.key_id,
        wrapped_key=new.key.wrap(ctx.master),
        meta=new.key.encrypt_meta({k: v for k, v in meta.items() if v is not None}),
        file_size=file_size,
        file_stored=file_size is not None,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    db.add(push)
    await db.flush()
    deliveries = [Delivery(push_id=new.id, device_id=d.id, state=PENDING) for d in targets]
    db.add_all(deliveries)
    try:
        await db.commit()
    except BaseException:
        if file_size is not None:
            ctx.blobs.delete(new.id)
        raise
    for device in targets:
        ctx.dispatcher.notify(device, new.id, kind)
    return (await statuses(db, [push]))[0]


# --- reading ------------------------------------------------------------------------------


def decrypt(ctx: Context, push: Push) -> tuple[DataKey, dict[str, Any]]:
    if push.wrapped_key is None or push.meta is None:
        raise APIError(410, "gone", "This push's content has been deleted")
    dek = DataKey.unwrap(ctx.master, push.wrapped_key, push.id)
    return dek, dek.decrypt_meta(push.meta)


def _item_fields(push: Push, meta: dict[str, Any], sender: str | None = None) -> dict[str, Any]:
    file = None
    if push.kind == "file":
        file = FileInfo(
            name=meta.get("filename") or push.id,
            mime=meta.get("mime") or "application/octet-stream",
            size=push.file_size or 0,
            sha256=meta.get("sha256", ""),
            available=push.file_stored,
        )
    return {
        "id": push.id,
        "kind": push.kind,
        "created_at": push.created_at,
        "expires_at": push.expires_at,
        # Never reveal a contact's device ids.
        "source_device_id": None if sender else push.source_device_id,
        "sender": sender,
        "title": meta.get("title"),
        "body": meta.get("body"),
        "url": meta.get("url"),
        "file": file,
    }


async def to_items(
    ctx: Context, db: AsyncSession, device: Device, pushes: Sequence[Push]
) -> list[InboxItem]:
    """Inbox items for ``device``, naming the sender of pushes shared by other accounts."""
    foreign = {p.user_id for p in pushes if p.user_id != device.user_id}
    names: dict[str, str] = {}
    if foreign:
        rows = await db.execute(select(User.id, User.username).where(User.id.in_(foreign)))
        names = {user_id: name for user_id, name in rows}
    items = []
    for push in pushes:
        _, meta = decrypt(ctx, push)
        items.append(InboxItem(**_item_fields(push, meta, names.get(push.user_id))))
    return items


def _open_inbox(device: Device, now: datetime) -> Any:
    return (
        select(Push)
        .join(Delivery, Delivery.push_id == Push.id)
        .where(
            Delivery.device_id == device.id,
            Delivery.state.in_(OPEN_STATES),
            Push.expires_at > now,
            Push.purged_at.is_(None),
        )
    )


async def list_inbox(
    ctx: Context, db: AsyncSession, device: Device, *, after: str | None, limit: int
) -> list[InboxItem]:
    now = timeutil.utcnow()
    query = _open_inbox(device, now).order_by(Push.id).limit(limit)
    if after:
        query = query.where(Push.id > after)
    found: list[Push] = list(await db.scalars(query))
    if found:
        await db.execute(
            update(Delivery)
            .where(
                Delivery.device_id == device.id,
                Delivery.push_id.in_([p.id for p in found]),
                Delivery.state == PENDING,
            )
            .values(state=DELIVERED, delivered_at=now)
        )
    items = await to_items(ctx, db, device, found)
    await db.commit()
    return items


async def get_inbox_push(db: AsyncSession, device: Device, push_id: str) -> Push:
    push: Push | None = await db.scalar(
        _open_inbox(device, timeutil.utcnow()).where(Push.id == push_id)
    )
    if push is None:
        raise not_found("Push")
    return push


async def ack(ctx: Context, db: AsyncSession, device: Device, push_ids: Sequence[str]) -> list[str]:
    """Acknowledge deliveries. Idempotent; returns the ids that are now acked."""
    now = timeutil.utcnow()
    deliveries = list(
        await db.scalars(
            select(Delivery).where(
                Delivery.device_id == device.id, Delivery.push_id.in_(list(set(push_ids)))
            )
        )
    )
    for delivery in deliveries:
        if delivery.state in OPEN_STATES:
            delivery.state = ACKED
            delivery.acked_at = now
    await maybe_purge(ctx, db, [d.push_id for d in deliveries], now)
    await commit(ctx, db)
    acked = {d.push_id for d in deliveries if d.state == ACKED}
    return [i for i in dict.fromkeys(push_ids) if i in acked]


# --- purging ------------------------------------------------------------------------------


async def maybe_purge(
    ctx: Context, db: AsyncSession, push_ids: Sequence[str], now: datetime
) -> None:
    """Purge the given pushes whose deliveries are all acked or expired."""
    if not push_ids:
        return
    await db.flush()
    ids = list(set(push_ids))
    still_open = set(
        await db.scalars(
            select(Delivery.push_id).where(
                Delivery.push_id.in_(ids), Delivery.state.in_(OPEN_STATES)
            )
        )
    )
    candidates = await db.scalars(select(Push).where(Push.id.in_(ids), Push.purged_at.is_(None)))
    for push in candidates:
        if push.id not in still_open:
            purge(ctx, db, push, now)


def purge(
    ctx: Context, db: AsyncSession, push: Push, now: datetime, *, history: bool = True
) -> None:
    """Delete a push's content, or move it to the sender's history when that is enabled."""
    push.purged_at = now
    days = ctx.settings.history_days
    if history and days > 0 and push.source_device_id is not None:
        push.history_until = now + timedelta(days=days)
        if not ctx.settings.history_keep_files:
            drop_file(db, push)
    else:
        drop_content(db, push)


def drop_content(db: AsyncSession, push: Push) -> None:
    push.meta = None
    push.wrapped_key = None
    push.history_until = None
    drop_file(db, push)


def drop_file(db: AsyncSession, push: Push) -> None:
    if push.file_stored:
        push.file_stored = False
        delete_blob_after_commit(db, push.id)


async def recall(ctx: Context, db: AsyncSession, push: Push) -> None:
    """Sender takes a push back: pending deliveries expire and all content is deleted."""
    now = timeutil.utcnow()
    open_devices = list(
        await db.scalars(
            select(Delivery.device_id).where(
                Delivery.push_id == push.id, Delivery.state.in_(OPEN_STATES)
            )
        )
    )
    await db.execute(
        update(Delivery)
        .where(Delivery.push_id == push.id, Delivery.state.in_(OPEN_STATES))
        .values(state=EXPIRED)
    )
    if push.purged_at is None:
        push.purged_at = now
    drop_content(db, push)
    await commit(ctx, db)
    for device_id in open_devices:
        ctx.hub.publish(device_id, {"type": "recalled", "id": push.id})


# --- sender side --------------------------------------------------------------------------


async def statuses(db: AsyncSession, pushes: Sequence[Push]) -> list[PushStatus]:
    """Delivery status of pushes. Deliveries to contacts show their username, not a device."""
    by_push: dict[str, list[DeliveryOut]] = {p.id: [] for p in pushes}
    owner = {p.id: p.user_id for p in pushes}
    if pushes:
        rows = await db.execute(
            select(Delivery, Device.user_id, User.username)
            .join(Device, Device.id == Delivery.device_id)
            .join(User, User.id == Device.user_id)
            .where(Delivery.push_id.in_(list(by_push)))
            .order_by(Delivery.device_id)
        )
        for delivery, device_user_id, username in rows:
            shared = device_user_id != owner[delivery.push_id]
            by_push[delivery.push_id].append(
                DeliveryOut(
                    device_id=None if shared else delivery.device_id,
                    recipient=username if shared else None,
                    state=delivery.state,
                    delivered_at=delivery.delivered_at,
                    acked_at=delivery.acked_at,
                )
            )
    return [
        PushStatus(
            id=p.id,
            kind=p.kind,
            created_at=p.created_at,
            expires_at=p.expires_at,
            purged_at=p.purged_at,
            source_device_id=p.source_device_id,
            deliveries=by_push[p.id],
        )
        for p in pushes
    ]


def _visible_to(principal: Principal) -> ColumnElement[bool]:
    """Pushes a principal may see and recall: sessions and devices see the whole account's;
    an app token (send-only, often embedded in scripts) sees only the ones it sent."""
    if principal.app_token is not None:
        return Push.source_app_token_id == principal.app_token.id
    return Push.user_id == principal.user.id


async def get_sent_push(db: AsyncSession, principal: Principal, push_id: str) -> Push:
    push: Push | None = await db.scalar(
        select(Push).where(Push.id == push_id, _visible_to(principal))
    )
    if push is None:
        raise not_found("Push")
    return push


async def list_sent(
    db: AsyncSession, principal: Principal, *, before: str | None, limit: int
) -> list[PushStatus]:
    query = select(Push).where(_visible_to(principal)).order_by(Push.id.desc()).limit(limit)
    if before:
        query = query.where(Push.id < before)
    return await statuses(db, list(await db.scalars(query)))


def _history_query(device: Device, now: datetime) -> Any:
    """Pushes this device sent whose content is still around (pending or kept in history)."""
    return select(Push).where(
        Push.source_device_id == device.id,
        Push.meta.is_not(None),
        (Push.purged_at.is_(None)) | (Push.history_until > now),
    )


async def list_history(
    ctx: Context, db: AsyncSession, device: Device, *, before: str | None, limit: int
) -> list[HistoryItem]:
    query = _history_query(device, timeutil.utcnow()).order_by(Push.id.desc()).limit(limit)
    if before:
        query = query.where(Push.id < before)
    items = []
    for push in await db.scalars(query):
        _, meta = decrypt(ctx, push)
        items.append(
            HistoryItem(
                **_item_fields(push, meta),
                purged_at=push.purged_at,
                history_until=push.history_until,
            )
        )
    return items


async def get_history_push(db: AsyncSession, device: Device, push_id: str) -> Push:
    push: Push | None = await db.scalar(
        _history_query(device, timeutil.utcnow()).where(Push.id == push_id)
    )
    if push is None:
        raise not_found("History entry")
    return push
