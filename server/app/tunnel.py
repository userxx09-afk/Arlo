"""Point bcn.tapper.co at this server (Cloudflare Tunnel), from the Move page.

With a Cloudflare API token it does the whole thing:
  1. finds the zone (tapper.co) and its account
  2. creates (or reuses) a tunnel named "bcn-<this computer>" (or an older "ban-<this computer>"), managed from Cloudflare
  3. sets that tunnel to send bcn.tapper.co to http://localhost:<port>
  4. takes bcn.tapper.co off any other Cloudflare-managed tunnel (tapper's, for example)
  5. points the bcn.tapper.co DNS record (CNAME, proxied) at the new tunnel
  6. installs cloudflared as a Windows service with the tunnel's token (on Linux it shows the
     command to run, since Camera Keeper doesn't run as root there)

The token needs: Account > Cloudflare Tunnel > Edit, and Zone > DNS > Edit (for tapper.co).
It is used once and not saved.

Without an API token, a tunnel token made by hand in the Cloudflare dashboard can be pasted
instead; then step 6 is all that happens.
"""
import logging
import os
import re
import shutil
import socket
import subprocess
import time

import requests

from . import config, store

log = logging.getLogger("tunnel")
API = "https://api.cloudflare.com/client/v4"
KEY = "tunnel"
DEFAULT_HOSTNAME = "bcn.tapper.co"
# Names from before the rename to BCN. Phone apps, GarysTV settings and alert links sent before the
# rename still use them, so the tunnel carries them too, to the same place.
OLD_HOSTNAMES = {"bcn.tapper.co": ["ban.tapper.co"]}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class TunnelError(Exception):
    pass


def settings():
    return store.get_setting(KEY) or {}


def hostname():
    h = settings().get("hostname") or DEFAULT_HOSTNAME
    for new, old in OLD_HOSTNAMES.items():     # set up before the rename: use the new name now
        if h in old:
            return new
    return h


def _with_old_names(host):
    return [host] + OLD_HOSTNAMES.get(host, [])


class _CF:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"})

    def call(self, method, path, **kw):
        try:
            r = self.s.request(method, API + path, timeout=30, **kw)
        except requests.RequestException as e:
            raise TunnelError(f"Couldn't reach Cloudflare: {e}")
        try:
            body = r.json()
        except ValueError:
            raise TunnelError(f"Cloudflare answered {r.status_code} to {path}")
        if not body.get("success"):
            msgs = "; ".join(e.get("message", "") for e in body.get("errors") or []) or r.text[:200]
            if r.status_code in (401, 403):
                msgs += " (check the API token's permissions: Cloudflare Tunnel Edit and DNS Edit)"
            raise TunnelError(f"Cloudflare: {msgs}")
        return body.get("result")


def _zone_for(cf, host):
    parts = host.split(".")
    for i in range(len(parts) - 1):
        name = ".".join(parts[i:])
        zones = cf.call("GET", "/zones", params={"name": name})
        if zones:
            return zones[0]
    raise TunnelError(f"No Cloudflare zone for {host} is visible to that token.")


def _tunnel_name(prefix="bcn"):
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-") or "server"
    return f"{prefix}-{host}"


def _find_or_create_tunnel(cf, account, name):
    """This computer's tunnel. One made before the rename (ban-<computer>) is reused, not doubled."""
    for n in (name, _tunnel_name("ban")):
        found = cf.call("GET", f"/accounts/{account}/cfd_tunnel", params={"name": n, "is_deleted": "false"})
        if found:
            return found[0], False
    t = cf.call("POST", f"/accounts/{account}/cfd_tunnel", json={"name": name, "config_src": "cloudflare"})
    return t, True


def _ingress(cf, account, tunnel_id):
    try:
        conf = cf.call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations") or {}
    except TunnelError:
        return None
    return (conf.get("config") or {}).get("ingress")


def _set_ingress(cf, account, tunnel_id, rules):
    if not rules or rules[-1].get("hostname"):
        rules = [r for r in rules if r.get("hostname")] + [{"service": "http_status:404"}]
    cf.call("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", json={"config": {"ingress": rules}})


def _take_off_other_tunnels(cf, account, host, keep_id):
    """bcn.tapper.co comes off any other Cloudflare-managed tunnel; its other names stay."""
    moved = []
    for t in cf.call("GET", f"/accounts/{account}/cfd_tunnel", params={"is_deleted": "false"}) or []:
        if t["id"] == keep_id:
            continue
        rules = _ingress(cf, account, t["id"])
        if not rules or not any(r.get("hostname") == host for r in rules):
            continue
        _set_ingress(cf, account, t["id"], [r for r in rules if r.get("hostname") != host])
        moved.append(t.get("name") or t["id"])
    return moved


def _point_dns(cf, zone_id, host, tunnel_id):
    target = f"{tunnel_id}.cfargotunnel.com"
    rec = {"type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1,
           "comment": "BCN / Camera Keeper tunnel"}
    existing = cf.call("GET", f"/zones/{zone_id}/dns_records", params={"name": host}) or []
    for r in existing:             # an A/AAAA record for the name would block the CNAME
        if r["type"] in ("A", "AAAA"):
            cf.call("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")
    cname = next((r for r in existing if r["type"] == "CNAME"), None)
    if cname:
        if cname.get("content") == target and cname.get("proxied"):
            return "already pointed here"
        cf.call("PUT", f"/zones/{zone_id}/dns_records/{cname['id']}", json=rec)
        return f"moved from {cname.get('content')}"
    cf.call("POST", f"/zones/{zone_id}/dns_records", json=rec)
    return "created"


# --- cloudflared on this machine ------------------------------------------------
def _cloudflared():
    """cloudflared to run as the service. On Windows a copy is kept in ProgramData, so installing
    a Camera Keeper update never has to replace a running .exe."""
    if os.name == "nt":
        bundled = os.environ.get("CLOUDFLARED") or ""
        dest_dir = os.path.join(config.DATA_DIR, "cloudflared")
        dest = os.path.join(dest_dir, "cloudflared.exe")
        if bundled and os.path.isfile(bundled):
            os.makedirs(dest_dir, exist_ok=True)
            try:
                if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(bundled):
                    if os.path.exists(dest):        # the running service holds the old copy open
                        _run(["sc.exe", "stop", "Cloudflared"], check=False)
                        time.sleep(3)
                    shutil.copy2(bundled, dest)
            except OSError as e:
                log.warning("couldn't refresh cloudflared copy: %s", e)
        if os.path.exists(dest):
            return dest
    found = shutil.which("cloudflared")
    if not found:
        raise TunnelError("cloudflared isn't installed on this computer.")
    return found


def _run(cmd, check=True, timeout=90):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        raise TunnelError(f"{os.path.basename(cmd[0])} {cmd[1]} {cmd[2] if len(cmd) > 2 else ''}: {out[-400:]}")
    return out


def install_service(token):
    """Run cloudflared as a service with this tunnel token. Returns what was done."""
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-=.]{40,}", token):
        raise TunnelError("That doesn't look like a tunnel token (the long text after 'service install').")
    if os.name != "nt":
        return {"service": "manual",
                "command": f"sudo cloudflared service uninstall; sudo cloudflared service install {token}"}
    exe = _cloudflared()
    _run([exe, "service", "uninstall"], check=False)      # an older tunnel's service, if any
    time.sleep(2)
    _run([exe, "service", "install", token])
    _run(["sc.exe", "config", "Cloudflared", "start=", "delayed-auto"], check=False)
    _run(["sc.exe", "failure", "Cloudflared", "reset=", "86400", "actions=", "restart/10000/restart/10000/restart/60000"], check=False)
    _run(["sc.exe", "start", "Cloudflared"], check=False)
    return {"service": "installed", "exe": exe}


def service_state():
    if os.name != "nt":
        return None
    try:
        out = _run(["sc.exe", "query", "Cloudflared"], check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return None


def check_outside(host, wait=45):
    """Does https://host answer as BCN yet? (The manifest needs no sign-in.)"""
    deadline = time.time() + wait
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"https://{host}/manifest.webmanifest", timeout=8)
            if r.ok and ("Camera Notifications" in r.text or "Arlo Notifications" in r.text):
                return True, "answers"
            last = f"answered {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(4)
    return False, last


def _update_alert_links(host):
    """Alert links: use https://bcn.tapper.co if they were blank, a home address, or the old name."""
    from . import migrate, notify
    s = notify.current()
    old = [f"https://{h}" for h in OLD_HOSTNAMES.get(host, [])]
    if not s.get("public_url") or migrate._is_lan(s.get("public_url")) or s["public_url"].rstrip("/") in old:
        s["public_url"] = f"https://{host}"
        notify.save(s)
        return True
    return False


# --- the two ways in -----------------------------------------------------------------
def setup_with_api(api_token, host=None, port=None):
    host = (host or hostname()).strip().lower().rstrip(".")
    port = port or config.WEB_PORT
    if not api_token.strip():
        raise TunnelError("Paste a Cloudflare API token.")
    cf = _CF(api_token)
    steps = []
    zone = _zone_for(cf, host)
    account = zone["account"]["id"]
    name = _tunnel_name()
    t, created = _find_or_create_tunnel(cf, account, name)
    steps.append(f"Tunnel “{t.get('name') or name}” {'created' if created else 'found'}.")
    hosts = _with_old_names(host)
    rules = [r for r in (_ingress(cf, account, t["id"]) or []) if r.get("hostname") and r.get("hostname") not in hosts]
    _set_ingress(cf, account, t["id"], [{"hostname": h, "service": f"http://localhost:{port}"} for h in hosts] + rules)
    for h in hosts:
        steps.append(f"{h} → http://localhost:{port} on this tunnel." + (" (the old name, kept for older phones and links)" if h != host else ""))
        for other in _take_off_other_tunnels(cf, account, h, t["id"]):
            steps.append(f"{h} taken off tunnel “{other}” (its other names are untouched).")
        steps.append(f"DNS for {h}: {_point_dns(cf, zone['id'], h, t['id'])}.")
    token = cf.call("GET", f"/accounts/{account}/cfd_tunnel/{t['id']}/token")
    svc = install_service(token)
    if svc["service"] == "installed":
        steps.append("cloudflared is running as a Windows service (starts with Windows).")
    else:
        steps.append("Run this on this server to start the tunnel: " + svc["command"])
    if _update_alert_links(host):
        steps.append(f"Alert links now open https://{host}.")
    store.set_setting(KEY, {"hostname": host, "tunnel_id": t["id"], "tunnel_name": t.get("name") or name, "account": account,
                            "zone": zone["id"], "set_up": time.strftime("%Y-%m-%d %H:%M"), "how": "api"})
    ok, why = check_outside(host) if svc["service"] == "installed" else (False, "not started yet")
    steps.append(f"https://{host} answers from here. ✅" if ok else
                 f"https://{host} isn't answering yet ({why}). It can take a minute; check again shortly.")
    return {"ok": True, "steps": steps, "reachable": ok}


def setup_with_token(tunnel_token, host=None):
    host = (host or hostname()).strip().lower()
    svc = install_service(tunnel_token)
    steps = []
    if svc["service"] == "installed":
        steps.append("cloudflared is running as a Windows service with that tunnel.")
    else:
        steps.append("Run this on this server: " + svc["command"])
    names = " and ".join(_with_old_names(host))
    steps.append(f"In the Cloudflare dashboard, the tunnel's public hostnames {names} must point to "
                 f"http://localhost:{config.WEB_PORT} (the dashboard updates DNS itself).")
    store.set_setting(KEY, {"hostname": host, "set_up": time.strftime("%Y-%m-%d %H:%M"), "how": "token"})
    ok, why = check_outside(host) if svc["service"] == "installed" else (False, "")
    steps.append(f"https://{host} answers from here. ✅" if ok else f"https://{host} isn't answering yet ({why}).")
    return {"ok": True, "steps": steps, "reachable": ok}
