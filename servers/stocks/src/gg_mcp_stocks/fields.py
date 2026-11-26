"""Helpers for parsing KIS responses.

KIS returns every value as a string. Empty strings, whitespace and "-" are
normalised to None. Note that 0 and "no value" must stay distinguishable, so
falsy checks are not good enough.
"""

from __future__ import annotations

from typing import Any

# Day-over-day direction codes, shared across KIS endpoints.
SIGN_CODES = {
    "1": "upper limit",
    "2": "up",
    "3": "unchanged",
    "4": "lower limit",
    "5": "down",
}


def num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "null", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def integer(value: Any) -> int | None:
    parsed = num(value)
    return None if parsed is None else int(parsed)


def text(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def sign_label(code: Any) -> str | None:
    key = text(code)
    return SIGN_CODES.get(key) if key else None


def signed(value: Any, sign_code: Any) -> float | None:
    """Restore the sign of a change value.

    KIS sometimes reports the magnitude of a change alongside a separate
    direction code (`prdy_vrss_sign`). Missing this flips down days into up
    days. Values that already carry a sign are left alone.
    """
    parsed = num(value)
    if parsed is None:
        return None
    if parsed <= 0:
        return parsed
    if text(sign_code) in {"4", "5"}:  # lower limit / down
        return -parsed
    return parsed


def yyyymmdd_to_iso(value: Any) -> str | None:
    raw = text(value)
    if not raw or len(raw) != 8 or not raw.isdigit():
        return raw
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
