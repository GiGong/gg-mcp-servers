#!/usr/bin/env bash
# Render the launchd template for one server and load it.
#
#   ./deploy/install.sh kis
#   LABEL=com.myname.kis-mcp ./deploy/install.sh kis
#
# Re-run after editing that server's .env to pick up the new configuration.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="${1:-}"

if [[ -z "$SERVER" ]]; then
  echo "usage: $0 <server>    (available: $(ls "$REPO_DIR/servers" | tr '\n' ' '))" >&2
  exit 1
fi

SERVER_DIR="$REPO_DIR/servers/$SERVER"
LABEL="${LABEL:-com.local.$SERVER-mcp}"
TEMPLATE="$REPO_DIR/deploy/com.example.mcp.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[[ -d "$SERVER_DIR" ]] || { echo "error: no such server: $SERVER_DIR" >&2; exit 1; }
[[ -f "$SERVER_DIR/.env" ]] || {
  echo "error: $SERVER_DIR/.env not found. Copy .env.example and fill it in." >&2
  exit 1
}

BIN="$REPO_DIR/.venv/bin/$SERVER-mcp"
[[ -x "$BIN" ]] || {
  echo "error: $BIN not found. Run: uv sync" >&2
  exit 1
}

PORT="$(grep -E '^MCP_PORT=' "$SERVER_DIR/.env" | cut -d= -f2- | tr -d '[:space:]')"
PORT="${PORT:-8765}"

# Guard against a second copy of the same server under a different label, which
# would fight over the port and crash-loop.
for plist in "$HOME/Library/LaunchAgents/"*.plist; do
  [[ -e "$plist" ]] || continue
  other="$(basename "$plist" .plist)"
  [[ "$other" == "$LABEL" ]] && continue
  if grep -q "servers/$SERVER\b" "$plist" 2>/dev/null; then
    echo "error: '$other' already runs servers/$SERVER (port $PORT would clash)." >&2
    echo "       Keep it and pass LABEL=$other, or remove it first:" >&2
    echo "         launchctl bootout $DOMAIN/$other && rm '$plist'" >&2
    exit 1
  fi
done

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/$SERVER-mcp"

sed -e "s|__BIN__|$BIN|g" \
    -e "s|__SERVER_DIR__|$SERVER_DIR|g" \
    -e "s|__LOG_DIR__|$HOME/Library/Logs/$SERVER-mcp|g" \
    -e "s|__LABEL__|$LABEL|g" \
    "$TEMPLATE" > "$TARGET"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout returns before the old instance is gone. Bootstrapping too early
# fails with "Bootstrap failed: 5: Input/output error", so wait it out.
for _ in $(seq 1 20); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "installed: $TARGET"
echo "waiting for the server to come up..."

for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "healthy: http://127.0.0.1:$PORT/healthz"
    curl -s "http://127.0.0.1:$PORT/healthz"; echo
    exit 0
  fi
  sleep 1
done

echo "error: the server did not become healthy on port $PORT." >&2
echo "       check $HOME/Library/Logs/$SERVER-mcp/stderr.log" >&2
exit 1
