import os
import sys
import time
from typing import Optional, Tuple

NGINX_SITES_DIR = "/app/nginx-sites"
NGINX_HTTP_CONF = "00-front-web-http.conf"


def _first_existing_file(paths) -> Optional[str]:
    for p in paths:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return None


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_configs() -> Tuple[list, str, str, str]:
    """Return (domains, proxy_pass, domain_path, proxy_path). Exits on error."""
    # In docker-compose we intend to mount ./app -> /app/app.
    # But keep compatibility with older layouts by probing multiple locations.
    domain_candidates = [
        "/app/app/domain.list",
        "/app/domain.list",
        os.path.join(os.getcwd(), "app", "domain.list"),
        os.path.join(os.getcwd(), "domain.list"),
    ]
    proxy_candidates = [
        "/app/app/proxy_pass",
        "/app/proxy_pass",
        os.path.join(os.getcwd(), "app", "proxy_pass"),
        os.path.join(os.getcwd(), "proxy_pass"),
    ]

    domain_path = _first_existing_file(domain_candidates)
    proxy_path = _first_existing_file(proxy_candidates)

    if not domain_path:
        print(
            "ERROR: domain.list not found or empty. Expected one of: "
            + ", ".join(domain_candidates),
            file=sys.stderr,
        )
        sys.exit(1)

    if not proxy_path:
        print(
            "ERROR: proxy_pass not found or empty. Expected one of: "
            + ", ".join(proxy_candidates),
            file=sys.stderr,
        )
        sys.exit(1)

    raw_domains = _read_text_file(domain_path).splitlines()
    domains = []
    for line in raw_domains:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        domains.append(s)

    if not domains:
        print(f"ERROR: {domain_path} contains no valid domains", file=sys.stderr)
        sys.exit(1)

    proxy_pass = ""
    for line in _read_text_file(proxy_path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        proxy_pass = s
        break

    if not proxy_pass:
        print(f"ERROR: {proxy_path} contains no valid proxy_pass line", file=sys.stderr)
        sys.exit(1)

    return domains, proxy_pass, domain_path, proxy_path


def write_http_vhost(domains: list, proxy_pass: str) -> None:
    """Write a minimal HTTP(80) nginx vhost that proxies to proxy_pass.

    This is MVP (no HTTPS yet). We keep the ACME challenge location so certbot
    can be added later without changing the http server block.
    """
    os.makedirs(NGINX_SITES_DIR, exist_ok=True)

    # Keep server_name exactly as the user's input order.
    server_names = " ".join(domains)
    conf_path = os.path.join(NGINX_SITES_DIR, NGINX_HTTP_CONF)

    conf = f"""server {{
    listen 80;
    server_name {server_names};

    location /.well-known/acme-challenge/ {{
        root /var/www/certbot;
    }}

    location / {{
        proxy_pass {proxy_pass};
        proxy_read_timeout    90;
        proxy_connect_timeout 90;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
    }}
}}
"""

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(conf)
    os.replace(tmp, conf_path)


def mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def main() -> None:
    print("front-web controller starting...", flush=True)

    domains, proxy_pass, domain_path, proxy_path = load_configs()
    last_domain_mtime = mtime(domain_path)
    last_proxy_mtime = mtime(proxy_path)

    print(f"Loaded domain.list from: {domain_path}")
    print(f"Loaded proxy_pass from:  {proxy_path}")
    print(f"Active domains ({len(domains)}): {', '.join(domains)}")
    print(f"proxy_pass: {proxy_pass}")

    write_http_vhost(domains, proxy_pass)
    print("Wrote nginx http vhost to /app/nginx-sites/00-front-web-http.conf (reload nginx to apply).", flush=True)

    # MVP: just stay alive and re-read configs when they change.
    # (Next steps will generate nginx vhosts and reload nginx.)
    while True:
        time.sleep(5)

        new_domain_mtime = mtime(domain_path)
        new_proxy_mtime = mtime(proxy_path)

        if new_domain_mtime != last_domain_mtime or new_proxy_mtime != last_proxy_mtime:
            try:
                domains, proxy_pass, _, _ = load_configs()
                print("Config changed; reloaded:")
                print(f"  domains ({len(domains)}): {', '.join(domains)}")
                print(f"  proxy_pass: {proxy_pass}")
                write_http_vhost(domains, proxy_pass)
                print("Wrote nginx http vhost to /app/nginx-sites/00-front-web-http.conf (reload nginx to apply).", flush=True)
                last_domain_mtime = new_domain_mtime
                last_proxy_mtime = new_proxy_mtime
            except SystemExit:
                # Keep running so you can fix files without the container restart-loop.
                print("Config reload failed; keeping last good config. Fix files and save again.")
            except Exception as e:
                print(f"Config reload error: {e!r}")


if __name__ == "__main__":
    main()