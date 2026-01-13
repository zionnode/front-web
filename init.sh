#!/usr/bin/env bash
set -euo pipefail

# front-web initializer
# - Creates required runtime directories
# - Ensures data/nginx/nginx.conf exists as a FILE (not a directory)
# - Starts the stack using Docker Compose v2: `docker compose`

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

say() { printf "[%s] %s\n" "$(date '+%F %T')" "$*"; }

# 1) Check docker
if ! command -v docker >/dev/null 2>&1; then
  say "ERROR: docker not found. Install Docker Engine first, then re-run ./init.sh"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  say "ERROR: Docker Compose plugin not found. Install docker-compose-plugin, then re-run ./init.sh"
  exit 1
fi

# 2) Create runtime directories (not committed to git)
mkdir -p data/nginx/sites
mkdir -p data/nginx/logs
mkdir -p data/certbot/www
mkdir -p data/certbot/conf

# 3) Ensure nginx.conf is a regular file
if [ -d "data/nginx/nginx.conf" ]; then
  say "WARN: data/nginx/nginx.conf is a directory; removing it so we can create the file"
  rm -rf data/nginx/nginx.conf
fi

if [ ! -f "data/nginx/nginx.conf" ]; then
  say "Creating data/nginx/nginx.conf"
  cat > data/nginx/nginx.conf <<'EOF'
user  nginx;
worker_processes auto;

error_log  /var/log/nginx/error.log warn;
pid        /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    log_format main '$remote_addr - $host [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log main;

    sendfile        on;
    keepalive_timeout  65;

    include /etc/nginx/conf.d/*.conf;
}
EOF
fi

# 4) Validate required local config files (NOT committed)
# We expect real deployment configs to live under ./app/
if [ ! -s "app/domain.list" ]; then
  say "ERROR: missing or empty app/domain.list (one domain per line). Create it before running ./init.sh"
  exit 1
fi

if [ ! -s "app/proxy_pass" ]; then
  say "ERROR: missing or empty app/proxy_pass (single upstream URL line). Create it before running ./init.sh"
  exit 1
fi

say "Init complete. Starting stack..."

docker compose up -d

docker compose ps

say "Done. You can edit ./app/domain.list and ./app/proxy_pass, then later we will add auto-reload logic in controller."