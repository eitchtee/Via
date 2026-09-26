from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from via import timeutil
from via.db.base import Base, UTCDateTime
from via.ids import new_id

# Delivery states
PENDING = "pending"
DELIVERED = "delivered"
ACKED = "acked"
EXPIRED = "expired"
OPEN_STATES = (PENDING, DELIVERED)

# Contact states
CONTACT_PENDING = "pending"
CONTACT_ACCEPTED = "accepted"

# Push providers (how a device is woken up)
PROVIDER_NONE = "none"
PROVIDER_FCM_RELAY = "fcm_relay"
PROVIDER_UNIFIEDPUSH = "unifiedpush"

# Device types that accept shares from contacts unless the owner turns it off.
SHARE_BY_DEFAULT = ("android", "ios", "desktop")


def _now() -> datetime:
    return timeutil.utcnow()


def _id_column() -> Mapped[str]:
    return mapped_column(String(26), primary_key=True, default=new_id)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = _id_column()
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuthSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = _id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    user_agent: Mapped[str | None] = mapped_column(String(256))


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = _id_column()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = _id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16))
    # Null once the device is revoked.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Whether items shared by contacts (other accounts) are delivered to this device.
    accepts_shares: Mapped[bool] = mapped_column(default=True, server_default=true())
    push_provider: Mapped[str] = mapped_column(String(16), default=PROVIDER_NONE)
    # FCM token or UnifiedPush endpoint, sealed with the master key.
    push_target: Mapped[bytes | None] = mapped_column(LargeBinary)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Contact(Base):
    """A contact between two accounts. Once accepted, either side can send to the other."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("requester_id", "addressee_id"),)

    id: Mapped[str] = _id_column()
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    addressee_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(12), default=CONTACT_PENDING)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AppToken(Base):
    __tablename__ = "app_tokens"

    id: Mapped[str] = _id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[str] = mapped_column(String(255), default="push:send")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Push(Base):
    __tablename__ = "pushes"

    id: Mapped[str] = _id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    source_app_token_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_tokens.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(8))
    encryption: Mapped[str] = mapped_column(String(16), default="server-v1")
    key_id: Mapped[str | None] = mapped_column(String(16))
    # Content columns; nulled when the push is purged.
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    meta: Mapped[bytes | None] = mapped_column(LargeBinary)
    file_size: Mapped[int | None]
    file_stored: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    history_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_deliveries_device_state", "device_id", "state"),)

    push_id: Mapped[str] = mapped_column(
        ForeignKey("pushes.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(12), default=PENDING)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
