"""Configuration loading.

The API key is read only from .env or the environment and is never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# servers/<name>/src/lostark_mcp/config.py -> servers/<name>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Official endpoint and quota, from the developer portal's getting-started page.
API_BASE_URL = "https://developer-lostark.game.onstove.com"
API_MAX_PER_MINUTE = 100


class ConfigError(RuntimeError):
    pass


def _expand(value: str) -> Path:
    path = Path(os.path.expanduser(value))
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    # --- Lost Ark API ---
    api_key: str
    rate_limit_per_min: int
    timeout: float
    max_wait_sec: float

    # --- local data ---
    data_dir: Path
    rankers_file: Path
    snapshot_db: Path
    collect_rate_limit_per_min: int
    collect_reserve: int

    # --- MCP ---
    host: str
    port: int
    mcp_path: str
    public_url: str

    # --- auth ---
    auth_mode: str
    oauth_passphrase: str
    oauth_signing_secret: str
    oauth_access_ttl: int
    oauth_refresh_ttl: int
    oauth_store: Path
    auth_page_label: str
    auth_page_subtitle: str

    # --- logging ---
    log_level: str
    log_file: Path | None

    def masked_api_key(self) -> str:
        """Every JWT starts with the same header bytes, so only the length is shown."""
        return f"<jwt, {len(self.api_key)} chars>"


def _rate(name: str, default: int) -> int:
    rate = int(os.getenv(name, str(default)))
    if rate <= 0:
        raise ConfigError(f"{name} must be > 0")
    if rate > API_MAX_PER_MINUTE:
        raise ConfigError(
            f"{name}={rate} exceeds the Lost Ark API limit ({API_MAX_PER_MINUTE}/min)."
        )
    return rate


def load_settings(env_file: Path | None = None, *, for_server: bool = True) -> Settings:
    """Load settings. `for_server=False` skips the auth checks for CLI tools."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    api_key = os.getenv("LOSTARK_API_KEY", "").strip()
    if api_key.lower().startswith("bearer "):
        raise ConfigError("LOSTARK_API_KEY must be the bare JWT, without 'bearer '.")
    if not api_key:
        raise ConfigError(
            "LOSTARK_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    auth_mode = os.getenv("AUTH_MODE", "oauth").strip().lower()
    if auth_mode not in {"oauth", "none"}:
        raise ConfigError(f"AUTH_MODE must be 'oauth' or 'none', got {auth_mode!r}")

    passphrase = os.getenv("OAUTH_PASSPHRASE", "")
    signing_secret = os.getenv("OAUTH_SIGNING_SECRET", "")
    public_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")

    if for_server and auth_mode == "oauth":
        if len(passphrase) < 16:
            raise ConfigError(
                "OAUTH_PASSPHRASE must be at least 16 characters when AUTH_MODE=oauth."
            )
        if len(signing_secret) < 16:
            raise ConfigError(
                "OAUTH_SIGNING_SECRET must be at least 16 characters when AUTH_MODE=oauth."
            )
        # Allow http on loopback so the OAuth flow can be exercised locally.
        is_loopback = public_url.startswith(("http://127.0.0.1", "http://localhost"))
        if not public_url.startswith("https://") and not is_loopback:
            raise ConfigError(
                "MCP_PUBLIC_URL must be set to the public https:// URL when AUTH_MODE=oauth."
            )

    collect_reserve = int(os.getenv("LOSTARK_COLLECT_RESERVE", "30"))
    if not 0 <= collect_reserve < API_MAX_PER_MINUTE:
        raise ConfigError("LOSTARK_COLLECT_RESERVE must be between 0 and 99")

    log_file_raw = os.getenv("LOG_FILE", "").strip()

    return Settings(
        api_key=api_key,
        rate_limit_per_min=_rate("LOSTARK_RATE_LIMIT_PER_MIN", 90),
        timeout=float(os.getenv("LOSTARK_TIMEOUT", "15")),
        max_wait_sec=float(os.getenv("LOSTARK_MAX_WAIT_SEC", "15")),
        data_dir=PROJECT_ROOT / "data",
        rankers_file=_expand(os.getenv("LOSTARK_RANKERS_FILE", "local/rankers.txt")),
        snapshot_db=_expand(os.getenv("LOSTARK_SNAPSHOT_DB", "local/snapshots.sqlite3")),
        collect_rate_limit_per_min=_rate("LOSTARK_COLLECT_RATE_LIMIT_PER_MIN", 50),
        collect_reserve=collect_reserve,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8766")),
        mcp_path=os.getenv("MCP_PATH", "/mcp"),
        public_url=public_url,
        auth_mode=auth_mode,
        oauth_passphrase=passphrase,
        oauth_signing_secret=signing_secret,
        oauth_access_ttl=int(os.getenv("OAUTH_ACCESS_TOKEN_TTL", "28800")),
        oauth_refresh_ttl=int(os.getenv("OAUTH_REFRESH_TOKEN_TTL", "2592000")),
        oauth_store=_expand(
            os.getenv("OAUTH_STORE", "~/.cache/lostark-mcp/oauth-store.json")
        ),
        # The login page is reachable without authentication, so the default
        # wording stays neutral. Operators override it in .env.
        auth_page_label=os.getenv("AUTH_PAGE_LABEL", "Private connector"),
        auth_page_subtitle=os.getenv(
            "AUTH_PAGE_SUBTITLE", "Enter the passphrase to connect."
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_file=_expand(log_file_raw) if log_file_raw else None,
    )
