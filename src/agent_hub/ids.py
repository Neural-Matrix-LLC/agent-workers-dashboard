"""Sortable ids (ULID layout: 48-bit millisecond time, 80 random bits) that never go backwards."""

import hashlib
import os
import threading
import time
from datetime import UTC, datetime

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last = 0


def new_id() -> str:
    global _last
    with _lock:
        value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10))
        if value <= _last:
            value = _last + 1
        _last = value
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
