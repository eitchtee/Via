from fastapi import APIRouter

from via.api.v1 import (
    account,
    admin,
    app_tokens,
    auth,
    contacts,
    devices,
    inbox,
    meta,
    pushes,
    send,
)

router = APIRouter(prefix="/v1")
for module in (meta, auth, account, admin, devices, contacts, app_tokens, pushes, send, inbox):
    router.include_router(module.router)
