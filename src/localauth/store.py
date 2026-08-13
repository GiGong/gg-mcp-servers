"""0600 권한으로 원자적 저장되는 아주 작은 JSON 상태 저장소.

서버 재시작 후에도 등록된 OAuth 클라이언트와 발급 토큰이 살아남아야
Claude.ai 커넥터가 매번 재인증을 요구하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("oauth store at %s is corrupt; starting empty", self._path)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def flush(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)

    def section(self, name: str) -> dict[str, Any]:
        return self._data.setdefault(name, {})

    def replace_section(self, name: str, value: dict[str, Any]) -> None:
        self._data[name] = value
