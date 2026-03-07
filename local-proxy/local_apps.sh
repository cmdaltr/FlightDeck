#!/usr/bin/env bash
set -euo pipefail

echo "==> Caddy + Docker local reverse-proxy setup (macOS)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPS_FILE="${SCRIPT_DIR}/apps.conf"
CADDYFILE="${SCRIPT_DIR}/Caddyfile"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
HOSTS_FILE="/etc/hosts"

echo "Script directory: $SCRIPT_DIR"

echo "==> Checking Docker installation..."
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not in PATH."
  echo "Install Docker Desktop for Mac, then rerun this script."
  exit 1
fi

echo "Docker detected."

echo "==> Reading apps.conf..."
if [[ ! -f "$APPS_FILE" ]]; then
  echo "ERROR: apps.conf not found at: $APPS_FILE"
  exit 1
fi

echo "==> Generating Caddyfile..."

cat > "$CADDYFILE" << EOF
{
    local_certs
}

# Generated from $APPS_FILE
EOF

while read -r domain port _; do
  [[ -z "${domain:-}" ]] && continue
  [[ "$domain" =~ ^# ]] && continue
  [[ -z "${port:-}" ]] && continue

cat >> "$CADDYFILE" << EOF

$domain {
    reverse_proxy host.docker.internal:$port
}
EOF

done < "$APPS_FILE"

echo "Caddyfile created: $CADDYFILE"


echo "==> Generating docker-compose.yml..."

cat > "$COMPOSE_FILE" << EOF
version: "3.8"

services:
  caddy:
    image: caddy:2-alpine
    container_name: caddy-local
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  caddy_data:
  caddy_config:
EOF

echo "docker-compose.yml created"


echo "==> Updating /etc/hosts..."

sudo bash -c "
  if ! grep -q '# Local app domains (managed by local_apps.sh)' '$HOSTS_FILE'; then
    printf '\n# Local app domains (managed by local_apps.sh)\n' >> '$HOSTS_FILE'
  fi
"

while read -r domain port _; do
  [[ -z "${domain:-}" ]] && continue
  [[ "$domain" =~ ^# ]] && continue

  if ! grep -qw "$domain" "$HOSTS_FILE"; then
    echo "Adding: $domain"
    echo "127.0.0.1 $domain" | sudo tee -a "$HOSTS_FILE" >/dev/null
  else
    echo "Already exists: $domain"
  fi

done < "$APPS_FILE"


echo "==> Starting Caddy in Docker..."

if docker compose version >/dev/null 2>&1; then
  DC_CMD="docker compose"
else
  DC_CMD="docker-compose"
fi

cd "$SCRIPT_DIR"
$DC_CMD down >/dev/null 2>&1 || true
$DC_CMD up -d

echo ""
echo "==> Setup Complete! 🎉"
echo "Your apps are now available at:"
grep -vE '^\s*#|^\s*$' "$APPS_FILE" | awk '{print "  https://" $1}'
echo ""
echo "Modify apps in apps.conf → rerun ./local_apps.sh"