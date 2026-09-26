"""Encryption at rest (scheme ``server-v1``).

* A 256-bit **master key** wraps a random **data key** (DEK) per push (AES-256-GCM).
* Push metadata (title, body, url, file info) is one AES-GCM message under the DEK.
* File contents are split into fixed-size chunks, each its own AES-GCM message, so files can
  be streamed and ``Range`` requests only decrypt the chunks they touch. Each chunk's
  associated data binds it to the push, its index and whether it is the final chunk, which
  prevents reordering and truncation.

A DEK is only ever used for one push, so nonces are deterministic counters:
``0x00 || chunk index`` for file chunks and ``0x01 || 0…`` for the metadata.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

SCHEME = "server-v1"
KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 64 * 1024
_META_NONCE = b"\x01" + bytes(11)


class DecryptionError(Exception):
    pass


def generate_key() -> str:
    """Return a new random master key, base64 encoded."""
    return base64.b64encode(os.urandom(KEY_SIZE)).decode()


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except binascii.Error as e:
        raise ValueError("master key must be base64 encoded") from e
    if len(key) != KEY_SIZE:
        raise ValueError(f"master key must decode to {KEY_SIZE} bytes, got {len(key)}")
    return key


class MasterKey:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)
        self.key_id = hashlib.sha256(b"via/key-id/" + key).hexdigest()[:16]

    @classmethod
    def from_b64(cls, raw: str) -> MasterKey:
        return cls(_decode_key(raw))

    @classmethod
    def load(
        cls, master_key: str | None, master_key_file: Path | None, data_dir: Path
    ) -> MasterKey:
        """Load from ``VIA_MASTER_KEY``, ``VIA_MASTER_KEY_FILE`` or ``<data>/master.key``.

        When none exist, a key is generated into ``<data>/master.key``.
        """
        if master_key:
            return cls(_decode_key(master_key))
        if master_key_file:
            return cls(_decode_key(master_key_file.read_text()))
        path = data_dir / "master.key"
        if not path.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(generate_key() + "\n")
            log.warning(
                "Generated a new master key at %s. BACK IT UP: without it, stored content "
                "cannot be decrypted.",
                path,
            )
        return cls(_decode_key(path.read_text()))

    def seal(self, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        return nonce + self._aead.encrypt(nonce, data, aad)

    def open(self, blob: bytes, aad: bytes) -> bytes:
        try:
            return self._aead.decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], aad)
        except InvalidTag as e:
            raise DecryptionError("master key cannot decrypt this value") from e


class DataKey:
    """Per-push data encryption key."""

    def __init__(self, key: bytes, push_id: str) -> None:
        self._key = key
        self._aead = AESGCM(key)
        self._id = push_id.encode()

    @classmethod
    def generate(cls, push_id: str) -> DataKey:
        return cls(os.urandom(KEY_SIZE), push_id)

    @classmethod
    def unwrap(cls, master: MasterKey, wrapped: bytes, push_id: str) -> DataKey:
        return cls(master.open(wrapped, b"via/dek/" + push_id.encode()), push_id)

    def wrap(self, master: MasterKey) -> bytes:
        return master.seal(self._key, b"via/dek/" + self._id)

    def encrypt_meta(self, meta: dict[str, Any]) -> bytes:
        data = json.dumps(meta, separators=(",", ":")).encode()
        return self._aead.encrypt(_META_NONCE, data, b"via/meta/" + self._id)

    def decrypt_meta(self, blob: bytes) -> dict[str, Any]:
        try:
            data = self._aead.decrypt(_META_NONCE, blob, b"via/meta/" + self._id)
        except InvalidTag as e:
            raise DecryptionError("metadata failed authentication") from e
        result: dict[str, Any] = json.loads(data)
        return result

    def _chunk_params(self, index: int, last: bool) -> tuple[bytes, bytes]:
        nonce = b"\x00" + index.to_bytes(11, "big")
        aad = b"via/file/" + self._id + index.to_bytes(8, "big") + (b"\x01" if last else b"\x00")
        return nonce, aad

    def encrypt_chunk(self, index: int, data: bytes, *, last: bool) -> bytes:
        nonce, aad = self._chunk_params(index, last)
        return self._aead.encrypt(nonce, data, aad)

    def decrypt_chunk(self, index: int, data: bytes, *, last: bool) -> bytes:
        nonce, aad = self._chunk_params(index, last)
        try:
            return self._aead.decrypt(nonce, data, aad)
        except InvalidTag as e:
            raise DecryptionError(f"file chunk {index} failed authentication") from e


class EncryptingWriter:
    """Encrypts a byte stream into chunks as it is written. Call :meth:`finish` at the end."""

    def __init__(self, fh: BinaryIO, dek: DataKey) -> None:
        self._fh = fh
        self._dek = dek
        self._buf = bytearray()
        self._index = 0
        self._sha = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        self._sha.update(data)
        self.size += len(data)
        self._buf += data
        # Keep at least one byte buffered so the final chunk is only written by finish().
        while len(self._buf) > CHUNK_SIZE:
            self._emit(bytes(self._buf[:CHUNK_SIZE]), last=False)
            del self._buf[:CHUNK_SIZE]

    def finish(self) -> None:
        self._emit(bytes(self._buf), last=True)
        self._buf.clear()

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def _emit(self, chunk: bytes, *, last: bool) -> None:
        self._fh.write(self._dek.encrypt_chunk(self._index, chunk, last=last))
        self._index += 1


def chunk_count(size: int) -> int:
    return max(1, -(-size // CHUNK_SIZE))


def iter_plaintext(
    fh: BinaryIO, dek: DataKey, size: int, start: int = 0, end: int | None = None
) -> Iterator[bytes]:
    """Yield decrypted bytes ``start..end`` (inclusive) of a file of plaintext ``size``."""
    if size == 0:
        return
    end = size - 1 if end is None else end
    total = chunk_count(size)
    first, last = start // CHUNK_SIZE, end // CHUNK_SIZE
    for i in range(first, last + 1):
        fh.seek(i * (CHUNK_SIZE + TAG_SIZE))
        plain = dek.decrypt_chunk(i, fh.read(CHUNK_SIZE + TAG_SIZE), last=i == total - 1)
        lo = start - i * CHUNK_SIZE if i == first else 0
        hi = end - i * CHUNK_SIZE + 1 if i == last else len(plain)
        yield plain[lo:hi]
