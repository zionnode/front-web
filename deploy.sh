#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Auto-load .env (Compose loads it automatically, bash does not)
if [[ -f ./.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

DOMAIN_FILE="./app/domain.list"
PROXY_FILE="./app/proxy_pass"

# ====== 可通过 .env 覆盖 ======
# CERTBOT_EMAIL 在 .env 里配置最合适
CERTBOT_EMAIL="${CERTBOT_EMAIL:-wzhang@zionladder.com}"

# 1=先跑 staging（推荐），0=不跑
DO_STAGING="${DO_STAGING:-1}"

# 1=跑 production，0=不跑（默认建议 0，等你确认 staging 完整没问题再开）
DO_PROD="${DO_PROD:-0}"

# 1=prod 强制重签（可能触发限流），0=prod 默认 keep-until-expiring（推荐）
FORCE_PROD="${FORCE_PROD:-0}"

# 1=只有当域名 A 记录指向本机才处理，0=不检查
CHECK_A_RECORD="${CHECK_A_RECORD:-1}"

# 你现在建议先不配 AAAA，所以默认不检查 AAAA
CHECK_AAAA_RECORD="${CHECK_AAAA_RECORD:-0}"

STAGING_SUFFIX="-staging"

log() { echo "[$(date '+%F %T')] $*"; }

require_file() {
  local f="$1"
  if [[ ! -f "$f" ]]; then
    echo "Missing file: $f" >&2
    exit 1
  fi
}

# 获取本机公网 IPv4
get_public_ipv4() {
  curl -4 -s https://ifconfig.me || true
}

# 获取域名 A 记录（取第一个）
get_domain_a() {
  local d="$1"
  getent hosts "$d" 2>/dev/null | awk '{print $1}' | head -n1 || true
}

# 获取域名 AAAA（可选）
get_domain_aaaa() {
  local d="$1"
  getent ahosts "$d" 2>/dev/null | awk '{print $1}' | grep -E ':' | head -n1 || true
}

# 读取 domain.list，生成去重后的域名数组（保持顺序）
read_domains() {
  mapfile -t raw < <(grep -vE '^\s*($|#)' "$DOMAIN_FILE" | tr -d '\r')
  local -a out=()
  local seen=""
  for d in "${raw[@]}"; do
    d="$(echo "$d" | xargs)"
    [[ -z "$d" ]] && continue
    if [[ "$seen" != *"|$d|"* ]]; then
      out+=("$d")
      seen+="|$d|"
    fi
  done
  printf "%s\n" "${out[@]}"
}

# 按 apex 分组（apex + www）
# 输出：每行 "apex|name1 name2"
group_domains() {
  local -a domains=("$@")
  local set="|"
  for d in "${domains[@]}"; do set+="$d|"; done

  local -A groups=()
  for d in "${domains[@]}"; do
    local apex="$d"
    [[ "$d" == www.* ]] && apex="${d#www.}"
    groups["$apex"]=1
  done

  for apex in "${!groups[@]}"; do
    local names=()
    if [[ "$set" == *"|$apex|"* ]]; then names+=("$apex"); fi
    local www="www.$apex"
    if [[ "$set" == *"|$www|"* ]]; then names+=("$www"); fi
    # 若只有 www 没有 apex
    if [[ ${#names[@]} -eq 0 && "$set" == *"|www.$apex|"* ]]; then
      names+=("www.$apex")
    fi
    echo "$apex|${names[*]}"
  done | sort
}

cert_exists() {
  local certname="$1"
  [[ -f "./data/certbot/conf/live/$certname/fullchain.pem" && -f "./data/certbot/conf/live/$certname/privkey.pem" ]]
}

run_certbot_staging() {
  local certname="$1"; shift
  local -a names=("$@")
  log "[STAGING] Requesting cert: $certname (${names[*]})"
  docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    --non-interactive \
    --preferred-challenges http \
    --staging \
    --email "$CERTBOT_EMAIL" \
    --agree-tos --no-eff-email \
    --cert-name "$certname" \
    $(printf -- "-d %s " "${names[@]}")
}

run_certbot_prod() {
  local certname="$1"; shift
  local -a names=("$@")
  log "[PROD] Requesting cert: $certname (${names[*]})"
  local extra=("--keep-until-expiring")
  if [[ "$FORCE_PROD" == "1" ]]; then
    extra=("--force-renewal")
  fi
  docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    --non-interactive \
    --preferred-challenges http \
    --email "$CERTBOT_EMAIL" \
    --agree-tos --no-eff-email \
    --cert-name "$certname" \
    "${extra[@]}" \
    $(printf -- "-d %s " "${names[@]}")
}

# ====== main ======

# Ensure bind-mount directories exist (fresh clone)
mkdir -p ./data/nginx/sites ./data/nginx/logs ./data/certbot/www ./data/certbot/conf

require_file "$DOMAIN_FILE"
require_file "$PROXY_FILE"

log "Bringing stack up..."
docker compose up -d

# 确保 nginx 端口已发布
if ! docker compose port nginx 80 >/dev/null 2>&1; then
  log "nginx has no published ports, recreating nginx..."
  docker compose up -d --force-recreate --no-deps nginx
fi

mapfile -t DOMAINS < <(read_domains)

# Validate domain entries (must look like a hostname and contain at least one dot)
DOMAINS_VALID=()
for d in "${DOMAINS[@]}"; do
  if [[ "$d" == *.* ]] && [[ "$d" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
    DOMAINS_VALID+=("$d")
  else
    log "WARN invalid domain entry skipped: $d"
  fi
done
DOMAINS=("${DOMAINS_VALID[@]}")

if [[ ${#DOMAINS[@]} -eq 0 ]]; then
  echo "domain.list has no valid domains" >&2
  exit 1
fi

PUB4="$(get_public_ipv4)"
log "Public IPv4: ${PUB4:-<empty>}"

log "Processing domain groups..."
GROUPS=()
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  GROUPS+=("$line")
done < <(group_domains "${DOMAINS[@]}")

if [[ ${#GROUPS[@]} -eq 0 ]]; then
  echo "No domain groups produced (check app/domain.list formatting)." >&2
  exit 1
fi

if [[ "${DEBUG:-0}" == "1" ]]; then
  log "DEBUG groups: ${GROUPS[*]}"
fi

for line in "${GROUPS[@]}"; do
  apex="${line%%|*}"
  names_str="${line#*|}"
  # shellcheck disable=SC2206
  names=($names_str)

  # DNS A 检查（避免申请失败）
  if [[ "$CHECK_A_RECORD" == "1" ]]; then
    a="$(get_domain_a "$apex")"
    if [[ -z "$a" || -z "$PUB4" || "$a" != "$PUB4" ]]; then
      log "SKIP $apex: A record($a) != PublicIPv4($PUB4)"
      continue
    fi
  fi

  # （可选）AAAA 检查
  if [[ "$CHECK_AAAA_RECORD" == "1" ]]; then
    aaaa="$(get_domain_aaaa "$apex")"
    if [[ -n "$aaaa" ]]; then
      log "INFO $apex: AAAA exists ($aaaa). Ensure IPv6 80/443 is reachable."
    fi
  fi

  staging_name="${apex}${STAGING_SUFFIX}"
  prod_name="${apex}"

  # staging：没有就申请
  if [[ "$DO_STAGING" == "1" ]] && ! cert_exists "$staging_name"; then
    run_certbot_staging "$staging_name" "${names[@]}"
  else
    log "[STAGING] skip $staging_name (exists or disabled)"
  fi

  # prod：按开关决定是否申请
  if [[ "$DO_PROD" == "1" ]]; then
    run_certbot_prod "$prod_name" "${names[@]}"
  else
    log "[PROD] skip $prod_name (DO_PROD=0)"
  fi
done

log "Reloading nginx..."
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload

log "Done."