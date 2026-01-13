#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import socket
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent

DOMAIN_FILE = ROOT / "app" / "domain.list"
PROXY_FILE  = ROOT / "app" / "proxy_pass"

def log(msg: str):
    print(f"[{datetime.now().strftime('%F %T')}] {msg}", flush=True)

def load_env_file(env_path: Path):
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

def sh(cmd, check=True, capture=False):
    log(f"$ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )

DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$")

def read_domains():
    if not DOMAIN_FILE.exists():
        raise SystemExit(f"Missing file: {DOMAIN_FILE}")
    raw = []
    for line in DOMAIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().replace("\r", "")
        if not line or line.startswith("#"):
            continue
        raw.append(line)

    # de-dup keep order + validate
    seen = set()
    out = []
    for d in raw:
        if d in seen:
            continue
        seen.add(d)
        if "." not in d or not DOMAIN_RE.match(d):
            log(f"WARN invalid domain skipped: {d}")
            continue
        out.append(d)

    if not out:
        raise SystemExit("domain.list has no valid domains")
    return out

def group_domains(domains):
    # apex -> list(names)
    groups = {}
    present = set(domains)

    for d in domains:
        apex = d[4:] if d.startswith("www.") else d
        groups.setdefault(apex, [])
    for apex in list(groups.keys()):
        names = []
        if apex in present:
            names.append(apex)
        www = f"www.{apex}"
        if www in present:
            names.append(www)
        groups[apex] = names

    # stable order
    return [(apex, groups[apex]) for apex in sorted(groups.keys())]

def get_public_ipv4():
    # use curl like your bash did
    r = subprocess.run(["curl", "-4", "-s", "https://ifconfig.me"], text=True, stdout=subprocess.PIPE)
    return r.stdout.strip()

def resolve_a(domain: str):
    # return first IPv4 if exists
    try:
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            return ""
        return infos[0][4][0]
    except Exception:
        return ""

def ensure_dirs():
    (ROOT / "data" / "nginx" / "sites").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "nginx" / "logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "certbot" / "www").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "certbot" / "conf").mkdir(parents=True, exist_ok=True)

def cert_exists(certname: str):
    live = ROOT / "data" / "certbot" / "conf" / "live" / certname
    return (live / "fullchain.pem").exists() and (live / "privkey.pem").exists()

def certbot_cmd(certname: str, names, staging: bool, email: str, force: bool):
    cmd = [
        "docker", "compose", "run", "--rm",
        "--entrypoint", "certbot",
        "certbot",
        "certonly",
        "--webroot", "-w", "/var/www/certbot",
        "--non-interactive",
        "--preferred-challenges", "http",
        "--agree-tos",
        "--no-eff-email",
        "--cert-name", certname,
    ]
    if staging:
        cmd.append("--staging")
    if email:
        cmd += ["--email", email]
    else:
        cmd.append("--register-unsafely-without-email")

    # prod renewal behavior
    if not staging:
        cmd.append("--force-renewal" if force else "--keep-until-expiring")

    for n in names:
        cmd += ["-d", n]
    return cmd

def main():
    load_env_file(ROOT / ".env")

    email = os.environ.get("CERTBOT_EMAIL", "wzhang@zionladder.com")
    do_staging = os.environ.get("DO_STAGING", "1") == "1"
    do_prod = os.environ.get("DO_PROD", "0") == "1"
    force_prod = os.environ.get("FORCE_PROD", "0") == "1"
    check_a = os.environ.get("CHECK_A_RECORD", "1") == "1"
    check_aaaa = os.environ.get("CHECK_AAAA_RECORD", "0") == "1"  # 先不实现 AAAA 逻辑也行

    ensure_dirs()

    if not PROXY_FILE.exists():
        raise SystemExit(f"Missing file: {PROXY_FILE}")

    log("Bringing stack up...")
    sh(["docker", "compose", "up", "-d"], check=True)

    pub4 = get_public_ipv4()
    log(f"Public IPv4: {pub4 or '<empty>'}")

    domains = read_domains()
    log(f"Loaded domains ({len(domains)}): {' '.join(domains)}")

    groups = group_domains(domains)
    log(f"Group count: {len(groups)}")
    log("Groups: " + " | ".join([f"{a}=>{','.join(ns)}" for a, ns in groups]))

    for apex, names in groups:
        if not names:
            log(f"WARN {apex}: no names in list, skip")
            continue

        if check_a:
            a = resolve_a(apex)
            log(f"DNS A check apex={apex} resolved_A={a!r} public_ipv4={pub4!r}")
            if not a or not pub4 or a != pub4:
                log(f"SKIP {apex}: A record({a}) != PublicIPv4({pub4})")
                continue

        staging_name = f"{apex}-staging"
        prod_name = apex

        if do_staging:
            if cert_exists(staging_name):
                log(f"[STAGING] skip {staging_name} (exists)")
            else:
                log(f"[STAGING] Requesting cert: {staging_name} ({' '.join(names)})")
                sh(certbot_cmd(staging_name, names, staging=True, email=email, force=False), check=True)

        if do_prod:
            log(f"[PROD] Requesting cert: {prod_name} ({' '.join(names)})")
            sh(certbot_cmd(prod_name, names, staging=False, email=email, force=force_prod), check=True)
        else:
            log(f"[PROD] skip {prod_name} (DO_PROD=0)")

    log("Reloading nginx...")
    sh(["docker", "compose", "exec", "nginx", "nginx", "-t"], check=True)
    sh(["docker", "compose", "exec", "nginx", "nginx", "-s", "reload"], check=True)

    log("Done.")

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        log("ERROR: command failed")
        if e.stdout:
            print(e.stdout)
        sys.exit(e.returncode)