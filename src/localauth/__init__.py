"""localauth - a reusable MCP authentication module with no external IdP.

This package does not depend on any particular MCP server. Copy it or import
it from any FastMCP project.

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
