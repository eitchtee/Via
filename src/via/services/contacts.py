"""Contacts between accounts. Sharing requires an accepted contact, in either direction."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from via import timeutil
from via.db.models import CONTACT_ACCEPTED, CONTACT_PENDING, Contact, Device, User
from via.errors import APIError, not_found
from via.schemas import ContactOut
from via.services.accounts import normalize_username


def _involving(user: User) -> ColumnElement[bool]:
    return or_(Contact.requester_id == user.id, Contact.addressee_id == user.id)


def _between(a: User, b: User) -> ColumnElement[bool]:
    return or_(
        (Contact.requester_id == a.id) & (Contact.addressee_id == b.id),
        (Contact.requester_id == b.id) & (Contact.addressee_id == a.id),
    )


async def list_contacts(db: AsyncSession, user: User) -> list[ContactOut]:
    contacts = list(await db.scalars(select(Contact).where(_involving(user)).order_by(Contact.id)))
    other_ids = {c.addressee_id if c.requester_id == user.id else c.requester_id for c in contacts}
    rows = await db.execute(select(User.id, User.username).where(User.id.in_(other_ids)))
    names = {user_id: name for user_id, name in rows}
    return [
        ContactOut(
            id=c.id,
            username=names[c.addressee_id if c.requester_id == user.id else c.requester_id],
            status=c.status,
            direction="outgoing" if c.requester_id == user.id else "incoming",
            created_at=c.created_at,
            accepted_at=c.accepted_at,
        )
        for c in contacts
    ]


async def request_contact(db: AsyncSession, user: User, username: str) -> Contact:
    """Send a contact request. If they already asked you, this accepts theirs instead."""
    other = await db.scalar(select(User).where(User.username == normalize_username(username)))
    if other is None or other.disabled_at is not None:
        raise APIError(404, "user_not_found", "No such user")
    if other.id == user.id:
        raise APIError(422, "cannot_add_self", "You can't add yourself as a contact")
    existing = await db.scalar(select(Contact).where(_between(user, other)))
    if existing is not None:
        if existing.status == CONTACT_ACCEPTED:
            raise APIError(409, "already_contacts", "You are already contacts")
        if existing.requester_id == user.id:
            raise APIError(409, "already_requested", "You already sent a request")
        return accept(existing)
    contact = Contact(requester_id=user.id, addressee_id=other.id, status=CONTACT_PENDING)
    db.add(contact)
    await db.flush()
    return contact


def accept(contact: Contact) -> Contact:
    contact.status = CONTACT_ACCEPTED
    contact.accepted_at = timeutil.utcnow()
    return contact


async def get_contact(db: AsyncSession, user: User, contact_id: str) -> Contact:
    contact = await db.scalar(select(Contact).where(Contact.id == contact_id, _involving(user)))
    if contact is None:
        raise not_found("Contact")
    return contact


async def sharing_devices(db: AsyncSession, user: User, username: str) -> list[Device]:
    """Devices of an accepted contact that accept shares."""
    name = normalize_username(username)
    other = await db.scalar(select(User).where(User.username == name))
    contact = None
    if other is not None and other.disabled_at is None:
        contact = await db.scalar(
            select(Contact).where(
                Contact.status == CONTACT_ACCEPTED,
                _between(user, other),
            )
        )
    if other is None or contact is None:
        raise APIError(422, "unknown_contact", f"@{name} is not one of your contacts")
    devices = list(
        await db.scalars(
            select(Device)
            .where(
                Device.user_id == other.id,
                Device.revoked_at.is_(None),
                Device.accepts_shares.is_(True),
            )
            .order_by(Device.id)
        )
    )
    if not devices:
        raise APIError(422, "contact_unreachable", f"@{name} has no devices accepting shares")
    return devices
