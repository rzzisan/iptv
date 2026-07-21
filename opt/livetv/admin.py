#!/usr/bin/env python3
"""Live TV admin/control backend. Stdlib only. Binds 127.0.0.1:8088 (nginx proxies).
Persistence lives in db.py (SQLite); this module is pure HTTP routing + systemd/nginx glue.
"""
import json, os, re, hmac, hashlib, time, subprocess, threading, ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db

CHAN_DIR = "/etc/livetv/channels"
HLS_ROOT = "/var/www/hls"
LOGO_DIR = "/var/www/logos"
AUTH_JS = "/etc/nginx/njs/auth.js"
ACL_CONF = "/etc/nginx/conf.d/livetv-acl.conf"
CONFIG_LOCK = threading.Lock()   # guards settings/category/channel mutations
STATS_LOCK = threading.Lock()    # guards viewer session writes (kept separate: heartbeats
                                  # must never queue behind a slow channel save/systemctl call)

SAFE_ID = re.compile(r"^ch\d+$")
SAFE_CAT_ID = re.compile(r"^cat\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOGO_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")

STREAM_TOKEN_TTL = 90  # seconds; short-lived so a leaked /hls/ URL stops working almost immediately

# ---------- signed cookies (matches njs HMAC-SHA256) ----------
def sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def make_token(secret, hours):
    exp = str(int(time.time()) + int(hours) * 3600)
    return exp + "." + sign(secret, exp)

def make_token_seconds(secret, seconds):
    exp = str(int(time.time()) + int(seconds))
    return exp + "." + sign(secret, exp)

def verify_token(secret, token):
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    if sign(secret, payload) != sig:
        return False
    try:
        return time.time() <= int(payload)
    except ValueError:
        return False

# ---------- nginx njs auth.js generation ----------
def write_auth_js(settings):
    enabled = 1 if settings.get("auth_enabled") else 0
    secret = settings["viewer_secret"].replace("'", "")
    stream_secret = settings["stream_secret"].replace("'", "")
    js = """import crypto from 'crypto';
var AUTH_ENABLED = %d;
var SECRET = '%s';
var STREAM_SECRET = '%s';
function sign(secret,v){var h=crypto.createHmac('sha256',secret);h.update(v);return h.digest('hex');}
function verify(secret,t){if(!t)return false;var d=t.indexOf('.');if(d<1)return false;var p=t.substring(0,d),s=t.substring(d+1);if(sign(secret,p)!==s)return false;var e=Number(p);if(!e||(Date.now()/1000)>e)return false;return true;}
function validate(r){ if(!AUTH_ENABLED) return '1'; return verify(SECRET,r.variables.cookie_session)?'1':'0'; }
function validateStream(r){ return verify(STREAM_SECRET,r.variables.cookie_stream_token)?'1':'0'; }
export default { validate, validateStream };
""" % (enabled, secret, stream_secret)
    tmp = AUTH_JS + ".tmp"
    with open(tmp, "w") as f:
        f.write(js)
    os.replace(tmp, AUTH_JS)
    os.chmod(AUTH_JS, 0o600)
    subprocess.run(["nginx", "-s", "reload"], capture_output=True)

# ---------- nginx IP ACL (geo map) generation ----------
def normalize_cidr(raw):
    """Returns the canonical 'a.b.c.d/n' form, or None if raw isn't a valid IPv4/IPv6 network."""
    try:
        return str(ipaddress.ip_network(str(raw).strip(), strict=False))
    except ValueError:
        return None

def write_acl_conf(enabled, prefixes):
    lines = ["geo $livetv_acl_allowed {"]
    if enabled:
        lines.append("    default 0;")
        for p in prefixes:
            lines.append("    %s 1;" % p["cidr"])
    else:
        lines.append("    default 1;")
    lines.append("}\n")
    tmp = ACL_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, ACL_CONF)
    # config is admin-entered CIDRs; verify before reloading a live streaming server
    if sh("nginx", "-t").returncode == 0:
        subprocess.run(["nginx", "-s", "reload"], capture_output=True)

# ---------- channel / systemd management ----------
def sh(*args):
    return subprocess.run(args, capture_output=True, text=True)

def write_channel_env(ch):
    path = os.path.join(CHAN_DIR, ch["id"] + ".env")
    lines = [
        "CHANNEL_URL=%s" % ch["url"],
        "CHANNEL_TYPE=%s" % ch.get("type", "ts"),
        "MODE=%s" % ch.get("mode", "auto"),
    ]
    if ch.get("res"):
        lines.append("RES=%s" % ch["res"])
    if ch.get("vbitrate"):
        lines.append("VBITRATE=%s" % ch["vbitrate"])
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

def apply_channel(ch):
    """Write env, ensure hls dir, (re)start the channel service."""
    write_channel_env(ch)
    d = os.path.join(HLS_ROOT, ch["id"])
    os.makedirs(d, exist_ok=True)
    sh("chown", "www-data:www-data", d)
    unit = "livetv-channel@%s.service" % ch["id"]
    if ch.get("enabled", True):
        sh("systemctl", "enable", unit)
        sh("systemctl", "restart", unit)
    else:
        sh("systemctl", "disable", "--now", unit)

def remove_channel(cid):
    unit = "livetv-channel@%s.service" % cid
    sh("systemctl", "disable", "--now", unit)
    try: os.remove(os.path.join(CHAN_DIR, cid + ".env"))
    except OSError: pass
    sh("rm", "-rf", os.path.join(HLS_ROOT, cid))

def list_logos():
    try:
        files = [f for f in os.listdir(LOGO_DIR) if os.path.splitext(f)[1].lower() in LOGO_EXT]
    except OSError:
        return []
    files.sort(key=lambda f: os.path.getmtime(os.path.join(LOGO_DIR, f)), reverse=True)
    return ["/logos/" + f for f in files]

def _today_bdt():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + db.BDT_OFFSET))

# ---------- HTTP handler ----------
class H(BaseHTTPRequestHandler):
    server_version = "livetv/1.0"
    def log_message(self, *a): pass

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        try: return json.loads(self._body() or b"{}")
        except Exception: return {}

    def _send(self, code, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if headers:
            for k, v in headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _is_admin(self, settings):
        return verify_token(settings["admin_secret"], self._cookies().get("admin_session"))

    # ---- routing ----
    def do_GET(self):
        p = urlparse(self.path).path
        settings = db.get_settings()
        if p == "/api/channels":
            chans = [{"id": c["id"], "name": c["name"], "logo": c["logo"], "categories": c["categories"]}
                     for c in db.get_channels() if c["enabled"]]
            return self._send(200, {"auth_enabled": settings["auth_enabled"], "channels": chans,
                                     "categories": db.get_categories()})
        if p == "/api/streamtoken":
            tok = make_token_seconds(settings["stream_secret"], STREAM_TOKEN_TTL)
            ck = "stream_token=%s; Path=/hls/; Max-Age=%d; HttpOnly; SameSite=Lax" % (tok, STREAM_TOKEN_TTL)
            return self._send(200, {"ok": True, "ttl": STREAM_TOKEN_TTL}, [("Set-Cookie", ck)])
        if p == "/api/logout":
            return self._send(200, {"ok": True},
                              [("Set-Cookie", "session=; Path=/; Max-Age=0")])
        if p == "/api/admin/logout":
            return self._send(200, {"ok": True},
                              [("Set-Cookie", "admin_session=; Path=/; Max-Age=0")])
        if p == "/api/admin/state":
            if not self._is_admin(settings):
                return self._send(401, {"error": "unauthorized"})
            channels = db.get_channels()
            return self._send(200, {
                "auth_enabled": settings["auth_enabled"],
                "viewer_password": settings["viewer_password"],
                "channels": channels,
                "categories": db.get_categories(),
                "logos": list_logos(),
                "statuses": {c["id"]: self._status(c["id"]) for c in channels},
                "acl_enabled": settings["acl_enabled"],
                "acl_prefixes": db.get_acl_prefixes(),
            })
        if p == "/api/admin/stats/live":
            if not self._is_admin(settings):
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, db.live_stats())
        if p == "/api/admin/stats/range":
            if not self._is_admin(settings):
                return self._send(401, {"error": "unauthorized"})
            q = parse_qs(urlparse(self.path).query)
            today = _today_bdt()
            date_from = q.get("from", [today])[0]
            date_to = q.get("to", [today])[0]
            if not (DATE_RE.match(date_from) and DATE_RE.match(date_to)):
                return self._send(400, {"error": "bad date"})
            return self._send(200, db.range_stats(date_from, date_to))
        return self._send(404, {"error": "not found"})

    def _status(self, cid):
        r = sh("systemctl", "is-active", "livetv-channel@%s.service" % cid)
        return r.stdout.strip() or "unknown"

    def do_POST(self):
        p = urlparse(self.path).path
        settings = db.get_settings()
        # ---- viewer login ----
        if p == "/api/login":
            if not settings["auth_enabled"]:
                return self._send(200, {"ok": True})
            pw = self._json_body().get("password", "")
            if pw == settings["viewer_password"]:
                tok = make_token(settings["viewer_secret"], settings["session_hours"])
                ck = "session=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                    tok, int(settings["session_hours"]) * 3600)
                return self._send(200, {"ok": True}, [("Set-Cookie", ck)])
            return self._send(401, {"error": "wrong password"})
        # ---- admin login ----
        if p == "/api/admin/login":
            pw = self._json_body().get("password", "")
            if pw == settings["admin_password"]:
                tok = make_token(settings["admin_secret"], settings["session_hours"])
                ck = "admin_session=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                    tok, int(settings["session_hours"]) * 3600)
                return self._send(200, {"ok": True}, [("Set-Cookie", ck)])
            return self._send(401, {"error": "wrong password"})
        # ---- viewer heartbeat (public) ----
        if p == "/api/heartbeat":
            b = self._json_body()
            vid = str(b.get("vid", "")).strip()[:64]
            channel = str(b.get("channel", "")).strip()
            if not vid or not SAFE_ID.match(channel):
                return self._send(400, {"error": "bad heartbeat"})
            with STATS_LOCK:
                db.record_heartbeat(vid, channel, ip=self.client_address[0])
            return self._send(200, {"ok": True})
        # ---- everything below requires admin ----
        if not self._is_admin(settings):
            return self._send(401, {"error": "unauthorized"})

        if p == "/api/admin/channel":
            return self._channel_save()
        if p == "/api/admin/channel/toggle":
            return self._channel_toggle()
        if p == "/api/admin/category":
            return self._category_save()
        if p == "/api/admin/category/delete":
            cid = self._json_body().get("id", "")
            with CONFIG_LOCK:
                db.delete_category(cid)
            return self._send(200, {"ok": True})
        if p == "/api/admin/category/reorder":
            order = self._json_body().get("order", [])
            with CONFIG_LOCK:
                ok = db.reorder_categories(order)
            if not ok:
                return self._send(400, {"error": "order must list all category ids exactly once"})
            return self._send(200, {"ok": True})
        if p == "/api/admin/channel/delete":
            cid = self._json_body().get("id", "")
            with CONFIG_LOCK:
                db.soft_delete_channel(cid)
                remove_channel(cid)
            return self._send(200, {"ok": True})
        if p == "/api/admin/channel/reorder":
            order = self._json_body().get("order", [])
            with CONFIG_LOCK:
                ok = db.reorder_channels(order)
            if not ok:
                return self._send(400, {"error": "order must list all channel ids exactly once"})
            return self._send(200, {"ok": True})
        if p == "/api/admin/auth":
            enabled = bool(self._json_body().get("auth_enabled"))
            with CONFIG_LOCK:
                db.set_setting("auth_enabled", "1" if enabled else "0")
                write_auth_js(db.get_settings())
            return self._send(200, {"ok": True, "auth_enabled": enabled})
        if p == "/api/admin/acl":
            enabled = bool(self._json_body().get("acl_enabled"))
            with CONFIG_LOCK:
                db.set_setting("acl_enabled", "1" if enabled else "0")
                write_acl_conf(enabled, db.get_acl_prefixes())
            return self._send(200, {"ok": True, "acl_enabled": enabled})
        if p == "/api/admin/acl/prefix":
            b = self._json_body()
            cidr = normalize_cidr(b.get("cidr", ""))
            if not cidr:
                return self._send(400, {"error": "invalid IP/CIDR"})
            note = str(b.get("note", "")).strip()[:200]
            with CONFIG_LOCK:
                new_id = db.add_acl_prefix(cidr, note)
                if new_id is None:
                    return self._send(400, {"error": "this prefix is already in the list"})
                write_acl_conf(db.get_settings()["acl_enabled"], db.get_acl_prefixes())
            return self._send(200, {"ok": True, "id": new_id, "cidr": cidr})
        if p == "/api/admin/acl/prefix/delete":
            aid = self._json_body().get("id")
            with CONFIG_LOCK:
                db.delete_acl_prefix(aid)
                write_acl_conf(db.get_settings()["acl_enabled"], db.get_acl_prefixes())
            return self._send(200, {"ok": True})
        if p == "/api/admin/password":
            np = str(self._json_body().get("viewer_password", "")).strip()
            if not np:
                return self._send(400, {"error": "empty"})
            with CONFIG_LOCK:
                db.set_setting("viewer_password", np)
            return self._send(200, {"ok": True})
        if p == "/api/admin/adminpassword":
            np = str(self._json_body().get("admin_password", "")).strip()
            if len(np) < 4:
                return self._send(400, {"error": "too short"})
            with CONFIG_LOCK:
                db.set_setting("admin_password", np)
            return self._send(200, {"ok": True})
        if p == "/api/admin/logo":
            return self._logo()
        return self._send(404, {"error": "not found"})

    def _channel_save(self):
        b = self._json_body()
        name = str(b.get("name", "")).strip()
        url = str(b.get("url", "")).strip()
        ctype = str(b.get("type", "ts")).strip().lower()
        logo = str(b.get("logo", "")).strip()
        cid = str(b.get("id", "")).strip()
        mode = str(b.get("mode", "auto")).strip().lower()
        raw_cats = b.get("categories", [])
        if not isinstance(raw_cats, list):
            raw_cats = []
        if mode not in ("auto", "copy", "transcode"):
            mode = "auto"
        if not name or not url:
            return self._send(400, {"error": "name and url required"})
        if not re.match(r"^(https?|rtsp|rtmp)://", url):
            return self._send(400, {"error": "url must start with http/rtsp/rtmp"})
        if ctype not in ("ts", "hls", "rtsp"):
            ctype = "ts"
        if cid and not SAFE_ID.match(cid):
            return self._send(400, {"error": "bad id"})
        valid_cats = {c["id"] for c in db.get_categories()}
        categories = []
        for x in raw_cats:
            x = str(x).strip()
            if x not in valid_cats:
                return self._send(400, {"error": "no such category"})
            if x not in categories:
                categories.append(x)
        with CONFIG_LOCK:
            new_id = db.save_channel(cid or None, name, url, ctype, logo, mode, categories)
            if new_id is None:
                return self._send(404, {"error": "no such channel"})
            ch = db.get_channel(new_id)
            apply_channel(ch)
        return self._send(200, {"ok": True, "id": new_id})

    def _channel_toggle(self):
        b = self._json_body()
        cid = str(b.get("id", "")).strip()
        enabled = bool(b.get("enabled"))
        if not SAFE_ID.match(cid):
            return self._send(400, {"error": "bad id"})
        with CONFIG_LOCK:
            ok = db.set_channel_enabled(cid, enabled)
            if not ok:
                return self._send(404, {"error": "no such channel"})
            ch = db.get_channel(cid)
            apply_channel(ch)
        return self._send(200, {"ok": True, "enabled": enabled})

    def _category_save(self):
        b = self._json_body()
        name = str(b.get("name", "")).strip()
        cid = str(b.get("id", "")).strip()
        if not name:
            return self._send(400, {"error": "name required"})
        if cid and not SAFE_CAT_ID.match(cid):
            return self._send(400, {"error": "bad id"})
        with CONFIG_LOCK:
            new_id = db.save_category(cid or None, name)
        if new_id is None:
            return self._send(404, {"error": "no such category"})
        return self._send(200, {"ok": True, "id": new_id})

    def _logo(self):
        q = parse_qs(urlparse(self.path).query)
        fn = (q.get("filename", ["logo"])[0]).lower()
        ext = os.path.splitext(fn)[1]
        if ext not in LOGO_EXT:
            return self._send(400, {"error": "image only (png/jpg/webp/gif/svg)"})
        data = self._body()
        if len(data) > 3 * 1024 * 1024:
            return self._send(400, {"error": "too large (max 3MB)"})
        safe = "logo_%d%s" % (int(time.time() * 1000), ext)
        path = os.path.join(LOGO_DIR, safe)
        with open(path, "wb") as f:
            f.write(data)
        sh("chown", "www-data:www-data", path)
        return self._send(200, {"ok": True, "logo": "/logos/" + safe})


def _sweep_loop():
    while True:
        time.sleep(20)
        try:
            with STATS_LOCK:
                db.sweep_stale_sessions()
        except Exception:
            pass


def startup():
    db.init_db()
    os.makedirs(CHAN_DIR, exist_ok=True)
    for ch in db.get_channels():
        write_channel_env(ch)
    settings = db.get_settings()
    write_auth_js(settings)
    write_acl_conf(settings["acl_enabled"], db.get_acl_prefixes())

if __name__ == "__main__":
    startup()
    threading.Thread(target=_sweep_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8088), H).serve_forever()
