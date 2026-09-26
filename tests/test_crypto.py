import io

import pytest

from via.config import parse_duration, parse_size
from via.crypto import (
    CHUNK_SIZE,
    DataKey,
    DecryptionError,
    EncryptingWriter,
    MasterKey,
    generate_key,
    iter_plaintext,
)
from via.ids import new_id


def _encrypt(data: bytes, dek: DataKey) -> bytes:
    out = io.BytesIO()
    writer = EncryptingWriter(out, dek)
    # Write in odd-sized pieces to exercise buffering.
    for i in range(0, len(data), 10_007):
        writer.write(data[i : i + 10_007])
    writer.finish()
    assert writer.size == len(data)
    return out.getvalue()


@pytest.mark.parametrize(
    "size", [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 3 * CHUNK_SIZE + 17]
)
def test_file_roundtrip(size: int) -> None:
    data = bytes(i % 251 for i in range(size))
    dek = DataKey.generate("push1")
    blob = _encrypt(data, dek)
    assert b"".join(iter_plaintext(io.BytesIO(blob), dek, size)) == data


def test_ranges() -> None:
    size = 3 * CHUNK_SIZE + 100
    data = bytes(i % 251 for i in range(size))
    dek = DataKey.generate("push1")
    blob = _encrypt(data, dek)
    for start, end in [(0, 0), (5, 10), (CHUNK_SIZE - 1, CHUNK_SIZE), (100, 2 * CHUNK_SIZE + 3)]:
        got = b"".join(iter_plaintext(io.BytesIO(blob), dek, size, start, end))
        assert got == data[start : end + 1]


def test_tampering_and_truncation_are_detected() -> None:
    data = b"x" * (2 * CHUNK_SIZE + 5)
    dek = DataKey.generate("push1")
    blob = bytearray(_encrypt(data, dek))
    blob[100] ^= 1
    with pytest.raises(DecryptionError):
        b"".join(iter_plaintext(io.BytesIO(bytes(blob)), dek, len(data)))

    # Dropping the final chunk and claiming a shorter size must fail: chunk 1 isn't "last".
    intact = _encrypt(data, dek)
    truncated = intact[: 2 * (CHUNK_SIZE + 16)]
    with pytest.raises(DecryptionError):
        b"".join(iter_plaintext(io.BytesIO(truncated), dek, 2 * CHUNK_SIZE))


def test_keys_are_bound_to_their_push() -> None:
    master = MasterKey(bytes(32))
    dek = DataKey.generate("push1")
    wrapped = dek.wrap(master)
    meta = dek.encrypt_meta({"title": "hi"})
    assert DataKey.unwrap(master, wrapped, "push1").decrypt_meta(meta) == {"title": "hi"}
    with pytest.raises(DecryptionError):
        DataKey.unwrap(master, wrapped, "push2")
    with pytest.raises(DecryptionError):
        DataKey.unwrap(MasterKey(b"\x01" * 32), wrapped, "push1")


def test_master_key_is_generated_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    first = MasterKey.load(None, None, tmp_path)
    second = MasterKey.load(None, None, tmp_path)
    assert first.key_id == second.key_id
    assert MasterKey.load(generate_key(), None, tmp_path).key_id != first.key_id
    with pytest.raises(ValueError):
        MasterKey.load("not base64!", None, tmp_path)


def test_ids_are_sortable_and_unique() -> None:
    ids = [new_id() for _ in range(2000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 26 for i in ids)


def test_parsers() -> None:
    assert parse_size("100MiB") == 100 * 1024**2
    assert parse_size("1gb") == 10**9
    assert parse_size(42) == 42
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90") == 90
    for bad in ("7x", "d", "1h foo"):
        with pytest.raises(ValueError):
            parse_duration(bad)
