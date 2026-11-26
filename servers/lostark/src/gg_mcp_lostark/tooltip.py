"""Tooltip parsing.

A tooltip is a JSON string of `Element_NNN` objects, each `{type, value}`, with
game markup inside the values. Element numbering differs between items of the
same kind (the ark passive points box is Element_009 on a helmet and
Element_007 on a necklace) and slots can be null, so nothing here addresses an
element by number. Look-ups go by element type and by the label text inside it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .fields import clean, integer, lines

_ORDER = re.compile(r"(\d+)$")

# Element types that carry UI chrome rather than information.
_SKIP_TYPES = {"Progress", "ShowMeTheMoney", "BlinkLineEnd"}


@dataclass(frozen=True)
class Element:
    type: str
    value: Any


def _order(key: str) -> int:
    match = _ORDER.search(key)
    return int(match.group(1)) if match else 0


def parse(raw: Any) -> list[Element]:
    """Decode a tooltip into its elements, in display order. Never raises."""
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return [Element("Text", raw)]
    if not isinstance(data, dict):
        return []
    out: list[Element] = []
    for key in sorted(data, key=_order):
        element = data[key]
        if isinstance(element, dict) and element.get("value") is not None:
            out.append(Element(str(element.get("type", "")), element["value"]))
    return out


def part_boxes(elements: list[Element]) -> dict[str, list[str]]:
    """ItemPartBox label -> body lines, e.g. {"연마 효과": ["추가 피해 +1.60%", ...]}."""
    out: dict[str, list[str]] = {}
    for element in elements:
        if element.type != "ItemPartBox" or not isinstance(element.value, dict):
            continue
        label = clean(element.value.get("Element_000")) or ""
        out.setdefault(label, []).extend(lines(element.value.get("Element_001")))
    return out


def texts(elements: list[Element]) -> list[str]:
    """Lines from the free-text boxes."""
    out: list[str] = []
    for element in elements:
        if element.type in {"SingleTextBox", "MultiTextBox"} and isinstance(element.value, str):
            out.extend(lines(element.value))
    return out


def title(elements: list[Element]) -> dict[str, Any]:
    """The ItemTitle box: grade/kind line, level line and quality (None if n/a)."""
    for element in elements:
        if element.type == "ItemTitle" and isinstance(element.value, dict):
            value = element.value
            quality = integer(value.get("qualityValue"))
            return {
                "kind": clean(value.get("leftStr0")),
                "level_line": clean(value.get("leftStr2")),
                "quality": quality if quality is not None and quality >= 0 else None,
            }
    return {}


def indent_groups(elements: list[Element]) -> dict[str, list[tuple[str, str]]]:
    """IndentStringGroup topStr -> [(raw markup, cleaned text)].

    The raw markup is kept because some meaning lives only in colour, e.g. the
    red penalty engraving on an ability stone.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for element in elements:
        if element.type != "IndentStringGroup" or not isinstance(element.value, dict):
            continue
        for group in element.value.values():
            if not isinstance(group, dict):
                continue
            top = clean(group.get("topStr")) or ""
            content = group.get("contentStr")
            entries = content.values() if isinstance(content, dict) else []
            for entry in entries:
                raw = entry.get("contentStr") if isinstance(entry, dict) else entry
                if raw:
                    out.setdefault(top, []).append((str(raw), clean(raw) or ""))
    return out


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return lines(value)
    if isinstance(value, dict):
        out: list[str] = []
        for key in sorted(value, key=_order):
            out.extend(_strings(value[key]))
        return out
    if isinstance(value, list):
        return [line for item in value for line in _strings(item)]
    return []


def flatten(elements: list[Element]) -> list[str]:
    """Readable plain-text lines for the whole tooltip.

    Skill tooltips append a "next level" preview after BlinkLineStart; it
    describes a level the character does not have, so reading stops there.
    """
    out: list[str] = []
    for element in elements:
        kind, value = element.type, element.value
        if kind == "BlinkLineStart":
            break
        if kind in _SKIP_TYPES:
            continue
        if kind == "ItemTitle" and isinstance(value, dict):
            info = title([element])
            out.extend(x for x in (info.get("kind"), info.get("level_line")) if x)
            if info.get("quality") is not None:
                out.append(f"품질 {info['quality']}")
        elif kind == "CommonSkillTitle" and isinstance(value, dict):
            out.extend(
                x
                for x in (
                    clean(value.get("name")),
                    clean(value.get("level")),
                    clean(value.get("leftText")),
                    clean(value.get("middleText")),
                )
                if x
            )
        elif kind == "ItemPartBox" and isinstance(value, dict):
            label = clean(value.get("Element_000"))
            body = lines(value.get("Element_001"))
            if label:
                out.append(f"[{label}]")
            out.extend(body)
        elif kind == "IndentStringGroup":
            for top, entries in indent_groups([element]).items():
                if top:
                    out.append(f"[{top}]")
                out.extend(text for _, text in entries if text)
        elif kind == "TripodSkillCustom" and isinstance(value, dict):
            for slot in value.values():
                if isinstance(slot, dict) and not slot.get("lock"):
                    name, desc = clean(slot.get("name")), clean(slot.get("desc"))
                    out.append(" - ".join(x for x in (name, desc) if x))
        else:
            out.extend(_strings(value))
    return out
