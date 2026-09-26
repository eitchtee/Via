"""Request and response models of the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DeviceType = Literal["android", "ios", "desktop", "browser", "cli", "other"]
PushKind = Literal["link", "note", "file"]
DeliveryState = Literal["pending", "delivered", "acked", "expired"]
PushProvider = Literal["none", "fcm_relay", "unifiedpush"]


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth & account -----------------------------------------------------------------------


class LoginIn(Model):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class RegisterIn(Model):
    username: str = Field(
        min_length=3,
        max_length=32,
        description="3-32 characters: letters, digits, `_`, `.`, `-`. Case-insensitive.",
    )
    password: str = Field(min_length=8, max_length=1024)
    invite: str | None = Field(None, description="Invite code, required when signup is `invite`")


class UserOut(Model):
    id: str
    username: str
    is_admin: bool
    created_at: datetime


class SessionOut(Model):
    token: str = Field(description="Session token (`via_s_…`). Shown only once.")
    expires_at: datetime
    user: UserOut


class Usage(Model):
    pending_bytes: int = Field(description="Bytes of stored files counting toward the quota")
    quota: int


class AccountOut(UserOut):
    usage: Usage


class PasswordChangeIn(Model):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class InviteIn(Model):
    expires_in: int | str | None = Field(
        None, description="Seconds or a duration like `7d`. Defaults to `VIA_INVITE_TTL`."
    )


class InviteOut(Model):
    id: str
    created_at: datetime
    expires_at: datetime


class InviteCreated(InviteOut):
    code: str = Field(description="Invite code (`via_i_…`). Shown only once.")


# --- devices & tokens ---------------------------------------------------------------------


class DeviceIn(Model):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Pixel 9", "type": "android"}]}
    )

    name: str = Field(min_length=1, max_length=64)
    type: DeviceType = "other"
    accepts_shares: bool | None = Field(
        None,
        description="Receive items shared by contacts. Defaults to true for android, ios and "
        "desktop devices.",
    )


class DeviceUpdateIn(Model):
    name: str | None = Field(None, min_length=1, max_length=64)
    accepts_shares: bool | None = None


class DeviceOut(Model):
    id: str
    name: str
    type: str
    created_at: datetime
    last_seen_at: datetime | None
    push_provider: str
    accepts_shares: bool
    current: bool = Field(False, description="True for the device making the request")


class DeviceCreated(Model):
    device: DeviceOut
    token: str = Field(description="Device token (`via_d_…`). Shown only once.")


class TokenOut(Model):
    token: str


class PushRegistrationIn(Model):
    provider: PushProvider
    token: str | None = Field(None, max_length=4096, description="FCM token, for `fcm_relay`")
    endpoint: str | None = Field(
        None, max_length=4096, description="UnifiedPush endpoint URL, for `unifiedpush`"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.provider == "fcm_relay" and not self.token:
            raise ValueError("`token` is required for fcm_relay")
        if self.provider == "unifiedpush" and not (
            self.endpoint and self.endpoint.startswith(("https://", "http://"))
        ):
            raise ValueError("`endpoint` must be an http(s) URL for unifiedpush")
        return self


class AppTokenIn(Model):
    name: str = Field(min_length=1, max_length=64)


class AppTokenOut(Model):
    id: str
    name: str
    scopes: str
    created_at: datetime
    last_used_at: datetime | None


class AppTokenCreated(Model):
    app_token: AppTokenOut
    token: str = Field(description="App token (`via_a_…`). Shown only once.")


# --- pushes -------------------------------------------------------------------------------

Targets = Literal["all"] | list[str]


class PushIn(Model):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"kind": "link", "url": "https://example.com", "title": "Example", "to": "all"},
                {"kind": "note", "body": "Remember the milk", "to": ["01J9Z3JX0A2B4C6D8E0F2G4H6J"]},
                {"kind": "link", "url": "https://example.com", "to": ["@bob"]},
            ]
        }
    )

    kind: Literal["link", "note"]
    to: Targets = Field(
        "all",
        description="`all` (every other device of yours), or a list of your device ids and "
        "`@username` entries for contacts",
    )
    title: str | None = Field(None, max_length=1024)
    body: str | None = None
    url: str | None = Field(None, max_length=8192)
    ttl: int | str | None = Field(
        None, description="Seconds or a duration like `1h`. Capped at the server maximum."
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind == "link" and not self.url:
            raise ValueError("a link needs a `url`")
        if self.kind == "note" and not (self.body or self.title):
            raise ValueError("a note needs a `body` or a `title`")
        if isinstance(self.to, list) and not self.to:
            raise ValueError("`to` must not be empty")
        return self


class FileInfo(Model):
    name: str
    mime: str
    size: int
    sha256: str
    available: bool = Field(True, description="False once a history entry's file was deleted")


class InboxItem(Model):
    id: str
    kind: PushKind
    created_at: datetime
    expires_at: datetime
    source_device_id: str | None = Field(
        description="Your device that sent it; null if sent by a session, an app token or a contact"
    )
    sender: str | None = Field(
        None, description="Username of the contact who shared it; null for your own items"
    )
    title: str | None = None
    body: str | None = None
    url: str | None = None
    file: FileInfo | None = None


class InboxPage(Model):
    items: list[InboxItem]
    cursor: str | None = Field(
        None, description="Pass as `after` to get the next page; null when this page is empty"
    )


class AckIn(Model):
    ids: list[str] = Field(min_length=1, max_length=1000)


class AckOut(Model):
    acked: list[str]


class DeliveryOut(Model):
    device_id: str | None = Field(description="Your device; null for a contact's device")
    recipient: str | None = Field(
        None, description="Contact's username, for deliveries to another account"
    )
    state: DeliveryState
    delivered_at: datetime | None
    acked_at: datetime | None


class PushStatus(Model):
    id: str
    kind: PushKind
    created_at: datetime
    expires_at: datetime
    purged_at: datetime | None
    source_device_id: str | None
    deliveries: list[DeliveryOut]


class HistoryItem(InboxItem):
    purged_at: datetime | None
    history_until: datetime | None


# --- contacts -----------------------------------------------------------------------------


class ContactIn(Model):
    username: str = Field(max_length=64)


class ContactOut(Model):
    id: str
    username: str = Field(description="The other account")
    status: Literal["pending", "accepted"]
    direction: Literal["incoming", "outgoing"] = Field(
        description="`incoming` if they sent the request, `outgoing` if you did"
    )
    created_at: datetime
    accepted_at: datetime | None


# --- admin --------------------------------------------------------------------------------


class AdminUserIn(Model):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=1024)
    is_admin: bool = False


class AdminUserUpdateIn(Model):
    is_admin: bool | None = None
    disabled: bool | None = None


class AdminPasswordIn(Model):
    password: str = Field(min_length=8, max_length=1024)


class AdminUserOut(UserOut):
    disabled_at: datetime | None
    devices: int
    stored_bytes: int
    last_seen_at: datetime | None


class ServerStats(Model):
    users: int
    devices: int
    pushes: int = Field(description="Pushes with content still stored")
    pending_deliveries: int
    stored_bytes: int


# --- meta ---------------------------------------------------------------------------------


class Limits(Model):
    max_file_size: int
    user_quota: int
    max_text_length: int
    default_ttl: int
    max_ttl: int


class Features(Model):
    sse: bool = True
    long_poll: bool = True
    unifiedpush: bool = True
    fcm_relay: bool
    history: bool
    contacts: bool = True
    web_ui: bool


class InfoOut(Model):
    name: str = "via"
    version: str
    api_version: int
    signup: str
    encryption: str
    limits: Limits
    features: Features
