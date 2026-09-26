from fastapi import APIRouter, Response
from sqlalchemy import select

from via import security
from via.db.models import AppToken
from via.deps import DB, SessionAuth
from via.errors import not_found
from via.schemas import AppTokenCreated, AppTokenIn, AppTokenOut

router = APIRouter(prefix="/app-tokens", tags=["app tokens"])


@router.get("")
async def list_app_tokens(principal: SessionAuth, db: DB) -> list[AppTokenOut]:
    result = await db.scalars(
        select(AppToken).where(AppToken.user_id == principal.user.id).order_by(AppToken.id)
    )
    return [AppTokenOut.model_validate(t) for t in result]


@router.post("", status_code=201)
async def create_app_token(body: AppTokenIn, principal: SessionAuth, db: DB) -> AppTokenCreated:
    """Create a send-only token for scripts and integrations. Shown only once."""
    token = security.new_token(security.APP)
    app_token = AppToken(
        user_id=principal.user.id, name=body.name, token_hash=security.hash_token(token)
    )
    db.add(app_token)
    await db.commit()
    return AppTokenCreated(app_token=AppTokenOut.model_validate(app_token), token=token)


@router.delete("/{token_id}", status_code=204)
async def delete_app_token(token_id: str, principal: SessionAuth, db: DB) -> Response:
    app_token = await db.get(AppToken, token_id)
    if app_token is None or app_token.user_id != principal.user.id:
        raise not_found("App token")
    await db.delete(app_token)
    await db.commit()
    return Response(status_code=204)
