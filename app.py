import os, re, json, hmac, hashlib, sqlite3, secrets, time, threading, csv, io
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import (Flask, request, jsonify, render_template, redirect, url_for,
                   session, flash, abort, send_file, Response, g)

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
MEDIA_DIR = os.path.join(DATA_DIR, "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(DATA_DIR, "dmvora.sqlite3"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "")
if len(app.secret_key) < 32:
    raise RuntimeError("Set a random SECRET_KEY (at least 32 characters) in .env")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@dmvora.local").strip().lower()
META_APP_ID = os.getenv("META_APP_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_ES_CONFIG_ID = os.getenv("META_ES_CONFIG_ID", "").strip()
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0").strip()
META_ES_SYNC_HISTORY = os.getenv("META_ES_SYNC_HISTORY", "0") == "1"
HUMAN_PAUSE_MIN = int(os.getenv("HUMAN_PAUSE_MIN", "15"))   # automations stay quiet this long after the owner replies from the phone app
META_GRAPH = "https://graph.facebook.com"
PRAVAH_WEBHOOK_SECRET = os.getenv("PRAVAH_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://dmvora.sanjivanitechno.com").rstrip("/")
WEBHOOK_MAX_SKEW = int(os.getenv("WEBHOOK_MAX_SKEW", "300"))
RZP_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RZP_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RZP_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()

LIMIT_KEYS = ("max_numbers", "max_members", "max_automations", "max_forms")
MEMBER_PERMS = ("automations", "templates", "forms")
LEAD_STATUSES = ("new", "contacted", "qualified", "won", "lost")
MEDIA_KINDS = {"jpg": "image", "jpeg": "image", "png": "image", "mp4": "video", "3gp": "video",
               "pdf": "document", "doc": "document", "docx": "document", "xls": "document",
               "xlsx": "document", "ppt": "document", "pptx": "document", "txt": "document", "csv": "document"}
MEDIA_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "mp4": "video/mp4", "3gp": "video/3gpp",
              "pdf": "application/pdf", "doc": "application/msword",
              "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "xls": "application/vnd.ms-excel",
              "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              "ppt": "application/vnd.ms-powerpoint",
              "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
              "txt": "text/plain", "csv": "text/csv"}
MEDIA_MAX_MB = {"image": 5, "video": 16, "document": 16}


# ───────────────────────── database ─────────────────────────
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def add_col(c, table, col, ddl):
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, name TEXT NOT NULL,
          username TEXT, email TEXT UNIQUE NOT NULL, phone TEXT DEFAULT '', password_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', owner_id INTEGER, plan_id INTEGER, plan_expires_at TEXT,
          permissions_json TEXT DEFAULT '{}', business TEXT DEFAULT '', created_by INTEGER,
          created_at TEXT NOT NULL, last_login TEXT);
        CREATE TABLE IF NOT EXISTS plans(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
          price REAL NOT NULL DEFAULT 0, duration_days INTEGER NOT NULL DEFAULT 30,
          max_numbers INTEGER DEFAULT 0, max_members INTEGER DEFAULT 0,
          max_automations INTEGER DEFAULT 0, max_forms INTEGER DEFAULT 0,
          features_json TEXT DEFAULT '[]', active INTEGER DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
          amount REAL NOT NULL, currency TEXT DEFAULT 'INR', rzp_order_id TEXT UNIQUE, rzp_payment_id TEXT,
          status TEXT DEFAULT 'created', created_at TEXT NOT NULL, paid_at TEXT);
        CREATE TABLE IF NOT EXISTS numbers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, phone_number_id TEXT UNIQUE NOT NULL,
          waba_id TEXT NOT NULL, business_id TEXT DEFAULT '', display_phone TEXT DEFAULT '',
          verified_name TEXT DEFAULT '', quality TEXT DEFAULT '', access_token TEXT NOT NULL,
          api_version TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL,
          owner_id INTEGER, coexistence INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS member_numbers(
          user_id INTEGER NOT NULL, number_id INTEGER NOT NULL, PRIMARY KEY(user_id, number_id),
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          phone TEXT NOT NULL, name TEXT DEFAULT '', email TEXT DEFAULT '',
          status TEXT DEFAULT 'new', source TEXT DEFAULT 'whatsapp',
          form_data TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          human_replied_at TEXT, notes TEXT DEFAULT '',
          UNIQUE(number_id, phone), FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL, lead_id INTEGER,
          wa_message_id TEXT UNIQUE, direction TEXT NOT NULL, message_type TEXT DEFAULT 'text',
          body TEXT DEFAULT '', media_id TEXT DEFAULT '', media_url TEXT DEFAULT '',
          status TEXT DEFAULT 'received', raw_json TEXT DEFAULT '{}', created_at TEXT NOT NULL,
          from_app INTEGER DEFAULT 0,
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE,
          FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(number_id, lead_id, id);
        CREATE TABLE IF NOT EXISTS automations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          name TEXT NOT NULL, trigger_type TEXT NOT NULL, keyword TEXT DEFAULT '',
          enabled INTEGER DEFAULT 1, graph_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, match_type TEXT DEFAULT 'contains',
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS forms(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          fields_json TEXT NOT NULL DEFAULT '[]', public_token TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL, meta_flow_id TEXT NOT NULL DEFAULT '',
          first_screen_id TEXT NOT NULL DEFAULT 'WELCOME', cta_text TEXT NOT NULL DEFAULT 'Open form',
          prompt TEXT NOT NULL DEFAULT 'Please complete this form.', number_id INTEGER,
          status TEXT NOT NULL DEFAULT 'LOCAL', category TEXT NOT NULL DEFAULT 'LEAD_GENERATION',
          screen_title TEXT NOT NULL DEFAULT 'A few details', owner_id INTEGER);
        CREATE TABLE IF NOT EXISTS templates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          meta_name TEXT NOT NULL, language TEXT DEFAULT 'en_US', category TEXT DEFAULT '',
          status TEXT DEFAULT 'UNKNOWN', components_json TEXT DEFAULT '[]',
          header_format TEXT DEFAULT '', header_media_id INTEGER,
          UNIQUE(number_id, meta_name, language),
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS media(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, token TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL, filename TEXT NOT NULL, stored_name TEXT NOT NULL, mime TEXT NOT NULL,
          size INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS event_dedup(event_key TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        """)
        # upgrade older installs in place
        for table, col, ddl in (
            ("numbers", "owner_id", "INTEGER"), ("numbers", "coexistence", "INTEGER DEFAULT 0"),
            ("leads", "human_replied_at", "TEXT"), ("leads", "notes", "TEXT DEFAULT ''"),
            ("messages", "from_app", "INTEGER DEFAULT 0"),
            ("automations", "match_type", "TEXT DEFAULT 'contains'"),
            ("templates", "header_format", "TEXT DEFAULT ''"), ("templates", "header_media_id", "INTEGER"),
            ("forms", "meta_flow_id", "TEXT NOT NULL DEFAULT ''"),
            ("forms", "first_screen_id", "TEXT NOT NULL DEFAULT 'WELCOME'"),
            ("forms", "cta_text", "TEXT NOT NULL DEFAULT 'Open form'"),
            ("forms", "prompt", "TEXT NOT NULL DEFAULT 'Please complete this form.'"),
            ("forms", "number_id", "INTEGER"), ("forms", "status", "TEXT NOT NULL DEFAULT 'LOCAL'"),
            ("forms", "category", "TEXT NOT NULL DEFAULT 'LEAD_GENERATION'"),
            ("forms", "screen_title", "TEXT NOT NULL DEFAULT 'A few details'"), ("forms", "owner_id", "INTEGER"),
        ):
            add_col(c, table, col, ddl)
        if not c.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            for name, desc, price, days, nn, mm, aa, ff, feats in (
                ("Starter", "For one business number", 999, 30, 1, 2, 5, 3, ["1 WhatsApp number", "2 team members", "5 automations", "3 forms"]),
                ("Growth", "For growing teams", 2499, 30, 3, 10, 25, 15, ["3 WhatsApp numbers", "10 team members", "25 automations", "15 forms"]),
                ("Scale", "Unlimited everything", 5999, 30, 0, 0, 0, 0, ["Unlimited numbers", "Unlimited team", "Unlimited automations", "Unlimited forms"]),
            ):
                c.execute("""INSERT INTO plans(name,description,price,duration_days,max_numbers,max_members,
                             max_automations,max_forms,features_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (name, desc, price, days, nn, mm, aa, ff, json.dumps(feats), now_iso()))


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def parse_iso(s):
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def digits(v):
    return re.sub(r"\D", "", str(v or ""))


def ensure_admin():
    if not ADMIN_PASSWORD:
        return
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if not row:
            cur = c.execute("""INSERT INTO users(role,name,username,email,phone,password_hash,status,created_at)
                               VALUES('admin','Root admin',?,?,'',?,'active',?)""",
                            (ADMIN_USERNAME, ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), now_iso()))
            aid = cur.lastrowid
            c.execute("UPDATE users SET owner_id=id WHERE id=?", (aid,))
        else:
            aid = row["id"]
            if not check_password_hash(row["password_hash"], ADMIN_PASSWORD):
                c.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(ADMIN_PASSWORD), aid))
            if row["username"] != ADMIN_USERNAME:
                c.execute("UPDATE users SET username=? WHERE id=?", (ADMIN_USERNAME, aid))
        # data created before multi-tenant support belongs to the root admin
        c.execute("UPDATE numbers SET owner_id=? WHERE owner_id IS NULL", (aid,))
        c.execute("""UPDATE forms SET owner_id=COALESCE((SELECT owner_id FROM numbers WHERE numbers.id=forms.number_id),?)
                     WHERE owner_id IS NULL""", (aid,))


init_db()
ensure_admin()


# ───────────────────────── security helpers ─────────────────────────
_hits = {}


def rate_ok(key, limit, window):
    now = time.time()
    lst = [t for t in _hits.get(key, []) if now - t < window]
    if len(lst) >= limit:
        _hits[key] = lst
        return False
    lst.append(now)
    _hits[key] = lst
    return True


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.context_processor
def inject_csrf():
    return {"csrf_token": csrf_token()}


@app.before_request
def csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "x")):
            return jsonify(error="Session expired. Please refresh the page."), 403


@app.after_request
def secure_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def bad(msg, code=400, **extra):
    return jsonify(error=msg, **extra), code


# ───────────────────────── users, roles, plans ─────────────────────────
def current_user():
    if "user" in g:
        return g.user
    g.user = None
    uid = session.get("uid")
    if uid:
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if row and row["status"] == "active":
                if row["role"] == "member":
                    owner = c.execute("SELECT status FROM users WHERE id=?", (row["owner_id"],)).fetchone()
                    if not owner or owner["status"] != "active":
                        row = None
                g.user = dict(row) if row else None
    return g.user


def tenant_id(u=None):
    u = u or current_user()
    return u["owner_id"] if u["role"] == "member" else u["id"]


def member_perms(u):
    if u["role"] != "member":
        return {k: True for k in MEMBER_PERMS}
    try:
        p = json.loads(u.get("permissions_json") or "{}")
    except ValueError:
        p = {}
    return {k: bool(p.get(k)) for k in MEMBER_PERMS}


def plan_info(tid):
    with db() as c:
        o = c.execute("SELECT * FROM users WHERE id=?", (tid,)).fetchone()
        p = c.execute("SELECT * FROM plans WHERE id=?", (o["plan_id"],)).fetchone() if o and o["plan_id"] else None
    if not o:
        return {"active": False, "name": "", "limits": {k: 0 for k in LIMIT_KEYS}, "unlimited": False}
    if o["role"] == "admin":
        return {"active": True, "unlimited": True, "name": "Root admin", "plan_id": None, "expires_at": None,
                "days_left": None, "limits": {k: 0 for k in LIMIT_KEYS}}
    exp = parse_iso(o["plan_expires_at"])
    active = bool(p and exp and exp > now_utc())
    return {"active": active, "unlimited": False, "name": p["name"] if p else "", "plan_id": p["id"] if p else None,
            "expires_at": o["plan_expires_at"], "expired": bool(p and exp and exp <= now_utc()),
            "days_left": max(0, (exp - now_utc()).days) if exp else None,
            "limits": {k: (p[k] if p else 0) for k in LIMIT_KEYS}}


def api_auth(roles=None, need_plan=True, perm=None):
    def deco(fn):
        @wraps(fn)
        def w(*a, **kw):
            u = current_user()
            if not u:
                return bad("Authentication required", 401)
            if roles and u["role"] not in roles:
                return bad("You don't have access to this section", 403)
            if perm and not member_perms(u).get(perm):
                return bad("Ask your account owner to enable this permission for you", 403)
            if need_plan and not plan_info(tenant_id(u))["active"]:
                return bad("Your subscription is not active. Please choose or renew a plan.", 402, code="plan_required")
            return fn(*a, **kw)
        return w
    return deco


def limit_error(tid, key, label):
    lim = plan_info(tid)["limits"].get(key, 0)
    if not lim:
        return None
    table = {"max_numbers": ("numbers", "owner_id"), "max_forms": ("forms", "owner_id")}.get(key)
    with db() as c:
        if key == "max_members":
            n = c.execute("SELECT COUNT(*) n FROM users WHERE owner_id=? AND role='member'", (tid,)).fetchone()["n"]
        elif key == "max_automations":
            n = c.execute("SELECT COUNT(*) n FROM automations WHERE number_id IN (SELECT id FROM numbers WHERE owner_id=?)", (tid,)).fetchone()["n"]
        else:
            n = c.execute(f"SELECT COUNT(*) n FROM {table[0]} WHERE {table[1]}=?", (tid,)).fetchone()["n"]
    if n >= lim:
        return bad(f"Your plan allows up to {lim} {label}. Upgrade your plan to add more.", 403, code="limit")
    return None


def ser_user(r):
    d = dict(r)
    d.pop("password_hash", None)
    return d


def valid_email(e):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e or ""))


def create_user(role, name, email, phone, password, status, owner_id=None, perms=None, business="", created_by=None, plan=None):
    email = (email or "").strip().lower()
    phone = digits(phone)
    if not name.strip():
        return None, "Enter a name"
    if not valid_email(email):
        return None, "Enter a valid email address"
    if len(phone) < 7 or len(phone) > 15:
        return None, "Enter a valid phone number"
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters"
    try:
        with db() as c:
            if c.execute("SELECT 1 FROM users WHERE phone=? AND phone!=''", (phone,)).fetchone():
                return None, "That phone number is already registered"
            cur = c.execute("""INSERT INTO users(role,name,email,phone,password_hash,status,owner_id,permissions_json,
                               business,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (role, name.strip()[:80], email, phone, generate_password_hash(password), status, owner_id,
                             json.dumps(perms or {}), business.strip()[:120], created_by, now_iso()))
            uid = cur.lastrowid
            if role == "client":
                c.execute("UPDATE users SET owner_id=id WHERE id=?", (uid,))
    except sqlite3.IntegrityError:
        return None, "That email is already registered"
    if plan:
        apply_plan(uid, plan)
    return uid, ""


def apply_plan(uid, plan, days=None):
    with db() as c:
        u = c.execute("SELECT plan_id,plan_expires_at FROM users WHERE id=?", (uid,)).fetchone()
        base = now_utc()
        cur = parse_iso(u["plan_expires_at"]) if u else None
        if u and u["plan_id"] == plan["id"] and cur and cur > base:
            base = cur
        exp = base + timedelta(days=int(days or plan["duration_days"]))
        c.execute("UPDATE users SET plan_id=?,plan_expires_at=? WHERE id=?", (plan["id"], exp.isoformat(), uid))
    return exp.isoformat()


def ser_plan(r):
    d = dict(r)
    try:
        d["features"] = json.loads(d.pop("features_json") or "[]")
    except ValueError:
        d["features"] = []
    return d


def get_plan(pid):
    with db() as c:
        r = c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


# ───────────────────────── number access ─────────────────────────
def get_number(number_id):
    with db() as c:
        row = c.execute("SELECT * FROM numbers WHERE id=? AND active=1", (number_id,)).fetchone()
    return dict(row) if row else None


def number_by_phone_id(pid):
    with db() as c:
        row = c.execute("SELECT * FROM numbers WHERE phone_number_id=? AND active=1", (str(pid),)).fetchone()
    return dict(row) if row else None


def user_number_ids(u=None):
    u = u or current_user()
    with db() as c:
        if u["role"] == "member":
            rows = c.execute("""SELECT n.id FROM numbers n JOIN member_numbers mn ON mn.number_id=n.id
                                WHERE mn.user_id=? AND n.owner_id=? AND n.active=1""", (u["id"], u["owner_id"])).fetchall()
        else:
            rows = c.execute("SELECT id FROM numbers WHERE owner_id=? AND active=1", (u["id"],)).fetchall()
    return [r["id"] for r in rows]


def accessible_number(number_id):
    if not number_id:
        return None
    n = get_number(number_id)
    if not n or number_id not in user_number_ids():
        return None
    return n


def lead_for_user(lead_id):
    with db() as c:
        row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return None, None
    n = accessible_number(row["number_id"])
    return (dict(row), n) if n else (None, None)


def media_url(m):
    return f"{PUBLIC_BASE_URL}/m/{m['token']}"


def get_media(media_id, owner_id):
    if not media_id:
        return None
    with db() as c:
        r = c.execute("SELECT * FROM media WHERE id=? AND owner_id=?", (media_id, owner_id)).fetchone()
    return dict(r) if r else None


# ───────────────────────── Meta Graph ─────────────────────────
def meta_request(number, method, path, **kwargs):
    url = f"{META_GRAPH}/{number['api_version']}/{str(path).lstrip('/')}"
    try:
        r = requests.request(method, url, headers={"Authorization": f"Bearer {number['access_token']}"}, timeout=25, **kwargs)
        data = r.json() if r.content else {}
    except (requests.RequestException, ValueError) as e:
        return False, {"error": {"message": str(e)}}
    return (r.status_code < 400), data


def meta_error(data):
    e = (data or {}).get("error") or {}
    msg = e.get("error_user_msg") or e.get("message") or "Meta API request failed"
    det = (e.get("error_data") or {}).get("details")
    return f"{msg} — {det}" if det and det not in msg else msg


def meta_upload_sample(number, media_row):
    """Resumable Upload API: returns the 'handle' Meta needs as a template header example."""
    path = os.path.join(MEDIA_DIR, media_row["stored_name"])
    ext = media_row["filename"].rsplit(".", 1)[-1].lower()
    mime = MEDIA_MIME.get(ext, "application/octet-stream")
    if ext not in ("jpg", "jpeg", "png", "mp4", "pdf"):
        return None, "Meta accepts JPG, PNG, MP4 or PDF for template headers"
    hdr = {"Authorization": f"Bearer {number['access_token']}"}
    try:
        r1 = requests.post(f"{META_GRAPH}/{number['api_version']}/{META_APP_ID}/uploads",
                           params={"file_length": os.path.getsize(path), "file_type": mime, "file_name": media_row["filename"]},
                           headers=hdr, timeout=30)
        b1 = r1.json()
        if r1.status_code >= 400 or not b1.get("id"):
            return None, meta_error(b1)
        with open(path, "rb") as f:
            data = f.read()
        r2 = requests.post(f"{META_GRAPH}/{number['api_version']}/{b1['id']}", data=data, timeout=120,
                           headers={"Authorization": f"OAuth {number['access_token']}", "file_offset": "0"})
        b2 = r2.json()
        if r2.status_code >= 400 or not b2.get("h"):
            return None, meta_error(b2)
        return b2["h"], ""
    except (requests.RequestException, ValueError, OSError) as e:
        return None, str(e)


# ───────────────────────── leads & messages ─────────────────────────
def upsert_lead(number_id, phone, name=""):
    phone = digits(phone)
    if not phone:
        return None, False
    with db() as c:
        old = c.execute("SELECT * FROM leads WHERE number_id=? AND phone=?", (number_id, phone)).fetchone()
        if old:
            if name and (not old["name"] or old["name"] == old["phone"]):
                c.execute("UPDATE leads SET name=?,updated_at=? WHERE id=?", (name, now_iso(), old["id"]))
            return dict(c.execute("SELECT * FROM leads WHERE id=?", (old["id"],)).fetchone()), False
        cur = c.execute("INSERT INTO leads(number_id,phone,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (number_id, phone, name or phone, now_iso(), now_iso()))
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()), True


def message_text(m):
    typ = m.get("type", "unknown")
    if typ == "text":
        return (m.get("text") or {}).get("body", "")
    if typ == "button":
        return (m.get("button") or {}).get("text", "")
    if typ == "interactive":
        i = m.get("interactive") or {}
        title = (i.get("button_reply") or {}).get("title") or (i.get("list_reply") or {}).get("title")
        if title:
            return title
        resp = (i.get("nfm_reply") or {}).get("response_json") or {}
        try:
            resp = json.loads(resp) if isinstance(resp, str) else resp
        except (TypeError, ValueError):
            return "WhatsApp form submitted"
        if isinstance(resp, dict):
            resp = dict(resp)
            resp.pop("flow_token", None)
            lines = [f"{k.replace('_', ' ').title()}: {v}" for k, v in resp.items()]
            return "Form submitted\n" + "\n".join(lines) if lines else "WhatsApp form submitted"
        return "WhatsApp form submitted"
    return (m.get(typ) or {}).get("caption") or f"[{typ}]"


def save_message(number_id, lead_id, wamid, direction, typ, body, raw, media_id="", media_url_="", from_app=0):
    with db() as c:
        if wamid and c.execute("SELECT 1 FROM messages WHERE wa_message_id=?", (wamid,)).fetchone():
            return False
        c.execute("""INSERT INTO messages(number_id,lead_id,wa_message_id,direction,message_type,body,media_id,
                     media_url,status,raw_json,created_at,from_app) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (number_id, lead_id, wamid or None, direction, typ, body or "", media_id, media_url_,
                   "received" if direction == "in" else "sent", json.dumps(raw or {}, ensure_ascii=False), now_iso(), from_app))
    return True


def send_payload(number, payload):
    ok, data = meta_request(number, "POST", f"{number['phone_number_id']}/messages", json=payload)
    if not ok:
        return {"success": False, "error": meta_error(data), "details": data}
    return {"success": True, "message_id": ((data.get("messages") or [{}])[0]).get("id", ""), "response": data}


def _base(to, typ):
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to), "type": typ}


def send_text(number, to, body):
    return send_payload(number, {**_base(to, "text"), "text": {"preview_url": True, "body": str(body)[:4096]}})


def send_media(number, to, kind, url, caption="", filename=""):
    obj = {"link": url}
    if caption:
        obj["caption"] = caption[:1024]
    if kind == "document" and filename:
        obj["filename"] = filename[:100]
    return send_payload(number, {**_base(to, kind), kind: obj})


def _header_obj(m):
    obj = {"link": media_url(m)}
    if m["kind"] == "document":
        obj["filename"] = m["filename"][:100]
    return {"type": m["kind"], m["kind"]: obj}


def send_buttons(number, to, body, buttons, header_media=None):
    inter = {"type": "button", "body": {"text": body[:1024]},
             "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in buttons[:3]]}}
    if header_media:
        inter["header"] = _header_obj(header_media)
    return send_payload(number, {**_base(to, "interactive"), "interactive": inter})


def send_list(number, to, body, rows, label="Choose"):
    inter = {"type": "list", "body": {"text": body[:1024]},
             "action": {"button": (label or "Choose")[:20],
                        "sections": [{"title": "Options", "rows": [{"id": r["id"], "title": r["title"][:24]} for r in rows[:10]]}]}}
    return send_payload(number, {**_base(to, "interactive"), "interactive": inter})


def store_outgoing(number, lead, result, typ, body, raw=None, media_url_=""):
    if result.get("success"):
        save_message(number["id"], lead["id"], result.get("message_id", ""), "out", typ, body,
                     raw or result.get("response", {}), "", media_url_)
    return result


def say(number, lead, text):
    return store_outgoing(number, lead, send_text(number, lead["phone"], text), "text", text)


def render_vars(text, lead):
    name = (lead.get("name") or "").strip()
    real = name if name and name != lead.get("phone") else ""
    vals = {"name": real or "there", "first_name": (real.split(" ")[0] if real else "there"), "phone": lead.get("phone", "")}
    out = str(text or "")
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v).replace("{" + k + "}", v)
    return out


def set_lead_status(lead_id, status):
    if status in LEAD_STATUSES:
        with db() as c:
            c.execute("UPDATE leads SET status=?,updated_at=? WHERE id=?", (status, now_iso(), lead_id))


# ───────────────────────── templates (sending) ─────────────────────────
def template_parts(comps):
    header = next((x for x in comps if x.get("type") == "HEADER"), None)
    body = next((x for x in comps if x.get("type") == "BODY"), {})
    text = body.get("text", "")
    nums = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", text)]
    return {"header_format": ((header or {}).get("format") or "").lower(), "body_text": text,
            "body_vars": max(nums) if nums else 0, "footer": next((x.get("text", "") for x in comps if x.get("type") == "FOOTER"), ""),
            "buttons": next((x.get("buttons", []) for x in comps if x.get("type") == "BUTTONS"), [])}


def send_template(number, lead, tpl, variables, header_media_id=None):
    comps = json.loads(tpl["components_json"] or "[]")
    info = template_parts(comps)
    parts, shown_media = [], ""
    if info["header_format"] in ("image", "video", "document"):
        m = get_media(header_media_id or tpl.get("header_media_id"), number["owner_id"])
        if not m or m["kind"] != info["header_format"]:
            return {"success": False, "error": f"This template needs a {info['header_format']} header. Pick a file from your Media library."}
        parts.append({"type": "header", "parameters": [_header_obj(m)]})
        shown_media = media_url(m)
    vals = [render_vars(v, lead).strip() or "-" for v in (variables or [])]
    while len(vals) < info["body_vars"]:
        vals.append("-")
    vals = [re.sub(r"[\r\n\t]+", " ", v)[:1000] for v in vals[:info["body_vars"]]]
    if info["body_vars"]:
        parts.append({"type": "body", "parameters": [{"type": "text", "text": v} for v in vals]})
    payload = {**_base(lead["phone"], "template"),
               "template": {"name": tpl["meta_name"], "language": {"code": tpl["language"]}}}
    if parts:
        payload["template"]["components"] = parts
    result = send_payload(number, payload)
    rendered = info["body_text"]
    for i, v in enumerate(vals, 1):
        rendered = rendered.replace("{{%d}}" % i, v)
    return store_outgoing(number, lead, result, "template", rendered or f"[Template {tpl['meta_name']}]",
                          {"template": tpl["meta_name"]}, shown_media)


def get_template(tpl_id, number_id):
    with db() as c:
        r = c.execute("SELECT * FROM templates WHERE id=? AND number_id=?", (tpl_id, number_id)).fetchone()
    return dict(r) if r else None


# ───────────────────────── automations engine ─────────────────────────
NODE_TYPES = ("message", "template", "form", "status")
BTN_ACTIONS = ("next", "text", "url", "form", "status")


def normalize_graph(g):
    g = g or {}
    nodes = [dict(n) for n in (g.get("nodes") or []) if n.get("type") != "trigger"]
    start = g.get("start")
    if not start:
        trig = next((n for n in (g.get("nodes") or []) if n.get("type") == "trigger"), None)
        start = ((trig or {}).get("data") or {}).get("next_id") or (nodes[0].get("id") if nodes else "")
    for n in nodes:
        n["id"] = str(n.get("id"))
        n["data"] = dict(n.get("data") or {})
        if n.get("type") == "image":
            n["type"] = "message"
            n["data"]["_legacy_image"] = n["data"].get("image_url", "")
    return {"start": str(start or ""), "nodes": nodes}


def validate_graph(graph, number):
    nodes = (graph or {}).get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > 40:
        return None, "Add between 1 and 40 steps"
    owner = number["owner_id"]
    ids = set()
    for n in nodes:
        nid = str(n.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", nid) or nid in ids:
            return None, "Invalid step id"
        ids.add(nid)
    clean = []
    for i, n in enumerate(nodes, 1):
        nid, typ, d = str(n["id"]), n.get("type"), (n.get("data") or {})
        label = f"Step {i}"
        if typ not in NODE_TYPES:
            return None, f"{label}: unknown step type"
        nxt = str(d.get("next_id") or "")
        if nxt and nxt not in ids:
            return None, f"{label}: the next step no longer exists"
        out = {"next_id": nxt}
        if typ == "message":
            body = str(d.get("body") or "").strip()[:1024]
            mid = int(d.get("media_id") or 0) or None
            if mid and not get_media(mid, owner):
                return None, f"{label}: the attached file was deleted. Pick another."
            buttons = []
            for b in (d.get("buttons") or [])[:10]:
                title = str(b.get("title") or "").strip()[:24 if len(d.get("buttons") or []) > 3 else 20]
                if not title:
                    continue
                act = b.get("action", "next")
                if act not in BTN_ACTIONS:
                    return None, f"{label}: bad button action"
                bb = {"title": title, "action": act, "next_id": str(b.get("next_id") or ""), "text": str(b.get("text") or "")[:1024],
                      "url": str(b.get("url") or "").strip()[:500], "form_id": int(b.get("form_id") or 0), "status": b.get("status") or ""}
                if act == "next" and bb["next_id"] not in ids:
                    return None, f"{label}: button “{title}” needs a step to go to"
                if act == "text" and not bb["text"]:
                    return None, f"{label}: button “{title}” needs a reply text"
                if act == "url" and not re.match(r"https?://", bb["url"]):
                    return None, f"{label}: button “{title}” needs a link starting with https://"
                if act == "form" and not bb["form_id"]:
                    return None, f"{label}: button “{title}” needs a form"
                if act == "status" and bb["status"] not in LEAD_STATUSES:
                    return None, f"{label}: button “{title}” needs a lead status"
                buttons.append(bb)
            if not body and not mid:
                return None, f"{label}: write a message or attach a file"
            if buttons and not body:
                return None, f"{label}: buttons need message text"
            out.update(body=body, media_id=mid, buttons=buttons)
        elif typ == "template":
            tpl = get_template(int(d.get("template_id") or 0), number["id"])
            if not tpl:
                return None, f"{label}: pick a template"
            out.update(template_id=tpl["id"], variables=[str(v)[:200] for v in (d.get("variables") or [])][:10])
        elif typ == "form":
            with db() as c:
                f = c.execute("SELECT id FROM forms WHERE id=? AND owner_id=?", (int(d.get("form_id") or 0), owner)).fetchone()
            if not f:
                return None, f"{label}: pick a form"
            out = {"form_id": f["id"]}
        elif typ == "status":
            if d.get("status") not in LEAD_STATUSES:
                return None, f"{label}: pick a lead status"
            out.update(status=d["status"], body=str(d.get("body") or "").strip()[:1024])
        clean.append({"id": nid, "type": typ, "title": str(n.get("title") or "")[:40], "data": out})
    start = str(graph.get("start") or clean[0]["id"])
    if start not in ids:
        return None, "Choose which step starts the automation"
    return {"start": start, "nodes": clean}, ""


def send_form_node(number, lead, form_id):
    with db() as c:
        f = c.execute("SELECT * FROM forms WHERE id=? AND owner_id=?", (form_id, number["owner_id"])).fetchone()
    if not f or not f["meta_flow_id"]:
        return {"success": False, "error": "The selected WhatsApp Flow is not ready"}
    token = f"dmvora:{number['id']}:{lead['id']}:{f['id']}:{secrets.token_urlsafe(8)}"
    payload = {**_base(lead["phone"], "interactive"), "interactive": {
        "type": "flow", "body": {"text": (f["prompt"] or "Please complete this form.")[:1024]},
        "action": {"name": "flow", "parameters": {
            "flow_message_version": "3", "flow_token": token, "flow_id": str(f["meta_flow_id"]),
            "flow_cta": (f["cta_text"] or "Open form")[:20], "flow_action": "navigate",
            "flow_action_payload": {"screen": f["first_screen_id"] or "WELCOME"}}}}}
    return store_outgoing(number, lead, send_payload(number, payload), "flow", f["prompt"], {"flow_id": f["meta_flow_id"]})


def send_node(number, lead, node, flow_id):
    d, typ = node.get("data") or {}, node.get("type")
    if typ == "form":
        return send_form_node(number, lead, int(d.get("form_id") or 0))
    if typ == "status":
        set_lead_status(lead["id"], d.get("status"))
        if d.get("body"):
            return say(number, lead, render_vars(d["body"], lead))
        return {"success": True}
    if typ == "template":
        tpl = get_template(int(d.get("template_id") or 0), number["id"])
        if not tpl:
            return {"success": False, "error": "Template missing"}
        return send_template(number, lead, tpl, d.get("variables") or [])
    body = render_vars(d.get("body") or "", lead)
    media = get_media(d.get("media_id"), number["owner_id"])
    btns = d.get("buttons") or []
    if btns:
        items = [{"id": f"dmv:{flow_id}:{node['id']}:{i}", "title": b["title"]} for i, b in enumerate(btns)]
        if len(items) <= 3:
            res = send_buttons(number, lead["phone"], body, items, media)
        else:
            res = send_list(number, lead["phone"], body, items)
        return store_outgoing(number, lead, res, "interactive", body, {"buttons": [b["title"] for b in btns]},
                              media_url(media) if media and len(items) <= 3 else "")
    if media:
        res = send_media(number, lead["phone"], media["kind"], media_url(media), body, media["filename"])
        return store_outgoing(number, lead, res, media["kind"], body, {}, media_url(media))
    if d.get("_legacy_image"):
        res = send_media(number, lead["phone"], "image", d["_legacy_image"], body)
        return store_outgoing(number, lead, res, "image", body, {}, d["_legacy_image"])
    return say(number, lead, body)


def execute_node(number, lead, graph, node_id, flow_id, depth=0):
    if depth > 12:
        return
    node = next((n for n in graph.get("nodes", []) if str(n.get("id")) == str(node_id)), None)
    if not node:
        return
    res = send_node(number, lead, node, flow_id)
    if not res.get("success"):
        app.logger.warning("DMvora automation step failed: %s", res.get("error"))
        return
    d = node.get("data") or {}
    if node.get("type") == "form" or (node.get("type") == "message" and d.get("buttons")):
        return   # waits for the customer's choice / form
    if d.get("next_id"):
        execute_node(number, lead, graph, d["next_id"], flow_id, depth + 1)


def automation_blocked(number, lead):
    if not plan_info(number["owner_id"])["active"]:
        return True
    hr = parse_iso(lead.get("human_replied_at"))
    return bool(hr and HUMAN_PAUSE_MIN and (now_utc() - hr) < timedelta(minutes=HUMAN_PAUSE_MIN))


def keyword_match(f, text):
    t = (text or "").lower().strip()
    mt = f.get("match_type") or "contains"
    for k in [x.strip().lower() for x in (f.get("keyword") or "").split(",") if x.strip()]:
        if (mt == "exact" and t == k) or (mt == "starts" and t.startswith(k)) or (mt == "contains" and k in t):
            return True
    return False


def run_automations(number, lead, text):
    if automation_blocked(number, lead):
        return
    with db() as c:
        flows = [dict(r) for r in c.execute("SELECT * FROM automations WHERE number_id=? AND enabled=1 ORDER BY id", (number["id"],))]
        inbound = c.execute("SELECT created_at FROM messages WHERE number_id=? AND lead_id=? AND direction='in' ORDER BY id DESC LIMIT 2",
                            (number["id"], lead["id"])).fetchall()
    prev = parse_iso(inbound[1]["created_at"]) if len(inbound) > 1 else None

    def run(f):
        graph = normalize_graph(json.loads(f["graph_json"] or "{}"))
        if graph["start"]:
            execute_node(number, lead, graph, graph["start"], f["id"])
            return True
        return False

    ran = False
    for f in flows:
        if f["trigger_type"] == "keyword" and text and keyword_match(f, text):
            ran = run(f) or ran
    if not ran:
        for f in flows:
            if f["trigger_type"] == "first_message" and len(inbound) == 1:
                ran = run(f) or ran
            elif f["trigger_type"] == "first_after_24h" and (prev is None or (now_utc() - prev).total_seconds() >= 86400):
                ran = run(f) or ran
    if not ran:
        for f in flows:
            if f["trigger_type"] == "any_message" and run(f):
                break


def handle_click(number, lead, reply_id):
    m = re.fullmatch(r"dmv:(\d+):([^:]+):(\d+)", reply_id or "")
    if not m:
        return
    flow_id, node_id, idx = m.group(1), m.group(2), int(m.group(3))
    with db() as c:
        f = c.execute("SELECT * FROM automations WHERE id=? AND number_id=?", (flow_id, number["id"])).fetchone()
    if not f or automation_blocked(number, lead):
        return
    graph = normalize_graph(json.loads(f["graph_json"] or "{}"))
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    btns = ((node or {}).get("data") or {}).get("buttons") or []
    if idx >= len(btns):
        return
    b = btns[idx]
    act = b.get("action")
    if act == "next" and b.get("next_id"):
        execute_node(number, lead, graph, b["next_id"], flow_id)
    elif act == "text":
        say(number, lead, render_vars(b.get("text", ""), lead))
    elif act == "url":
        say(number, lead, f"{render_vars(b.get('text') or 'Here is the link', lead)}: {b.get('url')}")
    elif act == "form":
        send_form_node(number, lead, int(b.get("form_id") or 0))
    elif act == "status":
        set_lead_status(lead["id"], b.get("status"))
        if b.get("text"):
            say(number, lead, render_vars(b["text"], lead))


def handle_flow_response(number, lead, m):
    nfm = (m.get("interactive") or {}).get("nfm_reply") or {}
    raw = nfm.get("response_json") or "{}"
    try:
        sub = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        sub = {"raw_response": str(raw)}
    token = str(sub.pop("flow_token", "") or "")
    t = re.fullmatch(r"dmvora:(\d+):(\d+):(\d+):[^:]+", token)
    if not t or int(t.group(1)) != number["id"] or int(t.group(2)) != lead["id"]:
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM forms WHERE id=? AND owner_id=?", (int(t.group(3)), number["owner_id"])).fetchone():
            return
        old = c.execute("SELECT form_data FROM leads WHERE id=?", (lead["id"],)).fetchone()
        data = json.loads(old["form_data"] or "{}") if old else {}
        data.update(sub)
        c.execute("UPDATE leads SET form_data=?,updated_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now_iso(), lead["id"]))


def process_meta_payload(payload):
    try:
        _process(payload)
    except Exception:
        app.logger.exception("DMvora webhook processing failed")


def _process(payload):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            field, value = change.get("field"), change.get("value") or {}
            if field == "message_template_status_update":
                name, lang, ev = value.get("message_template_name"), value.get("message_template_language"), value.get("event")
                if name and ev:
                    with db() as c:
                        c.execute("""UPDATE templates SET status=? WHERE meta_name=? AND language=?
                                     AND number_id IN (SELECT id FROM numbers WHERE waba_id=?)""",
                                  (str(ev).upper(), name, lang, str(entry.get("id") or "")))
                continue
            if field not in ("messages", "smb_message_echoes"):
                continue
            pid = str((value.get("metadata") or {}).get("phone_number_id") or "")
            number = number_by_phone_id(pid)
            if not number:   # strict tenant boundary: unknown numbers are ignored
                continue
            if field == "smb_message_echoes":
                for m in value.get("message_echoes") or []:
                    to = digits(m.get("to"))
                    if not to:
                        continue
                    lead, _ = upsert_lead(number["id"], to)
                    if save_message(number["id"], lead["id"], m.get("id", ""), "out", m.get("type", "text"),
                                    message_text(m), m, from_app=1):
                        with db() as c:
                            c.execute("UPDATE leads SET human_replied_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), lead["id"]))
                continue
            names = {str(x.get("wa_id")): (x.get("profile") or {}).get("name", "") for x in (value.get("contacts") or [])}
            for m in value.get("messages") or []:
                phone = digits(m.get("from"))
                if not phone:
                    continue
                lead, _ = upsert_lead(number["id"], phone, names.get(phone, ""))
                typ, body = m.get("type", "unknown"), message_text(m)
                mid = str((m.get(typ) or {}).get("id") or "") if isinstance(m.get(typ), dict) and typ not in ("text", "button", "interactive") else ""
                if not save_message(number["id"], lead["id"], m.get("id", ""), "in", typ, body, m, media_id=mid):
                    continue
                inter = (m.get("interactive") or {}) if typ == "interactive" else {}
                if inter.get("type") == "nfm_reply":
                    handle_flow_response(number, lead, m)
                elif inter.get("type") in ("button_reply", "list_reply"):
                    handle_click(number, lead, (inter.get(inter["type"]) or {}).get("id", ""))
                else:
                    run_automations(number, lead, body if typ in ("text", "button") else "")
            for st in value.get("statuses") or []:
                if st.get("id"):
                    with db() as c:
                        c.execute("UPDATE messages SET status=? WHERE wa_message_id=?", (st.get("status", "unknown"), st["id"]))


def verify_internal_signature(raw):
    if not PRAVAH_WEBHOOK_SECRET:
        return False
    ts, sig = request.headers.get("X-Pravah-Timestamp", ""), request.headers.get("X-Pravah-Signature", "")
    try:
        if abs(int(time.time()) - int(ts)) > WEBHOOK_MAX_SKEW:
            return False
    except (ValueError, TypeError):
        return False
    exp = hmac.new(PRAVAH_WEBHOOK_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, sig)


@app.post("/internal/meta/events")
def pravah_forwarded_events():
    raw = request.get_data()
    if not verify_internal_signature(raw):
        return bad("Invalid internal signature", 401)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        return bad("Invalid JSON")
    key = hashlib.sha256(raw).hexdigest()
    with db() as c:
        try:
            c.execute("INSERT INTO event_dedup(event_key,created_at) VALUES(?,?)", (key, now_iso()))
            c.execute("DELETE FROM event_dedup WHERE created_at < ?", ((now_utc() - timedelta(days=2)).isoformat(),))
        except sqlite3.IntegrityError:
            return jsonify(ok=True, duplicate=True)
    threading.Thread(target=process_meta_payload, args=(payload,), daemon=True).start()
    return jsonify(ok=True)


# ───────────────────────── pages & auth ─────────────────────────
@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("workspace"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    if not rate_ok("login:" + client_ip(), 12, 600):
        flash("Too many attempts. Please wait a few minutes.", "error")
        return redirect(url_for("login"))
    ident = (request.form.get("identifier") or request.form.get("username") or "").strip().lower()
    pw = request.form.get("password", "")
    with db() as c:
        row = c.execute("""SELECT * FROM users WHERE lower(email)=? OR lower(COALESCE(username,''))=?
                           OR (phone!='' AND phone=?) LIMIT 1""", (ident, ident, digits(ident))).fetchone()
    if not row or not check_password_hash(row["password_hash"], pw):
        flash("Incorrect login details.", "error")
        return redirect(url_for("login"))
    msgs = {"pending": "Your account is waiting for admin approval. We'll let you in as soon as it's approved.",
            "rejected": "Your signup request was not approved. Please contact the admin.",
            "disabled": "This account has been disabled. Please contact the admin."}
    if row["status"] != "active":
        flash(msgs.get(row["status"], "This account cannot sign in."), "error")
        return redirect(url_for("login"))
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    csrf_token()
    with db() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), row["id"]))
    return redirect(url_for("workspace"))


@app.get("/signup")
def signup():
    if current_user():
        return redirect(url_for("workspace"))
    return render_template("signup.html", done=request.args.get("done"))


@app.post("/signup")
def signup_post():
    if not rate_ok("signup:" + client_ip(), 6, 3600):
        flash("Too many signups from this network. Try again later.", "error")
        return redirect(url_for("signup"))
    f = request.form
    if f.get("password") != f.get("confirm"):
        flash("Passwords do not match.", "error")
        return redirect(url_for("signup"))
    uid, err = create_user("client", f.get("name", ""), f.get("email", ""), f.get("phone", ""), f.get("password", ""),
                           "pending", business=f.get("business", ""))
    if not uid:
        flash(err, "error")
        return redirect(url_for("signup"))
    return redirect(url_for("signup", done=1))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def dashboard():
    return redirect(url_for("workspace"))


@app.get("/app")
def workspace():
    if not current_user():
        return redirect(url_for("login"))
    return render_template("app.html")


@app.get("/health")
def health():
    return jsonify(status="ok", service="dmvora", time=now_iso())


@app.get("/api/me")
@api_auth(need_plan=False)
def api_me():
    u = current_user()
    tid = tenant_id(u)
    pi = plan_info(tid)
    with db() as c:
        usage = {
            "max_numbers": c.execute("SELECT COUNT(*) n FROM numbers WHERE owner_id=?", (tid,)).fetchone()["n"],
            "max_members": c.execute("SELECT COUNT(*) n FROM users WHERE owner_id=? AND role='member'", (tid,)).fetchone()["n"],
            "max_automations": c.execute("SELECT COUNT(*) n FROM automations WHERE number_id IN (SELECT id FROM numbers WHERE owner_id=?)", (tid,)).fetchone()["n"],
            "max_forms": c.execute("SELECT COUNT(*) n FROM forms WHERE owner_id=?", (tid,)).fetchone()["n"],
        }
    return jsonify(user={k: u[k] for k in ("id", "role", "name", "email", "phone")}, perms=member_perms(u), plan=pi, usage=usage,
                   signup_url=f"{PUBLIC_BASE_URL}/signup", payments_ready=bool(RZP_KEY_ID and RZP_KEY_SECRET),
                   rzp_key=RZP_KEY_ID)


@app.post("/api/me/password")
@api_auth(need_plan=False)
def change_password():
    d = request.get_json(silent=True) or {}
    u = current_user()
    if not check_password_hash(u["password_hash"], d.get("current", "")):
        return bad("Current password is incorrect")
    if len(d.get("new") or "") < 8:
        return bad("New password must be at least 8 characters")
    with db() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(d["new"]), u["id"]))
    return jsonify(ok=True)


# ───────────────────────── numbers / Meta signup ─────────────────────────
@app.get("/api/meta/config")
@api_auth(roles=("admin", "client"))
def meta_config():
    return jsonify(ready=bool(META_APP_ID and META_APP_SECRET and META_ES_CONFIG_ID), app_id=META_APP_ID,
                   config_id=META_ES_CONFIG_ID, api_version=META_GRAPH_VERSION)


@app.post("/api/meta/complete")
@api_auth(roles=("admin", "client"))
def meta_complete():
    tid = tenant_id()
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    waba_id, pid, business_id = digits(data.get("waba_id")), digits(data.get("phone_number_id")), digits(data.get("business_id"))
    coexistence = bool(data.get("coexistence"))
    if not code:
        return bad("Meta authorization code missing")
    if not (META_APP_ID and META_APP_SECRET):
        return bad("Meta app credentials missing", 500)
    try:
        r = requests.get(f"{META_GRAPH}/{META_GRAPH_VERSION}/oauth/access_token",
                         params={"client_id": META_APP_ID, "client_secret": META_APP_SECRET, "code": code}, timeout=25)
        td = r.json()
    except (requests.RequestException, ValueError) as e:
        return bad(str(e), 502)
    if r.status_code >= 400 or not td.get("access_token"):
        return bad(meta_error(td))
    token = td["access_token"]
    tmp = {"access_token": token, "api_version": META_GRAPH_VERSION}
    if not waba_id:
        try:
            dbg = requests.get(f"{META_GRAPH}/{META_GRAPH_VERSION}/debug_token",
                               params={"input_token": token, "access_token": f"{META_APP_ID}|{META_APP_SECRET}"}, timeout=20).json()
            for it in ((dbg.get("data") or {}).get("granular_scopes") or []):
                if it.get("scope") == "whatsapp_business_management" and it.get("target_ids") and not waba_id:
                    waba_id = digits(it["target_ids"][0])
        except (requests.RequestException, ValueError):
            pass
    if not waba_id:
        return bad("Meta did not return a WhatsApp Business Account ID. Finish Embedded Signup and retry.")
    ok, sub = meta_request(tmp, "POST", f"{waba_id}/subscribed_apps")
    if not ok:
        return bad(f"Could not subscribe this WABA: {meta_error(sub)}")
    rows = [{"id": pid}] if pid else []
    for attempt in range(4 if coexistence else 1):
        if rows:
            break
        ok, listing = meta_request(tmp, "GET", f"{waba_id}/phone_numbers", params={"fields": "id,display_phone_number,verified_name,quality_rating"})
        rows = (listing.get("data") or []) if ok else []
        if not rows:
            time.sleep(3)
    ids = [digits(p.get("id")) for p in rows if digits(p.get("id"))]
    if not ids:
        return bad("No phone number was returned by Embedded Signup. Check that the number was fully added to the WABA.")
    with db() as c:
        taken = [i for i in ids if (x := c.execute("SELECT owner_id FROM numbers WHERE phone_number_id=?", (i,)).fetchone()) and x["owner_id"] != tid]
        mine = {r["phone_number_id"] for r in c.execute("SELECT phone_number_id FROM numbers WHERE owner_id=?", (tid,))}
        owned = len(mine)
    if taken:
        return bad("This WhatsApp number is already connected to another DMvora account.")
    lim = plan_info(tid)["limits"]["max_numbers"]
    new = [i for i in ids if i not in mine]
    if lim and owned + len(new) > lim:
        return bad(f"Your plan allows up to {lim} WhatsApp numbers. Upgrade your plan to connect more.", 403, code="limit")
    saved = []
    for phone_id in ids:
        ok, info = meta_request(tmp, "GET", phone_id, params={"fields": "display_phone_number,verified_name,quality_rating"})
        if coexistence and META_ES_SYNC_HISTORY:
            for st in ("smb_app_state_sync", "history"):
                meta_request(tmp, "POST", f"{phone_id}/smb_app_data", json={"messaging_product": "whatsapp", "sync_type": st})
        with db() as c:
            c.execute("""INSERT INTO numbers(phone_number_id,waba_id,business_id,display_phone,verified_name,quality,
                         access_token,api_version,created_at,owner_id,coexistence) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(phone_number_id) DO UPDATE SET waba_id=excluded.waba_id,business_id=excluded.business_id,
                         display_phone=excluded.display_phone,verified_name=excluded.verified_name,quality=excluded.quality,
                         access_token=excluded.access_token,api_version=excluded.api_version,active=1,
                         coexistence=excluded.coexistence""",
                      (phone_id, waba_id, business_id, (info or {}).get("display_phone_number", ""),
                       (info or {}).get("verified_name", ""), (info or {}).get("quality_rating", ""),
                       token, META_GRAPH_VERSION, now_iso(), tid, 1 if coexistence else 0))
            saved.append(dict(c.execute("SELECT id,phone_number_id,display_phone,verified_name,coexistence FROM numbers WHERE phone_number_id=?", (phone_id,)).fetchone()))
    return jsonify(connected=True, numbers=saved)


@app.get("/api/numbers")
@api_auth()
def api_numbers():
    ids = user_number_ids()
    if not ids:
        return jsonify(numbers=[])
    with db() as c:
        rows = c.execute(f"""SELECT id,phone_number_id,waba_id,display_phone,verified_name,quality,coexistence,created_at
                             FROM numbers WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id DESC""", ids).fetchall()
    return jsonify(numbers=[dict(r) for r in rows])


@app.delete("/api/numbers/<int:number_id>")
@api_auth(roles=("admin", "client"))
def delete_number(number_id):
    if not accessible_number(number_id):
        return bad("Number not found", 404)
    with db() as c:
        c.execute("DELETE FROM forms WHERE number_id=?", (number_id,))
        c.execute("DELETE FROM numbers WHERE id=?", (number_id,))
    return jsonify(deleted=True)


# ───────────────────────── stats, inbox, leads ─────────────────────────
def in_clause(ids):
    return ",".join("?" * len(ids))


@app.get("/api/stats")
@api_auth()
def api_stats():
    ids = user_number_ids()
    out = {"leads": 0, "conversations": 0, "numbers": len(ids), "automations": 0, "messages_7d": 0, "series": [], "recent": []}
    if not ids:
        return jsonify(out)
    q = in_clause(ids)
    since = (now_utc() - timedelta(days=6)).strftime("%Y-%m-%d")
    with db() as c:
        out["leads"] = c.execute(f"SELECT COUNT(*) n FROM leads WHERE number_id IN ({q})", ids).fetchone()["n"]
        out["conversations"] = c.execute(f"SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE number_id IN ({q})", ids).fetchone()["n"]
        out["automations"] = c.execute(f"SELECT COUNT(*) n FROM automations WHERE enabled=1 AND number_id IN ({q})", ids).fetchone()["n"]
        rows = c.execute(f"""SELECT substr(created_at,1,10) d, direction, COUNT(*) n FROM messages
                             WHERE number_id IN ({q}) AND substr(created_at,1,10)>=? GROUP BY d,direction""", ids + [since]).fetchall()
        recent = c.execute(f"""SELECT l.id lead_id,l.number_id,l.name,l.phone,m.body,m.created_at FROM messages m JOIN leads l ON l.id=m.lead_id
                               WHERE m.number_id IN ({q}) AND m.id IN (SELECT MAX(id) FROM messages GROUP BY lead_id)
                               ORDER BY m.id DESC LIMIT 6""", ids).fetchall()
    series = {}
    for i in range(7):
        d = (now_utc() - timedelta(days=6 - i)).strftime("%Y-%m-%d")
        series[d] = {"date": d, "in": 0, "out": 0}
    for r in rows:
        if r["d"] in series:
            series[r["d"]][r["direction"]] = r["n"]
    out["series"] = list(series.values())
    out["messages_7d"] = sum(s["in"] + s["out"] for s in series.values())
    out["recent"] = [dict(r) for r in recent]
    return jsonify(out)


@app.get("/api/inbox")
@api_auth()
def api_inbox():
    n = accessible_number(request.args.get("number_id", type=int))
    if not n:
        return jsonify(conversations=[])
    q = request.args.get("q", "").strip()
    args, sql = [n["id"]], """SELECT l.id lead_id,l.phone,l.name,l.status,
      (SELECT body FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_message,
      (SELECT message_type FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_type,
      (SELECT direction FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_direction,
      (SELECT created_at FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_at,
      (SELECT created_at FROM messages m WHERE m.lead_id=l.id AND m.direction='in' ORDER BY m.id DESC LIMIT 1) last_in_at
      FROM leads l WHERE l.number_id=?"""
    if q:
        sql += " AND (l.name LIKE ? OR l.phone LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY COALESCE(last_at,l.updated_at) DESC LIMIT 300"
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    for r in rows:
        li = parse_iso(r["last_in_at"])
        r["window_open"] = bool(li and (now_utc() - li) < timedelta(hours=24))
    return jsonify(conversations=rows)


@app.get("/api/inbox/<int:lead_id>/messages")
@api_auth()
def api_thread(lead_id):
    lead, n = lead_for_user(lead_id)
    if not lead:
        return bad("Lead not found", 404)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY id ASC LIMIT 500", (lead_id,))]
        last_in = c.execute("SELECT created_at FROM messages WHERE lead_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    msgs = []
    for m in rows:
        src = m["media_url"] or (f"/api/messages/{m['id']}/media" if m["media_id"] else "")
        msgs.append({"id": m["id"], "direction": m["direction"], "type": m["message_type"], "body": m["body"], "status": m["status"],
                     "created_at": m["created_at"], "from_app": m["from_app"], "media_src": src})
    li = parse_iso(last_in["created_at"]) if last_in else None
    return jsonify(lead=lead, messages=msgs, window_open=bool(li and (now_utc() - li) < timedelta(hours=24)),
                   window_expires=(li + timedelta(hours=24)).isoformat() if li else None,
                   coexistence=bool(n["coexistence"]))


@app.get("/api/messages/<int:mid>/media")
@api_auth()
def message_media(mid):
    with db() as c:
        m = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    n = accessible_number(m["number_id"]) if m else None
    if not m or not n or not m["media_id"]:
        abort(404)
    ok, info = meta_request(n, "GET", m["media_id"])
    if not ok or not info.get("url"):
        abort(404)
    try:
        r = requests.get(info["url"], headers={"Authorization": f"Bearer {n['access_token']}"}, timeout=30)
    except requests.RequestException:
        abort(502)
    resp = Response(r.content, mimetype=info.get("mime_type") or r.headers.get("Content-Type", "application/octet-stream"))
    resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.post("/api/inbox/<int:lead_id>/send")
@api_auth()
def api_send(lead_id):
    lead, n = lead_for_user(lead_id)
    if not lead:
        return bad("Lead not found", 404)
    d = request.get_json(silent=True) or {}
    kind = d.get("type", "text")
    if kind == "template":
        tpl = get_template(int(d.get("template_id") or 0), n["id"])
        if not tpl:
            return bad("Choose a template")
        if tpl["status"] != "APPROVED":
            return bad("This template is not approved yet")
        res = send_template(n, lead, tpl, d.get("variables") or [], int(d.get("header_media_id") or 0) or None)
        return (jsonify(sent=True), 200) if res["success"] else bad(res["error"], details=res.get("details"))
    with db() as c:
        li = c.execute("SELECT created_at FROM messages WHERE lead_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    p = parse_iso(li["created_at"]) if li else None
    if not p or (now_utc() - p) > timedelta(hours=24):
        return bad("The 24-hour reply window is closed. Send an approved template instead.", 409, code="window_closed")
    if kind == "media":
        m = get_media(int(d.get("media_id") or 0), n["owner_id"])
        if not m:
            return bad("File not found in your media library")
        cap = (d.get("body") or "").strip()
        res = store_outgoing(n, lead, send_media(n, lead["phone"], m["kind"], media_url(m), cap, m["filename"]), m["kind"], cap, {}, media_url(m))
    else:
        body = (d.get("body") or "").strip()
        if not body:
            return bad("Message is empty")
        res = say(n, lead, body)
    return jsonify(sent=True) if res["success"] else bad(res["error"], details=res.get("details"))


@app.get("/api/leads")
@api_auth()
def api_leads():
    ids = user_number_ids()
    nid = request.args.get("number_id", type=int)
    if nid:
        ids = [nid] if nid in ids else []
    if not ids:
        return jsonify(leads=[])
    sql = f"SELECT l.*,n.display_phone FROM leads l JOIN numbers n ON n.id=l.number_id WHERE l.number_id IN ({in_clause(ids)})"
    args = list(ids)
    q, st = request.args.get("q", "").strip(), request.args.get("status", "")
    if q:
        sql += " AND (l.name LIKE ? OR l.phone LIKE ? OR l.email LIKE ?)"
        args += [f"%{q}%"] * 3
    if st in LEAD_STATUSES:
        sql += " AND l.status=?"
        args.append(st)
    sql += " ORDER BY l.updated_at DESC LIMIT 1000"
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    for r in rows:
        try:
            r["form_data"] = json.loads(r["form_data"] or "{}")
        except ValueError:
            r["form_data"] = {}
    return jsonify(leads=rows)


@app.get("/api/leads/export")
@api_auth()
def leads_export():
    ids = user_number_ids()
    if not ids:
        return Response("", mimetype="text/csv")
    with db() as c:
        rows = c.execute(f"""SELECT l.name,l.phone,l.email,l.status,n.display_phone,l.form_data,l.created_at
                             FROM leads l JOIN numbers n ON n.id=l.number_id WHERE l.number_id IN ({in_clause(ids)}) ORDER BY l.id DESC""", ids).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Phone", "Email", "Status", "WhatsApp number", "Form answers", "Created"])
    for r in rows:
        w.writerow([("'" + str(v) if str(v)[:1] in "=+-@" else v) for v in r])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=dmvora-leads.csv"})


@app.patch("/api/leads/<int:lead_id>")
@api_auth()
def update_lead(lead_id):
    lead, _ = lead_for_user(lead_id)
    if not lead:
        return bad("Lead not found", 404)
    d = request.get_json(silent=True) or {}
    status = d.get("status", lead["status"])
    if status not in LEAD_STATUSES:
        return bad("Invalid status")
    with db() as c:
        c.execute("UPDATE leads SET name=?,email=?,status=?,notes=?,updated_at=? WHERE id=?",
                  (str(d.get("name", lead["name"]))[:100], str(d.get("email", lead["email"]))[:150], status,
                   str(d.get("notes", lead["notes"]))[:2000], now_iso(), lead_id))
    return jsonify(ok=True)


# ───────────────────────── media library ─────────────────────────
@app.get("/m/<token>")
def public_media(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        abort(404)
    with db() as c:
        row = c.execute("SELECT * FROM media WHERE token=?", (token,)).fetchone()
    if not row:
        abort(404)
    return send_file(os.path.join(MEDIA_DIR, row["stored_name"]), mimetype=row["mime"], download_name=row["filename"], conditional=True)


def ser_media(r):
    d = dict(r)
    d["url"] = media_url(d)
    d.pop("stored_name", None)
    return d


@app.get("/api/media")
@api_auth()
def media_list():
    kind = request.args.get("kind", "")
    sql, args = "SELECT * FROM media WHERE owner_id=?", [tenant_id()]
    if kind in ("image", "video", "document"):
        sql += " AND kind=?"
        args.append(kind)
    with db() as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall()
    return jsonify(media=[ser_media(r) for r in rows])


@app.post("/api/media")
@api_auth()
def media_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return bad("Choose a file")
    name = secure_filename(f.filename) or "file"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kind = MEDIA_KINDS.get(ext)
    if not kind:
        return bad("Unsupported file. Use JPG, PNG, MP4, 3GP, PDF, DOC/DOCX, XLS/XLSX, PPT/PPTX, TXT or CSV.")
    data = f.read()
    if not data:
        return bad("File is empty")
    if len(data) > MEDIA_MAX_MB[kind] * 1024 * 1024:
        return bad(f"{kind.title()} files can be at most {MEDIA_MAX_MB[kind]} MB on WhatsApp")
    token = secrets.token_urlsafe(24)
    stored = f"{token}.{ext}"
    with open(os.path.join(MEDIA_DIR, stored), "wb") as fh:
        fh.write(data)
    with db() as c:
        cur = c.execute("INSERT INTO media(owner_id,token,kind,filename,stored_name,mime,size,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (tenant_id(), token, kind, name[:120], stored, MEDIA_MIME[ext], len(data), now_iso()))
        row = c.execute("SELECT * FROM media WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(media=ser_media(row)), 201


@app.delete("/api/media/<int:media_id>")
@api_auth()
def media_delete(media_id):
    with db() as c:
        row = c.execute("SELECT * FROM media WHERE id=? AND owner_id=?", (media_id, tenant_id())).fetchone()
        if not row:
            return bad("File not found", 404)
        c.execute("UPDATE templates SET header_media_id=NULL WHERE header_media_id=?", (media_id,))
        c.execute("DELETE FROM media WHERE id=?", (media_id,))
    try:
        os.remove(os.path.join(MEDIA_DIR, row["stored_name"]))
    except OSError:
        pass
    return jsonify(deleted=True)


# ───────────────────────── templates ─────────────────────────
def ser_template(r):
    d = dict(r)
    comps = json.loads(d.pop("components_json") or "[]")
    d.update(template_parts(comps))
    d["header_media"] = None
    if d.get("header_media_id"):
        m = get_media(d["header_media_id"], tenant_id())
        d["header_media"] = ser_media(m) if m else None
    return d


@app.get("/api/templates")
@api_auth()
def api_templates():
    n = accessible_number(request.args.get("number_id", type=int))
    if not n:
        return jsonify(templates=[])
    with db() as c:
        rows = c.execute("SELECT * FROM templates WHERE number_id=? ORDER BY meta_name", (n["id"],)).fetchall()
    return jsonify(templates=[ser_template(r) for r in rows])


@app.post("/api/templates/sync")
@api_auth(perm="templates")
def sync_templates():
    n = accessible_number(int((request.get_json(silent=True) or {}).get("number_id") or 0))
    if not n:
        return bad("Select a number")
    items, params = [], {"fields": "name,language,category,status,components", "limit": 100}
    for _ in range(5):
        ok, res = meta_request(n, "GET", f"{n['waba_id']}/message_templates", params=params)
        if not ok:
            return bad(meta_error(res))
        items += res.get("data") or []
        after = ((res.get("paging") or {}).get("cursors") or {}).get("after")
        if not (res.get("paging") or {}).get("next") or not after:
            break
        params["after"] = after
    with db() as c:
        for t in items:
            lang = t.get("language") if isinstance(t.get("language"), str) else (t.get("language") or {}).get("code", "en_US")
            comps = t.get("components") or []
            c.execute("""INSERT INTO templates(number_id,meta_name,language,category,status,components_json,header_format)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(number_id,meta_name,language) DO UPDATE SET
                         category=excluded.category,status=excluded.status,components_json=excluded.components_json,
                         header_format=excluded.header_format""",
                      (n["id"], t.get("name", ""), lang, t.get("category", ""), t.get("status", ""), json.dumps(comps),
                       template_parts(comps)["header_format"]))
    return jsonify(synced=len(items))


@app.post("/api/templates")
@api_auth(perm="templates")
def create_template():
    d = request.get_json(silent=True) or {}
    n = accessible_number(int(d.get("number_id") or 0))
    if not n:
        return bad("Select a number")
    name = re.sub(r"[^a-z0-9_]", "_", (d.get("name") or "").lower()).strip("_")[:100]
    body = (d.get("body") or "").strip()
    lang = d.get("language") or "en_US"
    if not name or not body or len(body) > 1024:
        return bad("Template name and body (max 1024 characters) are required")
    nums = sorted({int(x) for x in re.findall(r"\{\{(\d+)\}\}", body)})
    if nums != list(range(1, len(nums) + 1)):
        return bad("Variables must be {{1}}, {{2}}, {{3}} in order with no gaps")
    comps, header_media = [], None
    ht = (d.get("header_type") or "none").lower()
    if ht == "text":
        ht_text = (d.get("header_text") or "").strip()
        if not ht_text or len(ht_text) > 60:
            return bad("Header text is required (max 60 characters)")
        comps.append({"type": "HEADER", "format": "TEXT", "text": ht_text})
    elif ht in ("image", "video", "document"):
        header_media = get_media(int(d.get("header_media_id") or 0), n["owner_id"])
        if not header_media or header_media["kind"] != ht:
            return bad(f"Choose a {ht} from your Media library for the header")
        if not META_APP_ID:
            return bad("META_APP_ID is not configured on the server", 500)
        handle, err = meta_upload_sample(n, header_media)
        if not handle:
            return bad(f"Could not upload the header file to Meta: {err}")
        comps.append({"type": "HEADER", "format": ht.upper(), "example": {"header_handle": [handle]}})
    bc = {"type": "BODY", "text": body}
    if nums:
        ex = [str(x).strip() for x in (d.get("body_examples") or [])]
        if len(ex) < len(nums) or not all(ex[:len(nums)]):
            return bad(f"Give a sample value for each of the {len(nums)} variable(s)")
        bc["example"] = {"body_text": [ex[:len(nums)]]}
    comps.append(bc)
    if d.get("footer"):
        comps.append({"type": "FOOTER", "text": str(d["footer"])[:60]})
    btns = []
    for b in (d.get("buttons") or [])[:10]:
        title, bt = (b.get("title") or "").strip()[:25], b.get("type")
        if not title:
            continue
        if bt == "URL":
            url = (b.get("url") or "").strip()
            if not url.startswith("https://"):
                return bad("Button links must start with https://")
            btns.append({"type": "URL", "text": title, "url": url})
        elif bt == "PHONE_NUMBER":
            ph = "+" + digits(b.get("phone"))
            if len(ph) < 8:
                return bad("Call button needs a phone number with country code")
            btns.append({"type": "PHONE_NUMBER", "text": title, "phone_number": ph})
        else:
            btns.append({"type": "QUICK_REPLY", "text": title})
    if btns:
        btns.sort(key=lambda x: 0 if x["type"] == "QUICK_REPLY" else 1)
        comps.append({"type": "BUTTONS", "buttons": btns})
    ok, res = meta_request(n, "POST", f"{n['waba_id']}/message_templates",
                           json={"name": name, "language": lang, "category": d.get("category") or "UTILITY",
                                 "allow_category_change": True, "components": comps})
    if not ok:
        return bad(meta_error(res), details=res)
    with db() as c:
        c.execute("""INSERT INTO templates(number_id,meta_name,language,category,status,components_json,header_format,header_media_id)
                     VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(number_id,meta_name,language) DO UPDATE SET category=excluded.category,
                     status=excluded.status,components_json=excluded.components_json,header_format=excluded.header_format,
                     header_media_id=excluded.header_media_id""",
                  (n["id"], name, lang, res.get("category") or d.get("category") or "UTILITY", res.get("status", "PENDING"),
                   json.dumps(comps), ht if ht != "none" else "", header_media["id"] if header_media else None))
    return jsonify(submitted=True, status=res.get("status", "PENDING"))


@app.post("/api/templates/<int:tpl_id>/header")
@api_auth(perm="templates")
def set_template_header(tpl_id):
    with db() as c:
        t = c.execute("SELECT * FROM templates WHERE id=?", (tpl_id,)).fetchone()
    n = accessible_number(t["number_id"]) if t else None
    if not n:
        return bad("Template not found", 404)
    mid = int((request.get_json(silent=True) or {}).get("media_id") or 0) or None
    if mid:
        m = get_media(mid, n["owner_id"])
        if not m or m["kind"] != t["header_format"]:
            return bad(f"This template needs a {t['header_format']} file")
    with db() as c:
        c.execute("UPDATE templates SET header_media_id=? WHERE id=?", (mid, tpl_id))
    return jsonify(ok=True)


@app.delete("/api/templates/<int:tpl_id>")
@api_auth(perm="templates")
def delete_template(tpl_id):
    with db() as c:
        t = c.execute("SELECT * FROM templates WHERE id=?", (tpl_id,)).fetchone()
    n = accessible_number(t["number_id"]) if t else None
    if not n:
        return bad("Template not found", 404)
    ok, res = meta_request(n, "DELETE", f"{n['waba_id']}/message_templates", params={"name": t["meta_name"]})
    if not ok and "does not exist" not in meta_error(res).lower():
        return bad(meta_error(res))
    with db() as c:
        c.execute("DELETE FROM templates WHERE meta_name=? AND number_id=?", (t["meta_name"], n["id"]))
    return jsonify(deleted=True)


# ───────────────────────── automations API ─────────────────────────
TRIGGERS = ("keyword", "first_message", "first_after_24h", "any_message")


@app.get("/api/automations")
@api_auth(perm="automations")
def api_automations():
    n = accessible_number(request.args.get("number_id", type=int))
    if not n:
        return jsonify(automations=[])
    with db() as c:
        rows = c.execute("SELECT * FROM automations WHERE number_id=? ORDER BY id DESC", (n["id"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["graph"] = normalize_graph(json.loads(d.pop("graph_json") or "{}"))
        out.append(d)
    return jsonify(automations=out)


@app.post("/api/automations")
@api_auth(perm="automations")
def save_automation():
    d = request.get_json(silent=True) or {}
    n = accessible_number(int(d.get("number_id") or 0))
    if not n:
        return bad("Select a connected number")
    trig = d.get("trigger_type", "keyword")
    kw = ", ".join(k.strip() for k in (d.get("keyword") or "").split(",") if k.strip())
    mt = d.get("match_type") if d.get("match_type") in ("contains", "exact", "starts") else "contains"
    if trig not in TRIGGERS:
        return bad("Invalid trigger")
    if trig == "keyword" and not kw:
        return bad("Enter at least one keyword")
    graph, err = validate_graph(d.get("graph") or {}, n)
    if not graph:
        return bad(err)
    fid, name = d.get("id"), (d.get("name") or "New automation").strip()[:80]
    with db() as c:
        if fid:
            if not c.execute("SELECT 1 FROM automations WHERE id=? AND number_id=?", (fid, n["id"])).fetchone():
                return bad("Automation not found", 404)
            c.execute("""UPDATE automations SET name=?,trigger_type=?,keyword=?,match_type=?,enabled=?,graph_json=?,updated_at=?
                         WHERE id=?""", (name, trig, kw, mt, 1 if d.get("enabled", True) else 0, json.dumps(graph), now_iso(), fid))
    if not fid:
        e = limit_error(tenant_id(), "max_automations", "automations")
        if e:
            return e
        with db() as c:
            fid = c.execute("""INSERT INTO automations(number_id,name,trigger_type,keyword,match_type,enabled,graph_json,created_at,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (n["id"], name, trig, kw, mt, 1 if d.get("enabled", True) else 0, json.dumps(graph), now_iso(), now_iso())).lastrowid
    return jsonify(saved=True, id=fid)


@app.post("/api/automations/<int:flow_id>/toggle")
@api_auth(perm="automations")
def toggle_automation(flow_id):
    with db() as c:
        r = c.execute("SELECT * FROM automations WHERE id=?", (flow_id,)).fetchone()
    if not r or not accessible_number(r["number_id"]):
        return bad("Automation not found", 404)
    with db() as c:
        c.execute("UPDATE automations SET enabled=? WHERE id=?", (0 if r["enabled"] else 1, flow_id))
    return jsonify(enabled=not r["enabled"])


@app.delete("/api/automations/<int:flow_id>")
@api_auth(perm="automations")
def delete_automation(flow_id):
    with db() as c:
        r = c.execute("SELECT number_id FROM automations WHERE id=?", (flow_id,)).fetchone()
    if not r or not accessible_number(r["number_id"]):
        return bad("Automation not found", 404)
    with db() as c:
        c.execute("DELETE FROM automations WHERE id=?", (flow_id,))
    return jsonify(deleted=True)


# ───────────────────────── forms (WhatsApp Flows) ─────────────────────────
def clean_field_key(v):
    k = re.sub(r"[^a-zA-Z0-9_]", "_", str(v or "field").strip()).strip("_").lower()
    if not k or k[0].isdigit():
        k = "field_" + k
    return k[:32]


def build_meta_flow_json(form_name, screen_title, fields):
    children = [{"type": "TextHeading", "text": str(screen_title or form_name)[:80]}]
    payload, used = {}, set()
    for i, f in enumerate(fields):
        kind = f.get("type", "text")
        label = str(f.get("label") or f"Field {i + 1}").strip()[:20]
        key = clean_field_key(f.get("key") or label)
        while key in used:
            key = (key[:28] + f"_{i}")
        used.add(key)
        req = bool(f.get("required"))
        if kind in ("text", "email", "phone", "number"):
            comp = {"type": "TextInput", "name": key, "label": label, "required": req, "input-type": kind}
        elif kind == "date":
            comp = {"type": "DatePicker", "name": key, "label": label, "required": req}
        elif kind in ("dropdown", "radio", "checkbox"):
            choices, seen = [], set()
            for opt in (f.get("options") or [])[:20]:
                t = str(opt).strip()[:30]
                oid = clean_field_key(t)
                if t and oid not in seen:
                    seen.add(oid)
                    choices.append({"id": oid, "title": t})
            if len(choices) < 2:
                raise ValueError(f"{label}: add at least two different options")
            comp = {"type": {"dropdown": "Dropdown", "radio": "RadioButtonsGroup", "checkbox": "CheckboxGroup"}[kind],
                    "name": key, "label": label, "required": req, "data-source": choices}
        else:
            raise ValueError(f"Unsupported field type: {kind}")
        children.append(comp)
        payload[key] = "${form." + key + "}"
    if not fields:
        raise ValueError("Add at least one field")
    children.append({"type": "Footer", "label": "Submit", "on-click-action": {"name": "complete", "payload": payload}})
    return {"version": "7.3", "data_api_version": "3.0", "routing_model": {"CONTACT": []},
            "screens": [{"id": "CONTACT", "title": str(form_name)[:80], "terminal": True,
                         "layout": {"type": "SingleColumnLayout", "children": [{"type": "Form", "name": "lead_form", "children": children}]}}]}


@app.get("/api/forms")
@api_auth()
def api_forms():
    ids = user_number_ids()
    with db() as c:
        rows = c.execute("SELECT * FROM forms WHERE owner_id=? ORDER BY id DESC", (tenant_id(),)).fetchall()
    out = []
    for x in rows:
        if current_user()["role"] == "member" and x["number_id"] not in ids:
            continue
        out.append({"id": x["id"], "name": x["name"], "meta_flow_id": x["meta_flow_id"], "status": x["status"],
                    "category": x["category"], "cta_text": x["cta_text"], "prompt": x["prompt"], "number_id": x["number_id"],
                    "fields": json.loads(x["fields_json"] or "[]"), "created_at": x["created_at"]})
    return jsonify(forms=out)


@app.post("/api/forms")
@api_auth(perm="forms")
def save_form():
    d = request.get_json(silent=True) or {}
    n = accessible_number(int(d.get("number_id") or 0))
    name, fields = str(d.get("name") or "").strip()[:200], d.get("fields") or []
    cat = str(d.get("category") or "LEAD_GENERATION").upper()
    if not n:
        return bad("Select a connected WhatsApp number")
    if not name:
        return bad("Enter a form name")
    if cat not in {"SIGN_UP", "SIGN_IN", "APPOINTMENT_BOOKING", "LEAD_GENERATION", "SHOPPING", "CONTACT_US", "CUSTOMER_SUPPORT", "SURVEY", "OTHER"}:
        return bad("Invalid category")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
        return bad("Add between 1 and 20 fields")
    cta = str(d.get("cta_text") or "Continue").strip()
    if not cta or len(cta) > 20:
        return bad("Button text must be 1–20 characters")
    e = limit_error(tenant_id(), "max_forms", "forms")
    if e:
        return e
    try:
        flow_json = build_meta_flow_json(name, d.get("screen_title") or "A few details", fields)
    except ValueError as ex:
        return bad(str(ex))
    with db() as c:
        fid = c.execute("""INSERT INTO forms(name,fields_json,public_token,created_at,meta_flow_id,first_screen_id,cta_text,prompt,
                           number_id,status,category,screen_title,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name, json.dumps(fields, ensure_ascii=False), secrets.token_urlsafe(18), now_iso(), "", "CONTACT", cta,
                         str(d.get("prompt") or "Please share a few details so we can help you.").strip()[:1024], n["id"], "CREATING", cat,
                         str(d.get("screen_title") or "A few details")[:80], tenant_id())).lastrowid
    ok, created = meta_request(n, "POST", f"{n['waba_id']}/flows", data={"name": name, "categories": json.dumps([cat])})
    if not ok or not created.get("id"):
        with db() as c:
            c.execute("UPDATE forms SET status='ERROR' WHERE id=?", (fid,))
        return bad(meta_error(created), details=created, form_id=fid)
    meta_id = str(created["id"])
    ok, up = meta_request(n, "POST", f"{meta_id}/assets", data={"name": "flow.json", "asset_type": "FLOW_JSON"},
                          files={"file": ("flow.json", json.dumps(flow_json, ensure_ascii=False), "application/json")})
    errs = up.get("validation_errors") or up.get("validationErrors") or []
    status = "DRAFT" if ok and not errs else "INVALID"
    with db() as c:
        c.execute("UPDATE forms SET meta_flow_id=?,status=? WHERE id=?", (meta_id, status, fid))
    if not ok or errs:
        return bad(meta_error(up) if not ok else "Meta Flow validation failed", validation_errors=errs, form_id=fid)
    return jsonify(saved=True, id=fid, meta_flow_id=meta_id, status="DRAFT")


def form_for_user(form_id):
    with db() as c:
        r = c.execute("SELECT * FROM forms WHERE id=? AND owner_id=?", (form_id, tenant_id())).fetchone()
    return (dict(r), accessible_number(r["number_id"] or 0)) if r else (None, None)


@app.post("/api/forms/<int:form_id>/publish")
@api_auth(perm="forms")
def publish_form(form_id):
    f, n = form_for_user(form_id)
    if not f:
        return bad("Form not found", 404)
    if not n or not f["meta_flow_id"]:
        return bad("This form has no connected WhatsApp number or Meta Flow")
    if f["status"] == "PUBLISHED":
        return jsonify(published=True)
    ok, res = meta_request(n, "POST", f"{f['meta_flow_id']}/publish")
    if not ok:
        return bad(meta_error(res), details=res)
    with db() as c:
        c.execute("UPDATE forms SET status='PUBLISHED' WHERE id=?", (form_id,))
    return jsonify(published=True)


@app.delete("/api/forms/<int:form_id>")
@api_auth(perm="forms")
def delete_form(form_id):
    f, n = form_for_user(form_id)
    if not f:
        return bad("Form not found", 404)
    if f["status"] == "PUBLISHED":
        return bad("Published Meta Flows can't be deleted. Deprecate it in Meta Business Manager first.", 409)
    if n and f["meta_flow_id"]:
        ok, res = meta_request(n, "DELETE", f["meta_flow_id"])
        if not ok:
            return bad(meta_error(res))
    with db() as c:
        c.execute("DELETE FROM forms WHERE id=?", (form_id,))
    return jsonify(deleted=True)


# ───────────────────────── team ─────────────────────────
def clean_perms(p):
    return {k: bool((p or {}).get(k)) for k in MEMBER_PERMS}


def valid_member_numbers(ids, tid):
    own = {r["id"] for r in db_rows("SELECT id FROM numbers WHERE owner_id=?", (tid,))}
    return [int(i) for i in (ids or []) if int(i) in own]


def db_rows(sql, args=()):
    with db() as c:
        return c.execute(sql, args).fetchall()


def ser_member(r):
    d = ser_user(r)
    d["permissions"] = clean_perms(json.loads(d.pop("permissions_json") or "{}"))
    d["number_ids"] = [x["number_id"] for x in db_rows("SELECT number_id FROM member_numbers WHERE user_id=?", (d["id"],))]
    return d


@app.get("/api/team")
@api_auth(roles=("admin", "client"))
def team_list():
    rows = db_rows("SELECT * FROM users WHERE owner_id=? AND role='member' ORDER BY id DESC", (tenant_id(),))
    return jsonify(members=[ser_member(r) for r in rows])


@app.post("/api/team")
@api_auth(roles=("admin", "client"))
def team_create():
    d, tid = request.get_json(silent=True) or {}, tenant_id()
    e = limit_error(tid, "max_members", "team members")
    if e:
        return e
    nums = valid_member_numbers(d.get("number_ids"), tid)
    if not nums:
        return bad("Assign at least one WhatsApp number to this team member")
    uid, err = create_user("member", d.get("name", ""), d.get("email", ""), d.get("phone", ""), d.get("password", ""),
                           "active", owner_id=tid, perms=clean_perms(d.get("permissions")), created_by=current_user()["id"])
    if not uid:
        return bad(err)
    with db() as c:
        c.executemany("INSERT OR IGNORE INTO member_numbers(user_id,number_id) VALUES(?,?)", [(uid, n) for n in nums])
    return jsonify(created=True, id=uid), 201


def own_member(mid):
    r = db_rows("SELECT * FROM users WHERE id=? AND owner_id=? AND role='member'", (mid, tenant_id()))
    return r[0] if r else None


@app.patch("/api/team/<int:mid>")
@api_auth(roles=("admin", "client"))
def team_update(mid):
    m, d = own_member(mid), request.get_json(silent=True) or {}
    if not m:
        return bad("Team member not found", 404)
    sets, args = [], []
    if d.get("name"):
        sets.append("name=?"); args.append(str(d["name"]).strip()[:80])
    if d.get("phone"):
        ph = digits(d["phone"])
        if not 7 <= len(ph) <= 15:
            return bad("Enter a valid phone number")
        if db_rows("SELECT 1 FROM users WHERE phone=? AND id!=?", (ph, mid)):
            return bad("That phone number is already registered")
        sets.append("phone=?"); args.append(ph)
    if d.get("status") in ("active", "disabled"):
        sets.append("status=?"); args.append(d["status"])
    if d.get("password"):
        if len(d["password"]) < 8:
            return bad("Password must be at least 8 characters")
        sets.append("password_hash=?"); args.append(generate_password_hash(d["password"]))
    if "permissions" in d:
        sets.append("permissions_json=?"); args.append(json.dumps(clean_perms(d["permissions"])))
    with db() as c:
        if sets:
            c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", args + [mid])
        if "number_ids" in d:
            nums = valid_member_numbers(d["number_ids"], tenant_id())
            if not nums:
                return bad("Assign at least one WhatsApp number")
            c.execute("DELETE FROM member_numbers WHERE user_id=?", (mid,))
            c.executemany("INSERT OR IGNORE INTO member_numbers(user_id,number_id) VALUES(?,?)", [(mid, n) for n in nums])
    return jsonify(ok=True)


@app.delete("/api/team/<int:mid>")
@api_auth(roles=("admin", "client"))
def team_delete(mid):
    if not own_member(mid):
        return bad("Team member not found", 404)
    with db() as c:
        c.execute("DELETE FROM users WHERE id=?", (mid,))
    return jsonify(deleted=True)


# ───────────────────────── admin: clients, plans, payments ─────────────────────────
@app.get("/api/admin/clients")
@api_auth(roles=("admin",), need_plan=False)
def admin_clients():
    rows = db_rows("""SELECT u.*, p.name plan_name,
        (SELECT COUNT(*) FROM numbers n WHERE n.owner_id=u.id) numbers_count,
        (SELECT COUNT(*) FROM users m WHERE m.owner_id=u.id AND m.role='member') members_count
        FROM users u LEFT JOIN plans p ON p.id=u.plan_id WHERE u.role='client' ORDER BY u.id DESC""")
    out = []
    for r in rows:
        d = ser_user(r)
        d.pop("permissions_json", None)
        exp = parse_iso(d["plan_expires_at"])
        d["plan_active"] = bool(d["plan_id"] and exp and exp > now_utc())
        out.append(d)
    return jsonify(clients=out)


@app.post("/api/admin/clients")
@api_auth(roles=("admin",), need_plan=False)
def admin_create_client():
    d = request.get_json(silent=True) or {}
    plan = get_plan(int(d.get("plan_id") or 0)) if d.get("plan_id") else None
    uid, err = create_user("client", d.get("name", ""), d.get("email", ""), d.get("phone", ""), d.get("password", ""),
                           "active", business=d.get("business", ""), created_by=current_user()["id"], plan=plan)
    return (jsonify(created=True, id=uid), 201) if uid else bad(err)


def client_row(cid):
    r = db_rows("SELECT * FROM users WHERE id=? AND role='client'", (cid,))
    return r[0] if r else None


@app.post("/api/admin/clients/<int:cid>/action")
@api_auth(roles=("admin",), need_plan=False)
def admin_client_action(cid):
    d, row = request.get_json(silent=True) or {}, client_row(cid)
    if not row:
        return bad("Client not found", 404)
    act = d.get("action")
    status = {"approve": "active", "reject": "rejected", "disable": "disabled", "enable": "active"}.get(act)
    if not status:
        return bad("Unknown action")
    with db() as c:
        c.execute("UPDATE users SET status=? WHERE id=?", (status, cid))
    if act == "approve" and d.get("plan_id"):
        plan = get_plan(int(d["plan_id"]))
        if plan:
            apply_plan(cid, plan)
    return jsonify(ok=True)


@app.post("/api/admin/clients/<int:cid>/plan")
@api_auth(roles=("admin",), need_plan=False)
def admin_assign_plan(cid):
    d = request.get_json(silent=True) or {}
    if not client_row(cid):
        return bad("Client not found", 404)
    if not d.get("plan_id"):
        with db() as c:
            c.execute("UPDATE users SET plan_id=NULL,plan_expires_at=NULL WHERE id=?", (cid,))
        return jsonify(ok=True)
    plan = get_plan(int(d["plan_id"]))
    if not plan:
        return bad("Plan not found", 404)
    if d.get("expires_at"):
        exp = parse_iso(d["expires_at"] + "T23:59:59") if len(d["expires_at"]) == 10 else parse_iso(d["expires_at"])
        if not exp:
            return bad("Invalid date")
        with db() as c:
            c.execute("UPDATE users SET plan_id=?,plan_expires_at=? WHERE id=?", (plan["id"], exp.isoformat(), cid))
        return jsonify(ok=True, expires_at=exp.isoformat())
    return jsonify(ok=True, expires_at=apply_plan(cid, plan, d.get("days")))


@app.post("/api/admin/clients/<int:cid>/password")
@api_auth(roles=("admin",), need_plan=False)
def admin_reset_password(cid):
    pw = (request.get_json(silent=True) or {}).get("password") or ""
    if not client_row(cid):
        return bad("Client not found", 404)
    if len(pw) < 8:
        return bad("Password must be at least 8 characters")
    with db() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), cid))
    return jsonify(ok=True)


@app.delete("/api/admin/clients/<int:cid>")
@api_auth(roles=("admin",), need_plan=False)
def admin_delete_client(cid):
    if not client_row(cid):
        return bad("Client not found", 404)
    files = [r["stored_name"] for r in db_rows("SELECT stored_name FROM media WHERE owner_id=?", (cid,))]
    with db() as c:
        c.execute("DELETE FROM forms WHERE owner_id=?", (cid,))
        c.execute("DELETE FROM media WHERE owner_id=?", (cid,))
        c.execute("DELETE FROM numbers WHERE owner_id=?", (cid,))
        c.execute("DELETE FROM payments WHERE user_id=?", (cid,))
        c.execute("DELETE FROM users WHERE owner_id=? AND role='member'", (cid,))
        c.execute("DELETE FROM users WHERE id=?", (cid,))
    for f in files:
        try:
            os.remove(os.path.join(MEDIA_DIR, f))
        except OSError:
            pass
    return jsonify(deleted=True)


@app.get("/api/plans")
@api_auth(need_plan=False)
def plans_list():
    all_ = request.args.get("all") == "1" and current_user()["role"] == "admin"
    rows = db_rows("SELECT * FROM plans" + ("" if all_ else " WHERE active=1") + " ORDER BY price")
    return jsonify(plans=[ser_plan(r) for r in rows])


def plan_fields(d):
    try:
        price = float(d.get("price"))
        days = int(d.get("duration_days"))
        lims = {k: max(0, int(d.get(k) or 0)) for k in LIMIT_KEYS}
    except (TypeError, ValueError):
        return None, "Price, duration and limits must be numbers"
    if not (d.get("name") or "").strip():
        return None, "Enter a plan name"
    if price < 0 or days < 1:
        return None, "Price must be 0 or more and duration at least 1 day"
    feats = d.get("features") or []
    if isinstance(feats, str):
        feats = [x.strip() for x in feats.splitlines() if x.strip()]
    return {"name": d["name"].strip()[:60], "description": str(d.get("description") or "")[:200], "price": price,
            "duration_days": days, "features_json": json.dumps(feats[:12]), "active": 1 if d.get("active", True) else 0, **lims}, ""


@app.post("/api/admin/plans")
@api_auth(roles=("admin",), need_plan=False)
def plan_create():
    f, err = plan_fields(request.get_json(silent=True) or {})
    if not f:
        return bad(err)
    with db() as c:
        pid = c.execute("""INSERT INTO plans(name,description,price,duration_days,max_numbers,max_members,max_automations,max_forms,
                           features_json,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (f["name"], f["description"], f["price"], f["duration_days"], f["max_numbers"], f["max_members"],
                         f["max_automations"], f["max_forms"], f["features_json"], f["active"], now_iso())).lastrowid
    return jsonify(id=pid), 201


@app.patch("/api/admin/plans/<int:pid>")
@api_auth(roles=("admin",), need_plan=False)
def plan_update(pid):
    d = request.get_json(silent=True) or {}
    old = get_plan(pid)
    if not old:
        return bad("Plan not found", 404)
    f, err = plan_fields({**old, "features": json.loads(old["features_json"] or "[]"), **d})
    if not f:
        return bad(err)
    with db() as c:
        c.execute("""UPDATE plans SET name=?,description=?,price=?,duration_days=?,max_numbers=?,max_members=?,max_automations=?,
                     max_forms=?,features_json=?,active=? WHERE id=?""",
                  (f["name"], f["description"], f["price"], f["duration_days"], f["max_numbers"], f["max_members"],
                   f["max_automations"], f["max_forms"], f["features_json"], f["active"], pid))
    return jsonify(ok=True)


@app.delete("/api/admin/plans/<int:pid>")
@api_auth(roles=("admin",), need_plan=False)
def plan_delete(pid):
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE plan_id=?", (pid,)).fetchone() or c.execute("SELECT 1 FROM payments WHERE plan_id=?", (pid,)).fetchone():
            c.execute("UPDATE plans SET active=0 WHERE id=?", (pid,))
            return jsonify(archived=True)
        c.execute("DELETE FROM plans WHERE id=?", (pid,))
    return jsonify(deleted=True)


@app.get("/api/admin/payments")
@api_auth(roles=("admin",), need_plan=False)
def admin_payments():
    rows = db_rows("""SELECT pay.*,u.name user_name,u.email user_email,p.name plan_name FROM payments pay
                      JOIN users u ON u.id=pay.user_id JOIN plans p ON p.id=pay.plan_id ORDER BY pay.id DESC LIMIT 300""")
    return jsonify(payments=[dict(r) for r in rows])


# ───────────────────────── billing (Razorpay) ─────────────────────────
def rzp(method, path, **kw):
    try:
        r = requests.request(method, "https://api.razorpay.com/v1/" + path, auth=(RZP_KEY_ID, RZP_KEY_SECRET), timeout=20, **kw)
        return r.status_code < 400, r.json()
    except (requests.RequestException, ValueError) as e:
        return False, {"error": {"description": str(e)}}


@app.post("/api/billing/order")
@api_auth(roles=("client",), need_plan=False)
def billing_order():
    if not (RZP_KEY_ID and RZP_KEY_SECRET):
        return bad("Online payments are not configured yet. Please contact the admin.", 503)
    plan = get_plan(int((request.get_json(silent=True) or {}).get("plan_id") or 0))
    if not plan or not plan["active"]:
        return bad("Plan not found", 404)
    u = current_user()
    amount = int(round(plan["price"] * 100))
    if amount < 100:
        return bad("This plan has no payable price. Ask the admin to assign it.")
    ok, o = rzp("POST", "orders", json={"amount": amount, "currency": "INR", "receipt": f"dm{u['id']}p{plan['id']}t{int(time.time())}",
                                        "notes": {"user_id": str(u["id"]), "plan_id": str(plan["id"])}})
    if not ok:
        return bad(((o.get("error") or {}).get("description")) or "Could not start the payment")
    with db() as c:
        c.execute("INSERT INTO payments(user_id,plan_id,amount,rzp_order_id,status,created_at) VALUES(?,?,?,?,?,?)",
                  (u["id"], plan["id"], plan["price"], o["id"], "created", now_iso()))
    return jsonify(order_id=o["id"], amount=amount, currency="INR", key_id=RZP_KEY_ID, description=f"{plan['name']} plan · {plan['duration_days']} days")


def settle_payment(order_id, payment_id):
    """Idempotent: marks a payment paid and activates the plan exactly once."""
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE rzp_order_id=?", (order_id,)).fetchone()
        if not p:
            return None
        if p["status"] == "paid":
            return {"already": True, "user_id": p["user_id"]}
        c.execute("UPDATE payments SET status='paid',rzp_payment_id=?,paid_at=? WHERE id=?", (payment_id, now_iso(), p["id"]))
    plan = get_plan(p["plan_id"])
    exp = apply_plan(p["user_id"], plan)
    return {"plan": plan["name"], "expires_at": exp, "user_id": p["user_id"]}


@app.post("/api/billing/verify")
@api_auth(roles=("client",), need_plan=False)
def billing_verify():
    d = request.get_json(silent=True) or {}
    oid, pid, sig = d.get("razorpay_order_id", ""), d.get("razorpay_payment_id", ""), d.get("razorpay_signature", "")
    exp = hmac.new(RZP_KEY_SECRET.encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest()
    if not (oid and pid and sig) or not hmac.compare_digest(exp, sig):
        return bad("Payment verification failed. If money was deducted, contact the admin.")
    rows = db_rows("SELECT user_id FROM payments WHERE rzp_order_id=?", (oid,))
    if not rows or rows[0]["user_id"] != current_user()["id"]:
        return bad("Order not found", 404)
    res = settle_payment(oid, pid)
    info = plan_info(current_user()["id"])
    return jsonify(ok=True, plan=info["name"], expires_at=info["expires_at"])


@app.post("/webhooks/razorpay")
def razorpay_webhook():
    raw = request.get_data()
    if not RZP_WEBHOOK_SECRET:
        return jsonify(ignored=True), 200
    exp = hmac.new(RZP_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(exp, request.headers.get("X-Razorpay-Signature", "")):
        return bad("Invalid signature", 400)
    try:
        ev = json.loads(raw.decode())
        ent = ev["payload"]["payment"]["entity"]
        if ev.get("event") in ("payment.captured", "order.paid"):
            settle_payment(ent.get("order_id", ""), ent.get("id", ""))
    except (ValueError, KeyError, TypeError):
        pass
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Upload too large"), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8822")), debug=False)