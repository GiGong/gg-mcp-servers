#!/usr/bin/env bash
# Render the launchd template with this machine's paths and load the service.
#
#   ./deploy/install.sh                    # label: com.local.kis-mcp
#   LABEL=com.myname.kis-mcp ./deploy/install.sh
#
# Re-run after changing .env to pick the new configuration up.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${LABEL:-com.local.kis-mcp}"
TEMPLATE="$PROJECT_DIR/deploy/com.example.kis-mcp.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ ! -x "$PROJECT_DIR/.venv/bin/kis-mcp" ]]; then
  echo "error: $PROJECT_DIR/.venv/bin/kis-mcp not found." >&2
  echo "       Run: uv pip install --python .venv/bin/python -e ." >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "error: $PROJECT_DIR/.env not found. Copy .env.example and fill it in." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/kis-mcp"

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__LABEL__|$LABEL|g" \
    "$TEMPLATE" > "$TARGET"

# bootout is expected to fail when the service was never loaded.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "installed: $TARGET"
echo "waiting for the server to come up..."

PORT="$(grep -E '^MCP_PORT=' "$PROJECT_DIR/.env" | cut -d= -f2- | tr -d '[:space:]')"
PORT="${PORT:-8765}"

for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "healthy: http://127.0.0.1:$PORT/healthz"
    curl -s "http://127.0.0.1:$PORT/healthz"; echo
    exit 0
  fi
  sleep 1
done

echo "error: the server did not become healthy on port $PORT." >&2
echo "       check $HOME/Library/Logs/kis-mcp/stderr.log" >&2
exit 1
