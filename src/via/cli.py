"""The ``via`` command: run the server and administer it."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from via import __version__
from via.config import Settings, parse_duration
from via.crypto import MasterKey, generate_key
from via.db.engine import make_engine, make_sessionmaker, upgrade
from via.db.models import AuthSession, User
from via.errors import APIError
from via.lock import DataDirLock, DataDirLocked
from via.services import accounts, keys


def _serve(settings: Settings, args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "via.main:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level,
        timeout_graceful_shutdown=5,
    )


def _with_db(settings: Settings, fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
    async def run() -> None:
        upgrade(settings.db_path)
        engine = make_engine(settings.db_path)
        try:
            async with make_sessionmaker(engine)() as db:
                await fn(db)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match")
    return password


def _create_user(settings: Settings, args: argparse.Namespace) -> None:
    password = _read_password(args)

    async def fn(db: AsyncSession) -> None:
        user = await accounts.create_user(db, args.username, password, is_admin=args.admin)
        print(f"Created {'admin ' if user.is_admin else ''}user {user.username!r} ({user.id})")

    _with_db(settings, fn)


def _reset_password(settings: Settings, args: argparse.Namespace) -> None:
    password = _read_password(args)

    async def fn(db: AsyncSession) -> None:
        name = accounts.normalize_username(args.username)
        user = await db.scalar(select(User).where(User.username == name))
        if user is None:
            sys.exit(f"No user {name!r}")
        accounts.check_password_strength(password)
        user.password_hash = await accounts.hash_password(password)
        for session in await db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
            await db.delete(session)
        print(f"Password of {name!r} changed; all its sessions were signed out")

    _with_db(settings, fn)


def _create_invite(settings: Settings, args: argparse.Namespace) -> None:
    ttl = parse_duration(args.expires) if args.expires else settings.invite_ttl

    async def fn(db: AsyncSession) -> None:
        code, invite = await accounts.create_invite(db, None, ttl)
        print(code)
        print(f"(single use, expires {invite.expires_at:%Y-%m-%d %H:%M} UTC)", file=sys.stderr)

    _with_db(settings, fn)


def _gen_key(settings: Settings, args: argparse.Namespace) -> None:
    print(generate_key())


def _rotate_master_key(settings: Settings, args: argparse.Namespace) -> None:
    # A running server keeps using the old key: it couldn't read anything afterwards, and
    # items it stores meanwhile would be sealed with a key that is about to be discarded.
    # Holding the data directory lock also keeps the server from starting mid-rotation.
    try:
        with DataDirLock(settings.data_dir):
            _rotate(settings, args)
    except DataDirLocked:
        sys.exit("The Via server is running on this data directory. Stop it, then try again.")


def _rotate(settings: Settings, args: argparse.Namespace) -> None:
    old = MasterKey.load(settings.master_key, settings.master_key_file, settings.data_dir)
    new_raw = args.new_key_file.read_text().strip() if args.new_key_file else generate_key()
    new = MasterKey.from_b64(new_raw)
    if new.key_id == old.key_id:
        sys.exit("The new key is the same as the current one")

    # Save the new key before touching the database, so a crash can't leave data encrypted
    # under a key that exists nowhere.
    staged = settings.data_dir / "master.key.new"
    try:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        sys.exit(
            f"{staged} exists, probably from an interrupted rotation. If the server can't start "
            "with the current key, that file holds the key in use: rename it to master.key. "
            "Otherwise delete it and try again."
        )
    with os.fdopen(fd, "w") as fh:
        fh.write(new_raw + "\n")

    async def fn(db: AsyncSession) -> None:
        pushes, devices = await keys.rewrap(db, old, new)
        print(f"Re-encrypted the keys of {pushes} push(es) and {devices} device(s)")

    _with_db(settings, fn)

    if settings.master_key or settings.master_key_file:
        print(
            f"The database now uses the new key, saved in {staged}.\n"
            "Put it in VIA_MASTER_KEY / VIA_MASTER_KEY_FILE, delete that file, then start the "
            "server. The old key no longer works."
        )
    else:
        os.replace(staged, settings.data_dir / "master.key")
        print(
            f"Replaced {settings.data_dir / 'master.key'}. Back up the new key; the old one no "
            "longer works."
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="via", description="Via server")
    parser.add_argument("--version", action="version", version=f"via {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=_serve)

    p = sub.add_parser("create-user", help="create an account")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="make the user an admin")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=_create_user)

    p = sub.add_parser("reset-password", help="set a user's password")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=_reset_password)

    p = sub.add_parser("create-invite", help="print a single-use invite code")
    p.add_argument("--expires", help="e.g. 1d, 12h (default: VIA_INVITE_TTL)")
    p.set_defaults(func=_create_invite)

    p = sub.add_parser("gen-key", help="print a new random master key")
    p.set_defaults(func=_gen_key)

    p = sub.add_parser(
        "rotate-master-key",
        help="re-encrypt stored data under a new master key (stop the server first)",
    )
    p.add_argument(
        "--new-key-file", type=Path, help="use this key (base64) instead of generating one"
    )
    p.set_defaults(func=_rotate_master_key)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        args.func(Settings(), args)
    except APIError as e:
        sys.exit(f"error: {e.message}")


if __name__ == "__main__":
    main()
