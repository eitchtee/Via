from fastapi import APIRouter, Response

from via.db.models import PROVIDER_FCM_RELAY, PROVIDER_UNIFIEDPUSH, Device
from via.deps import DB, AnyAuth, Ctx, DeviceAuth, Principal, SessionAuth
from via.schemas import (
    DeviceCreated,
    DeviceIn,
    DeviceOut,
    DeviceUpdateIn,
    PushRegistrationIn,
    TokenOut,
)
from via.services import devices

router = APIRouter(prefix="/devices", tags=["devices"])


def _out(device: Device, principal: Principal) -> DeviceOut:
    out = DeviceOut.model_validate(device)
    out.current = principal.device is not None and principal.device.id == device.id
    return out


@router.post("", status_code=201)
async def register_device(body: DeviceIn, principal: SessionAuth, db: DB) -> DeviceCreated:
    """Register a device. The returned device token is the device's only credential for its
    inbox; it is shown only once."""
    token, device = await devices.register_device(
        db, principal.user, body.name, body.type, body.accepts_shares
    )
    await db.commit()
    return DeviceCreated(device=_out(device, principal), token=token)


@router.get("")
async def list_devices(principal: AnyAuth, db: DB) -> list[DeviceOut]:
    """The account's devices, e.g. for a "send to…" picker."""
    return [_out(d, principal) for d in await devices.list_devices(db, principal.user)]


@router.get("/me")
async def get_current_device(principal: DeviceAuth) -> DeviceOut:
    return _out(principal.this_device, principal)


@router.put("/me/push")
async def set_push_registration(
    body: PushRegistrationIn, principal: DeviceAuth, ctx: Ctx, db: DB
) -> DeviceOut:
    """Choose how this device is woken up: `fcm_relay` (with an FCM token), `unifiedpush`
    (with an endpoint URL) or `none` (SSE, long-poll or polling only)."""
    device = principal.this_device
    target = {PROVIDER_FCM_RELAY: body.token, PROVIDER_UNIFIEDPUSH: body.endpoint}.get(
        body.provider
    )
    devices.set_push_target(ctx, device, body.provider, target)
    db.add(device)
    await db.commit()
    return _out(device, principal)


@router.post("/me/rotate-token")
async def rotate_token(principal: DeviceAuth, db: DB) -> TokenOut:
    """Issue a new device token. The current one stops working immediately."""
    device = principal.this_device
    token = devices.rotate_token(device)
    db.add(device)
    await db.commit()
    return TokenOut(token=token)


def _apply(device: Device, body: DeviceUpdateIn) -> None:
    if body.name is not None:
        device.name = body.name
    if body.accepts_shares is not None:
        device.accepts_shares = body.accepts_shares


# Declared before PATCH /{device_id}, which would otherwise match "me" as an id.
@router.patch("/me")
async def update_current_device(body: DeviceUpdateIn, principal: DeviceAuth, db: DB) -> DeviceOut:
    """Rename this device, or choose whether it receives items shared by contacts. Other
    devices can only be changed with a session."""
    device = principal.this_device
    _apply(device, body)
    db.add(device)
    await db.commit()
    return _out(device, principal)


@router.patch("/{device_id}")
async def update_device(
    device_id: str, body: DeviceUpdateIn, principal: SessionAuth, db: DB
) -> DeviceOut:
    """Rename a device, or choose whether it receives items shared by contacts."""
    device = await devices.get_device(db, principal.user, device_id)
    _apply(device, body)
    await db.commit()
    return _out(device, principal)


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: str, principal: SessionAuth, ctx: Ctx, db: DB) -> Response:
    """Revoke a device. Its token stops working and items waiting for it are dropped."""
    device = await devices.get_device(db, principal.user, device_id)
    await devices.revoke_device(ctx, db, device)
    return Response(status_code=204)
