"""KIS 응답 필드 파싱 헬퍼.

KIS 는 숫자도 전부 문자열로 준다. 빈 문자열/공백/'-' 를 None 으로 정규화한다.
"""

from __future__ import annotations

from typing import Any

# 전일 대비 부호 코드 (KIS 공통)
SIGN_CODES = {
    "1": "상한",
    "2": "상승",
    "3": "보합",
    "4": "하한",
    "5": "하락",
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
    """KIS 는 대비값을 절대값 + 부호코드로 주는 경우가 있어 부호를 복원한다.

    이미 음수로 온 값은 그대로 둔다.
    """
    parsed = num(value)
    if parsed is None:
        return None
    if parsed <= 0:
        return parsed
    if text(sign_code) in {"4", "5"}:  # 하한/하락
        return -parsed
    return parsed


def yyyymmdd_to_iso(value: Any) -> str | None:
    raw = text(value)
    if not raw or len(raw) != 8 or not raw.isdigit():
        return raw
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
