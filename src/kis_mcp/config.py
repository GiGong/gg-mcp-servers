"""환경설정 로딩. 자격증명은 .env / 환경변수에서만 읽고 절대 로그로 내보내지 않는다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# KIS 공식 도메인. examples_llm/kis_auth.py 에서 확인.
KIS_DOMAINS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",
}

# KIS 공식 유량 한도 (앱키 단위). 설정값은 이 값을 넘을 수 없다.
KIS_MAX_RATE = {"real": 20.0, "demo": 5.0}


class ConfigError(RuntimeError):
    pass


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- KIS ---
    kis_env: str
    kis_app_key: str
    kis_app_secret: str
    kis_base_url: str
    kis_token_cache: Path
    kis_rate_limit_per_sec: float
    kis_timeout: float
    overseas_realtime: bool

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

    @property
    def is_demo(self) -> bool:
        return self.kis_env == "demo"

    def masked_app_key(self) -> str:
        """로그용. 앞 4자만 남기고 마스킹한다."""
        if len(self.kis_app_key) <= 4:
            return "****"
        return f"{self.kis_app_key[:4]}{'*' * 8}"


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    kis_env = os.getenv("KIS_ENV", "real").strip().lower()
    if kis_env not in KIS_DOMAINS:
        raise ConfigError(f"KIS_ENV must be one of {sorted(KIS_DOMAINS)}, got {kis_env!r}")

    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise ConfigError(
            "KIS_APP_KEY / KIS_APP_SECRET are not set. "
            "Copy .env.example to .env and fill them in."
        )

    rate = float(os.getenv("KIS_RATE_LIMIT_PER_SEC", "8"))
    ceiling = KIS_MAX_RATE[kis_env]
    if rate > ceiling:
        raise ConfigError(
            f"KIS_RATE_LIMIT_PER_SEC={rate} exceeds the KIS limit for "
            f"env={kis_env} ({ceiling}/s)."
        )
    if rate <= 0:
        raise ConfigError("KIS_RATE_LIMIT_PER_SEC must be > 0")

    auth_mode = os.getenv("AUTH_MODE", "oauth").strip().lower()
    if auth_mode not in {"oauth", "none"}:
        raise ConfigError(f"AUTH_MODE must be 'oauth' or 'none', got {auth_mode!r}")

    passphrase = os.getenv("OAUTH_PASSPHRASE", "")
    signing_secret = os.getenv("OAUTH_SIGNING_SECRET", "")
    public_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")

    if auth_mode == "oauth":
        if len(passphrase) < 16:
            raise ConfigError(
                "OAUTH_PASSPHRASE must be at least 16 characters when AUTH_MODE=oauth."
            )
        if len(signing_secret) < 16:
            raise ConfigError(
                "OAUTH_SIGNING_SECRET must be at least 16 characters when AUTH_MODE=oauth."
            )
        # 루프백 주소는 로컬 OAuth 플로우 검증용으로 http 를 허용한다.
        is_loopback = public_url.startswith(("http://127.0.0.1", "http://localhost"))
        if not public_url.startswith("https://") and not is_loopback:
            raise ConfigError(
                "MCP_PUBLIC_URL must be set to the https:// URL exposed by Cloudflare "
                "Tunnel when AUTH_MODE=oauth."
            )

    log_file_raw = os.getenv("LOG_FILE", "").strip()

    return Settings(
        kis_env=kis_env,
        kis_app_key=app_key,
        kis_app_secret=app_secret,
        kis_base_url=KIS_DOMAINS[kis_env],
        kis_token_cache=_expand(os.getenv("KIS_TOKEN_CACHE", "~/.cache/kis-mcp/token.json")),
        kis_rate_limit_per_sec=rate,
        kis_timeout=float(os.getenv("KIS_TIMEOUT", "10")),
        overseas_realtime=_get_bool("KIS_OVERSEAS_REALTIME", False),
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8765")),
        mcp_path=os.getenv("MCP_PATH", "/mcp"),
        public_url=public_url,
        auth_mode=auth_mode,
        oauth_passphrase=passphrase,
        oauth_signing_secret=signing_secret,
        oauth_access_ttl=int(os.getenv("OAUTH_ACCESS_TOKEN_TTL", "28800")),
        oauth_refresh_ttl=int(os.getenv("OAUTH_REFRESH_TOKEN_TTL", "2592000")),
        oauth_store=_expand(os.getenv("OAUTH_STORE", "~/.cache/kis-mcp/oauth-store.json")),
        # 인증 화면은 인증 없이 누구나 열 수 있다. 용도가 드러나지 않는 문구를 기본값으로 둔다.
        auth_page_label=os.getenv("AUTH_PAGE_LABEL", "mcp.example.com"),
        auth_page_subtitle=os.getenv(
            "AUTH_PAGE_SUBTITLE", "연결하려면 패스프레이즈를 입력하세요."
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_file=_expand(log_file_raw) if log_file_raw else None,
    )
