"""로깅 설정. 자격증명과 계좌번호는 포매터 단계에서 마스킹한다."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# 마스킹 규칙: (패턴, 치환)
# 로그 문자열에 자격증명이 섞여 들어오는 모든 경로를 방어적으로 막는다.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Bearer 토큰
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I), r"\1<REDACTED>"),
    # JSON/dict 형태의 키·시크릿·토큰
    (
        re.compile(
            r'(["\']?(?:appkey|appsecret|app_key|app_secret|access_token|refresh_token'
            r"|secretkey|secret_key|authorization|passphrase|signing_secret"
            r'|client_secret)["\']?\s*[:=]\s*["\']?)([^\s,"\'}\]]+)',
            re.I,
        ),
        r"\1<REDACTED>",
    ),
    # 계좌번호 8자리-2자리
    (re.compile(r"\b(\d{4})\d{4}[- ]?(\d{2})\b"), r"\1****\2"),
]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for pattern, replacement in _REDACTIONS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    """로그 이외의 경로(예: 예외 메시지)에서도 쓸 수 있는 마스킹 헬퍼."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redactor = RedactingFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(redactor)
        root.addHandler(rotating)

    # httpx 는 URL 전체를 INFO 로 찍는다. 쿼리스트링 노출을 막기 위해 낮춘다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
