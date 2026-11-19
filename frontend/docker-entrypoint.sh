#!/bin/sh
# Runtime configuration for the operator dashboard.
#
# Rewrites dist/config.js from the API_BASE_URL environment variable, so a
# single built image can be pointed at any backend address without a rebuild.
set -eu

API_BASE_URL="${API_BASE_URL:-http://localhost:8001}"
CONFIG_FILE="/app/dist/config.js"

if [ -f "$CONFIG_FILE" ]; then
  # Escape the value for a single-quoted JS string.
  ESCAPED=$(printf '%s' "$API_BASE_URL" | sed "s/'/\\\\'/g")
  cat > "$CONFIG_FILE" <<EOF
// Generated at container start from API_BASE_URL.
window.__API_BASE_URL__ = '${ESCAPED}';
EOF
  echo "frontend: API base URL set to ${API_BASE_URL}"
else
  echo "frontend: warning - ${CONFIG_FILE} not found; serving without runtime config" >&2
fi

exec "$@"
