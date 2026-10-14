"""Logging setup. The API key and auth secrets are masked at format time.

Secrets can reach a log through many paths, so rather than scrubbing at every
call site this masks once, immediately before anything is written.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# (pattern, replacement) pairs applied to every log record.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I), r"\1<REDACTED>"),
    # Bare JWTs (the Lost Ark API key is one)
    (re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), "<JWT>"),
    # Keys, secrets and tokens in JSON or dict form
    (
        re.compile(
            r'(["\']?(?:api_key|access_token|refresh_token|authorization|passphrase'
            r'|signing_secret|client_secret)["\']?\s*[:=]\s*["\']?)([^\s,"\'}\]]+)',
            re.I,
        ),
        r"\1<REDACTED>",
    ),
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
    """Mask secrets outside the logging path, e.g. in exception messages."""
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

    # httpx logs full URLs at INFO; the client logs its own concise line instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
