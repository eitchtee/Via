"""Password hashing and bearer tokens."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SESSION = "s"
DEVICE = "d"
APP = "a"
INVITE = "i"
_KINDS = {SESSION, DEVICE, APP, INVITE}

_hasher = PasswordHasher()
# Verified against when the user doesn't exist, so login timing doesn't reveal usernames.
_DUMMY_HASH = _hasher.hash("via-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        ok = _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return ok and password_hash is not None


def new_token(kind: str) -> str:
    """Return a new token like ``via_d_<43 random chars>``. The prefix names its type."""
    return f"via_{kind}_{secrets.token_urlsafe(32)}"


def token_kind(token: str) -> str | None:
    parts = token.split("_", 2)
    if len(parts) == 3 and parts[0] == "via" and parts[1] in _KINDS and parts[2]:
        return parts[1]
    return None


def hash_token(token: str) -> str:
    # Tokens carry 256 bits of entropy, so a fast hash is enough.
    return hashlib.sha256(token.encode()).hexdigest()
