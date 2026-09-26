from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from via import security, timeutil
from via.context import Context
from via.db.models import AuthSession, Invite, User
from via.errors import APIError

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
MIN_PASSWORD = 8


def normalize_username(username: str) -> str:
    return username.strip().lower()


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise APIError(422, "weak_password", f"Password must be at least {MIN_PASSWORD} characters")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(security.hash_password, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    return await asyncio.to_thread(security.verify_password, password_hash, password)


async def create_user(
    db: AsyncSession, username: str, password: str, *, is_admin: bool = False
) -> User:
    name = normalize_username(username)
    if not USERNAME_RE.match(name):
        raise APIError(
            422,
            "invalid_username",
            "Username must be 3-32 characters of letters, digits, '_', '.' or '-'",
        )
    check_password_strength(password)
    if await db.scalar(select(User.id).where(User.username == name)):
        raise APIError(409, "username_taken", "That username is taken")
    user = User(username=name, password_hash=await hash_password(password), is_admin=is_admin)
    db.add(user)
    await db.flush()
    return user


async def create_session(
    db: AsyncSession, user: User, ttl_seconds: int, user_agent: str | None
) -> tuple[str, AuthSession]:
    token = security.new_token(security.SESSION)
    session = AuthSession(
        user_id=user.id,
        token_hash=security.hash_token(token),
        expires_at=timeutil.utcnow() + timedelta(seconds=ttl_seconds),
        user_agent=(user_agent or "")[:256] or None,
    )
    db.add(session)
    await db.flush()
    return token, session


async def create_invite(
    db: AsyncSession, created_by: User | None, ttl_seconds: int
) -> tuple[str, Invite]:
    code = security.new_token(security.INVITE)
    invite = Invite(
        code_hash=security.hash_token(code),
        created_by=created_by.id if created_by else None,
        expires_at=timeutil.utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(invite)
    await db.flush()
    return code, invite


async def find_valid_invite(db: AsyncSession, code: str) -> Invite:
    invite = await db.scalar(select(Invite).where(Invite.code_hash == security.hash_token(code)))
    if invite is None or invite.used_at is not None or invite.expires_at <= timeutil.utcnow():
        raise APIError(403, "invalid_invite", "Invalid or expired invite code")
    return invite


async def bootstrap_admin(ctx: Context) -> None:
    """Create the admin from ``VIA_ADMIN_USERNAME``/``VIA_ADMIN_PASSWORD`` on an empty server."""
    settings = ctx.settings
    if not (settings.admin_username and settings.admin_password):
        return
    async with ctx.sessionmaker() as db:
        if await db.scalar(select(func.count()).select_from(User)):
            return
        await create_user(db, settings.admin_username, settings.admin_password, is_admin=True)
        await db.commit()
        log.info("created admin user %r", normalize_username(settings.admin_username))
