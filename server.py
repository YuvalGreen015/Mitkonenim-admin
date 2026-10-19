#!/usr/bin/env python3
"""Analytics backend: three live metrics (live installs, total installs, active users).
Zero dependencies (Python 3.9+ standard library).

  python3 server.py            # http://localhost:8080  (dashboard + API)

Env: DASHBOARD_PASSWORD (REQUIRED for you to see the dashboard; if unset a random one is printed at startup),
     PORT, DB_PATH, DASH_TZ (e.g. Asia/Jerusalem), ALLOWED_ORIGIN (your app's origin, for CORS),
     COOKIE_SECURE=1 (when served over https), TRUST_PROXY=1 (behind a reverse proxy), INGEST_KEY, ACTIVE_MIN_LOGINS.
"""
from urllib.parse import parse_qs
import functools, hashlib, hmac, json, os, secrets, sqlite3, threading, time
from http.cookies import SimpleCookie
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(HERE / "analytics.db"))
TZ = ZoneInfo(os.environ.get("DASH_TZ", "UTC"))
INGEST_KEY = os.environ.get("INGEST_KEY", "")
ACTIVE_WINDOW_S = 7 * 86400   # rolling 7 days
ACTIVE_MIN_LOGINS = int(os.environ.get("ACTIVE_MIN_LOGINS", 3))   # "more than 2 logins" = 3
SKEW_S = 300                  # tolerated client clock skew into the future

clock = time.time             # replaced in tests

# ---------------------------------------------------------------- privacy: only you can see the dashboard
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
GENERATED_PASSWORD = not PASSWORD
if GENERATED_PASSWORD: PASSWORD = secrets.token_urlsafe(12)      # never run open: no password set -> random one
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "") == "1"
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")           # CORS for the two app-facing endpoints only
SESSION_DAYS = 30
INGEST_PATHS = ("/api/session", "/api/events/install")

@functools.lru_cache(maxsize=1)
def _secret():
    env = os.environ.get("SESSION_SECRET")
    if env: return env.encode()
    p = Path(DB_PATH + ".secret")
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f: f.write(secrets.token_hex(32))
    return p.read_text().strip().encode()

def make_session(now=None):
    exp = int((now or clock()) + SESSION_DAYS * 86400)
    return f"{exp}." + hmac.new(_secret(), f"v1.{exp}".encode(), "sha256").hexdigest()

def check_session(value, now=None):
    try:
        exp, sig = value.split(".", 1)
        good = hmac.new(_secret(), f"v1.{int(exp)}".encode(), "sha256").hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > (now or clock())
    except Exception:
        return False

def owner_key():
    """Permanent private key for YOUR access link. Derived from the server secret, so it survives restarts
    and needs nothing typed: open  https://your-server/?key=<this>  once and the browser stays signed in."""
    return hmac.new(_secret(), b"owner-key.v1", "sha256").hexdigest()[:32]

def password_ok(p):
    return hmac.compare_digest(hashlib.sha256(str(p).encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest())

_fail = {}; _fail_lock = threading.Lock()
def _login_blocked(ip, limit=8, window=900):
    now = clock()
    with _fail_lock:
        _fail[ip] = [t for t in _fail.get(ip, []) if now - t < window]
        return len(_fail[ip]) >= limit
def _login_failed(ip):
    with _fail_lock: _fail.setdefault(ip, []).append(clock())

# ---------------------------------------------------------------- database
def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db(db):
    script = (HERE / "schema.sqlite.sql").read_text()
    # Migration from the first version (login_events had no users table / foreign key):
    old_login = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_events'").fetchone()
    if old_login and not db.execute("PRAGMA foreign_key_list(login_events)").fetchall():
        db.executescript("DROP INDEX IF EXISTS idx_login_at_user; ALTER TABLE login_events RENAME TO login_events_old;")
        db.executescript(script)
        with db:
            db.execute("INSERT OR IGNORE INTO users(user_id,created_at,legacy) "
                       "SELECT user_id, MIN(logged_in_at), 1 FROM login_events_old GROUP BY user_id")
            db.execute("INSERT INTO login_events SELECT event_id,user_id,logged_in_at,received_at FROM login_events_old")
        db.execute("DROP TABLE login_events_old"); db.commit()
        return
    db.executescript(script); db.commit()

def day_key(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

# ---------------------------------------------------------------- ingestion
def _ts(value, now):
    """Event time from the client, clamped so a wrong device clock can't write into the future."""
    ts = int(value) if value is not None else int(now)
    return min(ts, int(now) + SKEW_S)

def record_install(db, event_id, device_id, platform=None, ts=None):
    """Returns True if this was a NEW install event (counters incremented), False if a duplicate."""
    now = clock(); at = _ts(ts, now)
    with db:  # one transaction: event row + both counters, or nothing
        cur = db.execute(
            "INSERT OR IGNORE INTO install_events(event_id,device_id,platform,installed_at,received_at) VALUES(?,?,?,?,?)",
            (event_id, device_id, platform, at, int(now)))
        if cur.rowcount != 1:
            return False
        db.execute("INSERT INTO counters(name,value) VALUES('installs_total',1) "
                   "ON CONFLICT(name) DO UPDATE SET value=value+1")
        db.execute("INSERT INTO daily_installs(day,n) VALUES(?,1) "
                   "ON CONFLICT(day) DO UPDATE SET n=n+1", (day_key(at),))
    return True

class AuthError(Exception): pass

def _hash(token): return hashlib.sha256(token.encode()).hexdigest()
_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                     # no 0/O/1/I: easy to read aloud
def _new_user_id():                                                  # short code like K7QM-9XDA (~40 bits)
    c = "".join(secrets.choice(_CODE_CHARS) for _ in range(8))
    return c[:4] + "-" + c[4:]

def _find_user(db, token):
    r = db.execute("SELECT user_id FROM users WHERE token_hash=?", (_hash(token),)).fetchone()
    return r[0] if r else None

def _create_user(db, token, now):
    """Insert a user with a server-generated ID. The PRIMARY KEY makes a duplicate ID impossible to store:
    on the (astronomically unlikely) collision we simply draw another ID. A concurrent registration of the
    same token resolves to the same user."""
    for _ in range(8):
        uid = _new_user_id()
        try:
            db.execute("INSERT INTO users(user_id,token_hash,created_at) VALUES(?,?,?)", (uid, _hash(token), int(now)))
            return uid
        except sqlite3.IntegrityError:
            other = _find_user(db, token)
            if other: return other
    raise RuntimeError("could not allocate a unique user id")

def record_login(db, event_id, user_id, ts=None):
    """Low-level: record a login for an EXISTING user. Unknown users are rejected, never silently counted."""
    now = clock()
    try:
        with db:
            cur = db.execute(
                "INSERT OR IGNORE INTO login_events(event_id,user_id,logged_in_at,received_at) VALUES(?,?,?,?)",
                (event_id, user_id, _ts(ts, now), int(now)))
    except sqlite3.IntegrityError:
        raise AuthError("unknown user")
    return cur.rowcount == 1

def create_session(db, event_id, token, user_id=None, ts=None):
    """The one call an app makes on every login.
    - first ever call on a device (unknown token): a new user + unique ID is created automatically;
    - later calls: the token identifies the user (a supplied user_id must match);
    - the login event is stored under that ID in the same transaction, and is idempotent by event_id.
    Returns (user_id, created_user, login_counted)."""
    if not isinstance(token, str) or len(token) < 32:
        raise AuthError("token must be a random string of at least 32 characters")
    now = clock(); created = False
    with db:
        uid = _find_user(db, token)
        if uid is None:
            if user_id: raise AuthError("unknown user")
            uid = _create_user(db, token, now); created = True
        elif user_id and user_id != uid:
            raise AuthError("token does not belong to this user")
        cur = db.execute(
            "INSERT OR IGNORE INTO login_events(event_id,user_id,logged_in_at,received_at) VALUES(?,?,?,?)",
            (event_id, uid, _ts(ts, now), int(now)))
    return uid, created, cur.rowcount == 1

# ---------------------------------------------------------------- the three metrics
def total_installs(db):
    r = db.execute("SELECT value FROM counters WHERE name='installs_total'").fetchone()
    return r[0] if r else 0

def live_installs(db, now=None):
    """Installs since local midnight (the counter for today's row; resets itself at midnight)."""
    r = db.execute("SELECT n FROM daily_installs WHERE day=?", (day_key(now or clock()),)).fetchone()
    return r[0] if r else 0

def active_users(db, now=None):
    """Distinct users with MORE THAN 2 login events in the rolling last 7 days.
    Window is (now-7d, now]: the lower bound is exclusive, so a login exactly 7 days ago has expired."""
    now = int(now or clock())
    r = db.execute(
        "SELECT COUNT(*) FROM (SELECT user_id FROM login_events "
        "WHERE logged_in_at > ? AND logged_in_at <= ? GROUP BY user_id HAVING COUNT(*) >= ?)",
        (now - ACTIVE_WINDOW_S, now, ACTIVE_MIN_LOGINS)).fetchone()
    return r[0]

def logged_in_7d(db, now=None):
    """Distinct users with at least ONE login in the last 7 days (for comparison with 'active users')."""
    now = int(now or clock())
    return db.execute("SELECT COUNT(DISTINCT user_id) FROM login_events WHERE logged_in_at > ? AND logged_in_at <= ?",
                      (now - ACTIVE_WINDOW_S, now)).fetchone()[0]

def registered_users(db): return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def installs_by_day(db, days=14, now=None):
    now = now or clock()
    today = datetime.fromtimestamp(now, TZ).date()
    keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    rows = dict(db.execute("SELECT day,n FROM daily_installs WHERE day>=?", (keys[0],)).fetchall())
    return [{"day": k, "n": rows.get(k, 0)} for k in keys]

def metrics(db, now=None):
    now = now or clock()
    return {"live_installs": live_installs(db, now), "total_installs": total_installs(db),
            "active_users": active_users(db, now), "logged_in_7d": logged_in_7d(db, now),
            "registered_users": registered_users(db), "active_min_logins": ACTIVE_MIN_LOGINS, "installs_by_day": installs_by_day(db, 14, now),
            "tz": str(TZ), "generated_at": int(now)}

def rebuild_counters(db):
    """Repair tool: recompute counters from the event log (use if a counter is ever suspected to drift)."""
    with db:
        db.execute("DELETE FROM counters WHERE name='installs_total'")
        db.execute("INSERT INTO counters SELECT 'installs_total', COUNT(*) FROM install_events")
        db.execute("DELETE FROM daily_installs")
        for (at,) in db.execute("SELECT installed_at FROM install_events").fetchall():
            db.execute("INSERT INTO daily_installs(day,n) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET n=n+1", (day_key(at),))

# ---------------------------------------------------------------- live push (Server-Sent Events)
class Bus:
    def __init__(self): self.cv = threading.Condition(); self.version = 0
    def bump(self):
        with self.cv: self.version += 1; self.cv.notify_all()
    def wait(self, seen, timeout):
        with self.cv:
            self.cv.wait_for(lambda: self.version != seen, timeout)
            return self.version
bus = Bus()

# ---------------------------------------------------------------- HTTP
def _clean(v, n=128):
    return v.strip() if isinstance(v, str) and 0 < len(v.strip()) <= n else None

class Handler(BaseHTTPRequestHandler):
    server_version = "analytics/1"
    def log_message(self, *a): pass

    def _ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd: return fwd.split(",")[-1].strip()
        return self.client_address[0]

    def _authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "an_sess" in c and check_session(c["an_sess"].value)

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.path.split("?")[0] in INGEST_PATHS:      # CORS only for what your app calls, never for the dashboard
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        for k, val in (extra or {}).items(): self.send_header(k, val)
        self.end_headers(); self.wfile.write(data)

    def do_OPTIONS(self):                                 # CORS preflight for the app-facing endpoints
        if self.path.split("?")[0] not in INGEST_PATHS: return self._send(404, {"error": "not found"})
        self.send_response(204)
        for k, val in (("Access-Control-Allow-Origin", ALLOWED_ORIGIN), ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                       ("Access-Control-Allow-Headers", "content-type, x-api-key"), ("Access-Control-Max-Age", "86400")):
            self.send_header(k, val)
        self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            qs = parse_qs(self.path.partition("?")[2])
            if "key" in qs:                                   # your private link: sign in automatically, then drop the key from the URL
                ip = self._ip()
                if _login_blocked(ip): return self._send(429, {"error": "too many attempts, try again later"})
                if hmac.compare_digest(qs["key"][0], owner_key()):
                    ck = f"an_sess={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS*86400}" + ("; Secure" if COOKIE_SECURE else "")
                    return self._send(302, b"", "text/plain", extra={"Set-Cookie": ck, "Location": "/"})
                _login_failed(ip)
            page = "dashboard.html" if self._authed() else "login.html"
            return self._send(200, (HERE / page).read_bytes(), "text/html; charset=utf-8")
        if path in ("/api/metrics", "/api/stream") and not self._authed():
            return self._send(401, {"error": "login required"})
        if path == "/api/metrics":
            db = connect(); 
            try: return self._send(200, metrics(db))
            finally: db.close()
        if path == "/api/stream":
            return self._stream()
        self._send(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "keep-alive"); self.end_headers()
        try:
            while True:
                seen = bus.version          # read BEFORE computing, so an event arriving meanwhile is not missed
                db = connect()
                try: payload = json.dumps(metrics(db))
                finally: db.close()
                self.wfile.write(f"data: {payload}\n\n".encode()); self.wfile.flush()
                # push immediately on any new event; otherwise refresh every 10 s
                # (the rolling 7-day active-user number changes with time alone)
                bus.wait(seen, 10)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8192: return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict): raise ValueError
        except Exception:
            return self._send(400, {"error": "invalid json"})
        if self.path == "/api/login":
            ip = self._ip()
            if _login_blocked(ip): return self._send(429, {"error": "too many attempts, try again in 15 minutes"})
            if not password_ok(body.get("password", "")):
                _login_failed(ip); return self._send(401, {"error": "wrong password"})
            ck = f"an_sess={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS*86400}" + ("; Secure" if COOKIE_SECURE else "")
            return self._send(200, {"ok": True}, extra={"Set-Cookie": ck})
        if self.path == "/api/logout":
            return self._send(200, {"ok": True}, extra={"Set-Cookie": "an_sess=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
        ts = body.get("ts")
        if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float))): return self._send(400, {"error": "ts must be unix seconds"})
        eid = _clean(body.get("event_id"))
        if not eid: return self._send(400, {"error": "event_id required"})
        db = connect()
        try:
            if self.path == "/api/events/install":
                if INGEST_KEY and self.headers.get("X-API-Key") != INGEST_KEY:
                    return self._send(401, {"error": "bad api key"})
                dev = _clean(body.get("device_id"))
                if not dev: return self._send(400, {"error": "device_id required"})
                new = record_install(db, eid, dev, _clean(body.get("platform"), 32), ts)
                if new: bus.bump()
                return self._send(200, {"accepted": new, "duplicate": not new})
            if self.path == "/api/session":
                token = body.get("token")
                if not isinstance(token, str): return self._send(400, {"error": "token required"})
                if _unknown_token(db, token) and not _reg_allowed(self._ip()):
                    return self._send(429, {"error": "too many new registrations from this address"})
                try:
                    uid, created, counted = create_session(db, eid, token, _clean(body.get("user_id")), ts)
                except AuthError as e:
                    return self._send(401, {"error": str(e)})
                if counted or created: bus.bump()
                return self._send(200, {"user_id": uid, "new_user": created, "login_counted": counted})
            return self._send(404, {"error": "not found"})
        finally:
            db.close()

_reg_hits = {}; _reg_lock = threading.Lock()
def _unknown_token(db, token): return len(token) >= 32 and _find_user(db, token) is None
def _reg_allowed(ip, limit=60, window=3600):
    """Small in-memory brake on mass registration from a single address."""
    now = clock()
    with _reg_lock:
        hits = [t for t in _reg_hits.get(ip, []) if now - t < window]
        if len(hits) >= limit: _reg_hits[ip] = hits; return False
        hits.append(now); _reg_hits[ip] = hits; return True

def main():
    db = connect(); init_db(db); db.close()
    port = int(os.environ.get("PORT", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler); srv.daemon_threads = True
    print(f"dashboard: http://localhost:{port}   tz={TZ}   db={DB_PATH}")
    link = f"http://localhost:{port}/?key={owner_key()}"
    try: (Path(DB_PATH + ".owner-link.txt")).write_text(link + "\n"); os.chmod(Path(DB_PATH + ".owner-link.txt"), 0o600)
    except OSError: pass
    print(f"YOUR PRIVATE LINK (open once on your phone, nothing to type; replace localhost with your server address):\n   {link}")
    if GENERATED_PASSWORD:
        print(f"!! DASHBOARD_PASSWORD not set. Temporary password for this run: {PASSWORD}\n   Set DASHBOARD_PASSWORD to keep one that survives restarts.")
    elif len(PASSWORD) < 10: print("!! warning: use a DASHBOARD_PASSWORD of at least 10 characters")
    srv.serve_forever()

if __name__ == "__main__":
    main()
