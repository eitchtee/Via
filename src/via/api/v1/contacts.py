from fastapi import APIRouter, Response

from via.db.models import CONTACT_PENDING
from via.deps import DB, AnyAuth, SessionAuth
from via.errors import APIError
from via.schemas import ContactIn, ContactOut
from via.services import contacts

router = APIRouter(prefix="/contacts", tags=["contacts"])


async def _out(db: DB, principal: AnyAuth, contact_id: str) -> ContactOut:
    return next(c for c in await contacts.list_contacts(db, principal.user) if c.id == contact_id)


@router.get("")
async def list_contacts(principal: AnyAuth, db: DB) -> list[ContactOut]:
    """Your contacts and pending requests. Send to an accepted contact with
    `"to": ["@username"]`."""
    return await contacts.list_contacts(db, principal.user)


@router.post("", status_code=201)
async def add_contact(body: ContactIn, principal: SessionAuth, db: DB) -> ContactOut:
    """Send a contact request. If that user already asked you, this accepts their request."""
    contact = await contacts.request_contact(db, principal.user, body.username)
    await db.commit()
    return await _out(db, principal, contact.id)


@router.post("/{contact_id}/accept")
async def accept_contact(contact_id: str, principal: SessionAuth, db: DB) -> ContactOut:
    contact = await contacts.get_contact(db, principal.user, contact_id)
    if contact.status != CONTACT_PENDING or contact.addressee_id != principal.user.id:
        raise APIError(409, "not_acceptable", "Only a pending request sent to you can be accepted")
    contacts.accept(contact)
    await db.commit()
    return await _out(db, principal, contact.id)


@router.delete("/{contact_id}", status_code=204)
async def remove_contact(contact_id: str, principal: SessionAuth, db: DB) -> Response:
    """Decline or cancel a request, or remove a contact. Items already sent stay delivered."""
    contact = await contacts.get_contact(db, principal.user, contact_id)
    await db.delete(contact)
    await db.commit()
    return Response(status_code=204)
