#!/usr/bin/env python3
"""SQLite persistence for Live TV: settings, categories, channels, viewer sessions.
Stdlib only. One DB file, WAL mode, short-lived connection per call.
"""
import sqlite3, os, re, json, time, secrets
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "/etc/livetv/livetv.db"
CONFIG_JSON = "/etc/livetv/config.json"
BDT_OFFSET = 6 * 3600  # Bangladesh Standard Time, UTC+6, no DST — server runs in UTC
GAP_MAX = 40            # seconds of heartbeat silence before a session is considered ended

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'ts',
    logo TEXT DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'auto',
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS channel_categories (
    channel_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    PRIMARY KEY (channel_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_cc_category ON channel_categories(category_id);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_uid TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_channel_closed ON sessions(channel_id, closed);
CREATE INDEX IF NOT EXISTS idx_sessions_viewer ON sessions(viewer_uid);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _next_id(conn, table, prefix):
    rows = conn.execute("SELECT id FROM %s" % table).fetchall()
    pat = re.compile("^%s(\\d+)$" % re.escape(prefix))
    nums = [int(m.group(1)) for r in rows for m in [pat.match(r["id"])] if m]
    return "%s%d" % (prefix, (max(nums) + 1) if nums else 1)


# ---------- one-time migration from the old config.json ----------
def _migrate_from_json():
    if not os.path.exists(CONFIG_JSON):
        return
    with open(CONFIG_JSON) as f:
        cfg = json.load(f)
    with get_conn() as conn:
        for key in ("auth_enabled", "viewer_password", "viewer_secret",
                    "admin_password", "admin_secret", "session_hours"):
            if key not in cfg:
                continue
            val = cfg[key]
            if key == "auth_enabled":
                val = "1" if val else "0"
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, str(val)))
        for cat in cfg.get("categories", []):
            conn.execute("INSERT OR REPLACE INTO categories(id,name) VALUES (?,?)", (cat["id"], cat["name"]))
        for i, ch in enumerate(cfg.get("channels", [])):
            cats = ch.get("categories")
            if cats is None:
                cats = [ch["category"]] if ch.get("category") else []
            conn.execute("""INSERT OR REPLACE INTO channels
                             (id,name,url,type,logo,mode,enabled,sort_order,deleted_at)
                             VALUES (?,?,?,?,?,?,?,?,NULL)""",
                         (ch["id"], ch["name"], ch["url"], ch.get("type", "ts"), ch.get("logo", ""),
                          ch.get("mode", "auto"), 1 if ch.get("enabled", True) else 0, i))
            for cid in cats:
                conn.execute("INSERT OR IGNORE INTO channel_categories(channel_id,category_id) VALUES (?,?)",
                             (ch["id"], cid))
    os.replace(CONFIG_JSON, CONFIG_JSON + ".migrated.bak")


def _ensure_default_settings():
    with get_conn() as conn:
        existing = {r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()}
        for k, v in (("auth_enabled", "0"), ("session_hours", "24")):
            if k not in existing:
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (k, v))
        for k in ("viewer_password", "admin_password"):
            if k not in existing:
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (k, secrets.token_urlsafe(6)))
        for k in ("viewer_secret", "admin_secret", "stream_secret"):
            if k not in existing:
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (k, secrets.token_urlsafe(32)))


def init_db():
    is_new = not os.path.exists(DB_PATH)
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
    if is_new:
        _migrate_from_json()
    _ensure_default_settings()


# ---------- settings ----------
def get_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
    d = {r["key"]: r["value"] for r in rows}
    d["auth_enabled"] = d.get("auth_enabled") == "1"
    d["session_hours"] = int(d.get("session_hours", 24))
    return d


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


# ---------- categories ----------
def get_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT id,name FROM categories ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]


def save_category(cid, name):
    with get_conn() as conn:
        if cid:
            cur = conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
            if cur.rowcount == 0:
                return None
        else:
            cid = _next_id(conn, "categories", "cat")
            conn.execute("INSERT INTO categories(id,name) VALUES (?,?)", (cid, name))
    return cid


def delete_category(cid):
    with get_conn() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (cid,))
        conn.execute("DELETE FROM channel_categories WHERE category_id=?", (cid,))


# ---------- channels ----------
def _channel_row_to_dict(row, categories):
    return {
        "id": row["id"], "name": row["name"], "url": row["url"], "type": row["type"],
        "logo": row["logo"] or "", "mode": row["mode"],
        "enabled": bool(row["enabled"]), "categories": categories,
    }


def get_channels(include_deleted=False):
    with get_conn() as conn:
        q = "SELECT * FROM channels"
        if not include_deleted:
            q += " WHERE deleted_at IS NULL"
        q += " ORDER BY sort_order"
        rows = conn.execute(q).fetchall()
        cats_by_channel = {}
        for r in conn.execute("SELECT channel_id, category_id FROM channel_categories"):
            cats_by_channel.setdefault(r["channel_id"], []).append(r["category_id"])
    return [_channel_row_to_dict(r, cats_by_channel.get(r["id"], [])) for r in rows]


def get_channel(cid, include_deleted=True):
    with get_conn() as conn:
        q = "SELECT * FROM channels WHERE id=?"
        if not include_deleted:
            q += " AND deleted_at IS NULL"
        row = conn.execute(q, (cid,)).fetchone()
        if not row:
            return None
        cats = [r["category_id"] for r in
                conn.execute("SELECT category_id FROM channel_categories WHERE channel_id=?", (cid,))]
    return _channel_row_to_dict(row, cats)


def save_channel(cid, name, url, ctype, logo, mode, categories, enabled=None):
    """cid: existing id to edit, or falsy to create new. Returns the channel id, or None if cid not found."""
    with get_conn() as conn:
        if cid:
            row = conn.execute("SELECT enabled FROM channels WHERE id=? AND deleted_at IS NULL", (cid,)).fetchone()
            if row is None:
                return None
            new_enabled = int(bool(enabled)) if enabled is not None else row["enabled"]
            conn.execute("UPDATE channels SET name=?, url=?, type=?, logo=?, mode=?, enabled=? WHERE id=?",
                         (name, url, ctype, logo, mode, new_enabled, cid))
        else:
            maxo = conn.execute("SELECT COALESCE(MAX(sort_order),-1) AS m FROM channels").fetchone()["m"]
            cid = _next_id(conn, "channels", "ch")
            conn.execute("""INSERT INTO channels(id,name,url,type,logo,mode,enabled,sort_order,deleted_at)
                             VALUES (?,?,?,?,?,?,?,?,NULL)""",
                         (cid, name, url, ctype, logo, mode,
                          1 if enabled is None else int(bool(enabled)), maxo + 1))
        conn.execute("DELETE FROM channel_categories WHERE channel_id=?", (cid,))
        conn.executemany("INSERT INTO channel_categories(channel_id,category_id) VALUES (?,?)",
                          [(cid, c) for c in categories])
    return cid


def set_channel_enabled(cid, enabled):
    with get_conn() as conn:
        cur = conn.execute("UPDATE channels SET enabled=? WHERE id=? AND deleted_at IS NULL",
                            (1 if enabled else 0, cid))
    return cur.rowcount > 0


def soft_delete_channel(cid):
    with get_conn() as conn:
        conn.execute("UPDATE channels SET deleted_at=?, enabled=0 WHERE id=?", (int(time.time()), cid))


def reorder_channels(order):
    with get_conn() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM channels WHERE deleted_at IS NULL")}
        if set(order) != ids:
            return False
        for i, cid in enumerate(order):
            conn.execute("UPDATE channels SET sort_order=? WHERE id=?", (i, cid))
    return True


# ---------- viewer sessions / stats ----------
def record_heartbeat(vid, channel_id, ip=None, now=None):
    now = now or int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT id, channel_id, last_seen_ts FROM sessions WHERE viewer_uid=? AND closed=0",
                            (vid,)).fetchone()
        if row:
            gap = now - row["last_seen_ts"]
            if row["channel_id"] == channel_id and gap <= GAP_MAX:
                conn.execute("UPDATE sessions SET last_seen_ts=? WHERE id=?", (now, row["id"]))
                return
            conn.execute("UPDATE sessions SET closed=1 WHERE id=?", (row["id"],))
        conn.execute("INSERT INTO sessions(viewer_uid,channel_id,start_ts,last_seen_ts,closed,ip) "
                     "VALUES (?,?,?,?,0,?)", (vid, channel_id, now, now, ip))


def sweep_stale_sessions(now=None):
    now = now or int(time.time())
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET closed=1 WHERE closed=0 AND last_seen_ts < ?", (now - GAP_MAX,))


def live_stats():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.channel_id AS id, c.name AS name, COUNT(DISTINCT s.viewer_uid) AS count
            FROM sessions s JOIN channels c ON c.id = s.channel_id
            WHERE s.closed = 0
            GROUP BY s.channel_id
            ORDER BY count DESC
        """).fetchall()
    channels = [dict(r) for r in rows]
    total = sum(r["count"] for r in channels)
    return {"total": total, "channels": channels}


def _bdt_date_to_utc_ts(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) - BDT_OFFSET


def range_stats(date_from, date_to):
    """date_from/date_to: 'YYYY-MM-DD' strings, inclusive, bucketed by BDT calendar day."""
    from_ts = _bdt_date_to_utc_ts(date_from)
    to_ts = _bdt_date_to_utc_ts(date_to) + 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date(start_ts + ?, 'unixepoch') AS day, channel_id, viewer_uid,
                   SUM(last_seen_ts - start_ts) AS secs
            FROM sessions
            WHERE start_ts >= ? AND start_ts < ?
            GROUP BY day, channel_id, viewer_uid
        """, (BDT_OFFSET, from_ts, to_ts)).fetchall()
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM channels").fetchall()}
        deleted_ids = {r["id"] for r in conn.execute("SELECT id FROM channels WHERE deleted_at IS NOT NULL")}

    days = {}
    range_channels = {}
    for r in rows:
        d = days.setdefault(r["day"], {})
        c = d.setdefault(r["channel_id"], {"viewers": set(), "secs": 0})
        c["viewers"].add(r["viewer_uid"])
        c["secs"] += r["secs"] or 0
        rc = range_channels.setdefault(r["channel_id"], {"viewers": set(), "secs": 0})
        rc["viewers"].add(r["viewer_uid"])
        rc["secs"] += r["secs"] or 0

    result_days = []
    all_viewers = set()
    for day in sorted(days.keys()):
        chans = []
        day_viewers = set()
        for cid, info in days[day].items():
            day_viewers |= info["viewers"]
            all_viewers |= info["viewers"]
            chans.append({
                "id": cid, "name": names.get(cid, cid), "deleted": cid in deleted_ids,
                "unique_viewers": len(info["viewers"]), "watch_seconds": info["secs"],
            })
        chans.sort(key=lambda c: -c["unique_viewers"])
        result_days.append({"date": day, "unique_viewers": len(day_viewers), "channels": chans})

    channels_total = [{
        "id": cid, "name": names.get(cid, cid), "deleted": cid in deleted_ids,
        "unique_viewers": len(info["viewers"]), "watch_seconds": info["secs"],
    } for cid, info in range_channels.items()]
    channels_total.sort(key=lambda c: -c["unique_viewers"])

    return {"days": result_days, "total_unique_viewers": len(all_viewers), "channels": channels_total}
