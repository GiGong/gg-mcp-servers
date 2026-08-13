"""localauth — 외부 IdP 없이 동작하는 재사용 가능한 MCP 인증 모듈.

이 패키지는 kis-mcp 에 종속되지 않는다. 다른 로컬 MCP 서버에서도
그대로 복사하거나 import 해서 쓸 수 있도록 설계했다.

    from localauth import PassphraseOAuthProvider

    auth = PassphraseOAuthProvider(
        base_url="https://my-mcp.example.com",
        passphrase=...,
        signing_secret=...,
        store_path=Path("~/.cache/my-mcp/oauth.json").expanduser(),
    )
    mcp = FastMCP("my-server", auth=auth)
"""

from .passphrase_oauth import PassphraseOAuthProvider
from .store import JsonStore

__all__ = ["PassphraseOAuthProvider", "JsonStore"]
