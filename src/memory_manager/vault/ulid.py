# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal ULID generator and validator (https://github.com/ulid/spec).

A ULID is 128 bits: a 48-bit millisecond timestamp followed by 80 bits of
randomness, encoded as 26 characters of Crockford base32 (upper case).
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_CHAR_COUNT = 26
_CHAR_BITS = 5


def new_ulid(now: datetime | None = None) -> str:
    """Generate a new ULID.

    `now` defaults to the current UTC time; a naive `datetime` is treated as
    UTC.
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    timestamp_ms = int(moment.timestamp() * 1000)
    if timestamp_ms < 0 or timestamp_ms >= 1 << _TIMESTAMP_BITS:
        raise ValueError(f"timestamp out of ULID range: {timestamp_ms}")
    random_bits = secrets.randbits(_RANDOM_BITS)
    value = (timestamp_ms << _RANDOM_BITS) | random_bits
    chars = []
    for i in range(_CHAR_COUNT):
        shift = _CHAR_BITS * (_CHAR_COUNT - 1 - i)
        chars.append(_ENCODING[(value >> shift) & 0x1F])
    return "".join(chars)


def is_ulid(value: str) -> bool:
    """Check whether `value` has the shape of a valid ULID."""
    return bool(_ULID_RE.match(value))
