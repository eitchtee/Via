"""Monotonic ULIDs: 26-character ids that sort by creation time."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = 0
_last_rand = 0


def new_id() -> str:
    global _last_ms, _last_rand
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            # Same millisecond (or clock went back): keep ordering by bumping the random part.
            ms, rand = _last_ms, _last_rand + 1
        else:
            rand = int.from_bytes(os.urandom(10))
        _last_ms, _last_rand = ms, rand
    value = (ms << 80) | (rand & ((1 << 80) - 1))
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
