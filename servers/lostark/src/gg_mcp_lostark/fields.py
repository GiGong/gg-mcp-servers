"""Helpers for parsing Lost Ark responses.

Numbers arrive as ints, floats or strings with thousands separators
("1,726.66"), and the same field can switch type between two items of the same
kind. Free text carries game markup (<FONT>, <img>, <BR>, <textformat>) even in
fields that are not tooltips, such as gem names.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

KST = dt.timezone(dt.timedelta(hours=9), name="KST")

_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"[ \t ]+")


def clean(value: Any) -> str | None:
    """Strip markup and collapse whitespace. Line breaks are kept."""
    if value is None:
        return None
    text = _BR.sub("\n", str(value))
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    joined = "\n".join(line for line in lines if line)
    return joined or None


def lines(value: Any) -> list[str]:
    """Clean text split into lines. `|` is a column separator in tooltips."""
    text = clean(value)
    if not text:
        return []
    out: list[str] = []
    for line in text.split("\n"):
        out.extend(part.strip() for part in line.split("|") if part.strip())
    return out


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def integer(value: Any) -> int | None:
    parsed = num(value)
    return None if parsed is None else int(parsed)


def compact(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop None, empty strings and empty containers. Zero and False are kept."""
    return {
        key: value
        for key, value in mapping.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def kst_iso(moment: dt.datetime | None = None) -> str:
    return (moment or now_kst()).isoformat(timespec="seconds")


def epoch_to_kst(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return kst_iso(dt.datetime.fromtimestamp(epoch, KST))


def kst_from_naive(value: Any) -> str | None:
    """The API sends KST timestamps without an offset; attach +09:00."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return kst_iso(parsed.replace(microsecond=0))


def percent(value: float | None, digits: int = 2) -> str | None:
    return None if value is None else f"{value:.{digits}f}%"
