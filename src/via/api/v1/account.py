from fastapi import APIRouter, Response
from sqlalchemy import delete

from via.db.models import AuthSession
from via.deps import DB, Ctx, SessionAuth
from via.errors import APIError
from via.schemas import AccountOut, PasswordChangeIn, Usage, UserOut
from via.services import accounts, pushes

router = APIRouter(prefix="/account", tags=["account"])


@router.get("")
async def get_account(principal: SessionAuth, ctx: Ctx, db: DB) -> AccountOut:
    used = await pushes.stored_bytes(db, principal.user.id)
    return AccountOut(
        **UserOut.model_validate(principal.user).model_dump(),
        usage=Usage(pending_bytes=used, quota=ctx.settings.user_quota),
    )


@router.patch("/password", status_code=204)
async def change_password(body: PasswordChangeIn, principal: SessionAuth, db: DB) -> Response:
    """Change the password. Every other session is signed out."""
    user = principal.user
    if not await accounts.verify_password(user.password_hash, body.current_password):
        raise APIError(403, "invalid_credentials", "Current password is wrong")
    accounts.check_password_strength(body.new_password)
    user.password_hash = await accounts.hash_password(body.new_password)
    db.add(user)
    await db.execute(
        delete(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.id != principal.this_session.id
        )
    )
    await db.commit()
    return Response(status_code=204)
