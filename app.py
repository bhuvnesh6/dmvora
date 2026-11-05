import os, re, json, hmac, hashlib, sqlite3, secrets, time, threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, abort

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(DATA_DIR, "dmvora.sqlite3"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "")
if len(app.secret_key) < 32:
    raise RuntimeError("Set a random SECRET_KEY (at least 32 characters) in .env")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
META_APP_ID = os.getenv("META_APP_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_ES_CONFIG_ID = os.getenv("META_ES_CONFIG_ID", "").strip()
META_ES_VERIFY_TOKEN = os.getenv("META_ES_VERIFY_TOKEN", "").strip()
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0").strip()
META_GRAPH = "https://graph.facebook.com"
PRAVAH_WEBHOOK_SECRET = os.getenv("PRAVAH_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://dmvora.sanjivanitechno.com").rstrip("/")
WEBHOOK_MAX_SKEW = int(os.getenv("WEBHOOK_MAX_SKEW", "300"))

def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS numbers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, phone_number_id TEXT UNIQUE NOT NULL,
          waba_id TEXT NOT NULL, business_id TEXT DEFAULT '', display_phone TEXT DEFAULT '',
          verified_name TEXT DEFAULT '', quality TEXT DEFAULT '', access_token TEXT NOT NULL,
          api_version TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          phone TEXT NOT NULL, name TEXT DEFAULT '', email TEXT DEFAULT '',
          status TEXT DEFAULT 'new', source TEXT DEFAULT 'whatsapp',
          form_data TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(number_id, phone), FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL, lead_id INTEGER,
          wa_message_id TEXT UNIQUE, direction TEXT NOT NULL, message_type TEXT DEFAULT 'text',
          body TEXT DEFAULT '', media_id TEXT DEFAULT '', media_url TEXT DEFAULT '',
          status TEXT DEFAULT 'received', raw_json TEXT DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE,
          FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(number_id, lead_id, id);
        CREATE TABLE IF NOT EXISTS automations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          name TEXT NOT NULL, trigger_type TEXT NOT NULL, keyword TEXT DEFAULT '',
          enabled INTEGER DEFAULT 1, graph_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS forms(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          fields_json TEXT NOT NULL DEFAULT '[]', public_token TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS templates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
          meta_name TEXT NOT NULL, language TEXT DEFAULT 'en_US', category TEXT DEFAULT '',
          status TEXT DEFAULT 'UNKNOWN', components_json TEXT DEFAULT '[]',
          UNIQUE(number_id, meta_name, language),
          FOREIGN KEY(number_id) REFERENCES numbers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS event_dedup(
          event_key TEXT PRIMARY KEY, created_at TEXT NOT NULL
        );
        """)
        # Backward-compatible Meta WhatsApp Flow columns.
        form_columns = {row["name"] for row in c.execute("PRAGMA table_info(forms)").fetchall()}
        for column, definition in (
            ("meta_flow_id", "TEXT NOT NULL DEFAULT ''"),
            ("first_screen_id", "TEXT NOT NULL DEFAULT 'WELCOME'"),
            ("cta_text", "TEXT NOT NULL DEFAULT 'Open form'"),
            ("prompt", "TEXT NOT NULL DEFAULT 'Please complete this form.'"),
        ):
            if column not in form_columns:
                c.execute(f"ALTER TABLE forms ADD COLUMN {column} {definition}")
        # DMvora-native Flow Studio metadata. Existing installations migrate in place.
        form_columns = {row["name"] for row in c.execute("PRAGMA table_info(forms)").fetchall()}
        for column, definition in (
            ("number_id", "INTEGER"),
            ("status", "TEXT NOT NULL DEFAULT 'LOCAL'"),
            ("category", "TEXT NOT NULL DEFAULT 'LEAD_GENERATION'"),
            ("screen_title", "TEXT NOT NULL DEFAULT 'A few details'"),
        ):
            if column not in form_columns:
                c.execute(f"ALTER TABLE forms ADD COLUMN {column} {definition}")
init_db()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def digits(value):
    return re.sub(r"\D", "", str(value or ""))

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            if request.path.startswith("/api/") or request.path.startswith("/internal/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper

def meta_request(number, method, path, **kwargs):
    url = f"{META_GRAPH}/{number['api_version']}/{str(path).lstrip('/')}"
    try:
        r = requests.request(method, url, headers={"Authorization": f"Bearer {number['access_token']}"},
                             timeout=25, **kwargs)
        data = r.json() if r.content else {}
    except requests.RequestException as e:
        return False, {"error": {"message": str(e)}}
    if r.status_code >= 400:
        return False, data
    return True, data

def meta_error(data):
    return ((data or {}).get("error") or {}).get("message") or "Meta API request failed"

def get_number(number_id):
    with db() as c:
        row = c.execute("SELECT * FROM numbers WHERE id=? AND active=1", (number_id,)).fetchone()
    return dict(row) if row else None

def number_by_phone_id(pid):
    with db() as c:
        row = c.execute("SELECT * FROM numbers WHERE phone_number_id=? AND active=1", (str(pid),)).fetchone()
    return dict(row) if row else None

def upsert_lead(number_id, phone, name=""):
    phone = digits(phone)
    if not phone:
        return None, False
    with db() as c:
        old = c.execute("SELECT * FROM leads WHERE number_id=? AND phone=?", (number_id, phone)).fetchone()
        if old:
            if name and (not old["name"] or old["name"] == old["phone"]):
                c.execute("UPDATE leads SET name=?,updated_at=? WHERE id=?", (name, now_iso(), old["id"]))
            row = c.execute("SELECT * FROM leads WHERE id=?", (old["id"],)).fetchone()
            return dict(row), False
        cur = c.execute("""INSERT INTO leads(number_id,phone,name,created_at,updated_at)
                           VALUES(?,?,?,?,?)""", (number_id, phone, name or phone, now_iso(), now_iso()))
        row = c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row), True

def message_text(m):
    typ = m.get("type", "unknown")
    if typ == "text":
        return (m.get("text") or {}).get("body", "")
    if typ == "button":
        return (m.get("button") or {}).get("text", "")
    if typ == "interactive":
        i = m.get("interactive") or {}
        reply_title = ((i.get("button_reply") or {}).get("title") or
                       (i.get("list_reply") or {}).get("title"))
        if reply_title:
            return reply_title
        nfm = i.get("nfm_reply") or {}
        response = nfm.get("response_json") or {}
        try:
            response = json.loads(response) if isinstance(response, str) else response
        except (TypeError, ValueError):
            return "WhatsApp Flow form submitted"
        if isinstance(response, dict):
            response.pop("flow_token", None)
            details = [f"{key.replace('_', ' ').title()}: {value}" for key, value in response.items()]
            return "Form submitted\n" + "\n".join(details) if details else "WhatsApp Flow form submitted"
        return "WhatsApp Flow form submitted"
    obj = m.get(typ) or {}
    return obj.get("caption") or f"[{typ}]"

def media_id_from(m):
    typ = m.get("type", "")
    return str((m.get(typ) or {}).get("id") or "")

def save_message(number_id, lead_id, wamid, direction, typ, body, raw, media_id="", media_url=""):
    with db() as c:
        if wamid:
            exists = c.execute("SELECT id FROM messages WHERE wa_message_id=?", (wamid,)).fetchone()
            if exists:
                return False
        c.execute("""INSERT INTO messages(number_id,lead_id,wa_message_id,direction,message_type,
                     body,media_id,media_url,status,raw_json,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (number_id, lead_id, wamid or None, direction, typ, body or "", media_id,
                   media_url, "received" if direction == "in" else "sent",
                   json.dumps(raw or {}, ensure_ascii=False), now_iso()))
    return True

def send_payload(number, payload):
    ok, data = meta_request(number, "POST", f"{number['phone_number_id']}/messages", json=payload)
    if not ok:
        return {"success": False, "error": meta_error(data), "details": data}
    msg_id = ((data.get("messages") or [{}])[0]).get("id", "")
    return {"success": True, "message_id": msg_id, "response": data}

def send_text(number, to, body):
    return send_payload(number, {"messaging_product":"whatsapp","recipient_type":"individual",
        "to":digits(to),"type":"text","text":{"preview_url":True,"body":str(body)[:4096]}})

def send_interactive(number, to, body, buttons, image_url=""):
    btns = [{"type":"reply","reply":{"id":b["id"],"title":b["title"][:20]}} for b in buttons[:3]]
    interactive = {"type":"button","body":{"text":body[:1024]},
                   "action":{"buttons":btns}}
    if image_url:
        interactive["header"] = {"type":"image","image":{"link":image_url}}
    return send_payload(number, {"messaging_product":"whatsapp","recipient_type":"individual",
        "to":digits(to),"type":"interactive","interactive":interactive})

def send_image(number, to, url, caption=""):
    return send_payload(number, {"messaging_product":"whatsapp","recipient_type":"individual",
        "to":digits(to),"type":"image","image":{"link":url,"caption":caption[:1024]}})

def store_outgoing(number, lead, result, typ, body, raw=None, media_url=""):
    if result.get("success"):
        save_message(number["id"], lead["id"], result.get("message_id",""), "out", typ,
                     body, raw or result.get("response",{}), "", media_url)
    return result

def send_whatsapp_flow(number, lead, form, automation_id="manual"):
    flow_token = f"dmvora:{number['id']}:{lead['id']}:{form['id']}:{secrets.token_urlsafe(8)}"
    params = {
        "flow_message_version": "3",
        "flow_token": flow_token,
        "flow_id": str(form["meta_flow_id"]),
        "flow_cta": (form["cta_text"] or "Open form")[:20],
        "flow_action": "navigate",
        "flow_action_payload": {"screen": form["first_screen_id"] or "WELCOME"}
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": digits(lead["phone"]),
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": (form["prompt"] or "Please complete this form.")[:1024]},
            "action": {"name": "flow", "parameters": params}
        }
    }
    result = send_payload(number, payload)
    return store_outgoing(number, lead, result, "flow", form["prompt"], {
        "flow_id": form["meta_flow_id"], "flow_token": flow_token
    })

def send_node(number, lead, node, flow_id):
    data = node.get("data") or {}
    kind = node.get("type")
    buttons = []
    for i, b in enumerate(data.get("buttons") or []):
        bid = f"dmv:{flow_id}:{node.get('id')}:{i}"
        buttons.append({"id":bid, "title":(b.get("title") or f"Option {i+1}")})
    if kind == "form":
        form_id = int(data.get("form_id") or 0)
        with db() as c:
            form = c.execute("SELECT * FROM forms WHERE id=? AND (number_id=? OR number_id IS NULL)", (form_id, number["id"])).fetchone()
        if not form or not form["meta_flow_id"]:
            return {"success": False, "error": "Select a saved Meta WhatsApp Flow first"}
        return send_whatsapp_flow(number, lead, dict(form), flow_id)
    body = data.get("body") or ""
    image_url = data.get("image_url") or ""
    if buttons:
        result = send_interactive(number, lead["phone"], body or "Please choose an option.", buttons, image_url)
        return store_outgoing(number, lead, result, "interactive", body, {"buttons":buttons,"image_url":image_url})
    if kind == "image" and image_url:
        result = send_image(number, lead["phone"], image_url, body)
        return store_outgoing(number, lead, result, "image", body, result.get("response",{}), image_url)
    result = send_text(number, lead["phone"], body)
    return store_outgoing(number, lead, result, "text", body)

def execute_node(number, lead, graph, node_id, flow_id, depth=0):
    if depth > 12:
        return
    nodes = graph.get("nodes") or []
    node = next((n for n in nodes if str(n.get("id")) == str(node_id)), None)
    if not node:
        return
    result = send_node(number, lead, node, flow_id)
    if not result.get("success"):
        app.logger.warning("DMvora automation send failed: %s", result.get("error"))
        return
    # A WhatsApp Flow node must pause until the customer submits the form.
    # Message/image nodes with reply buttons also wait for the customer's choice.
    if node.get("type") == "form":
        return
    if node.get("type") not in ("message", "image") or not (node.get("data") or {}).get("buttons"):
        nxt = (node.get("data") or {}).get("next_id")
        if nxt:
            execute_node(number, lead, graph, nxt, flow_id, depth+1)

def run_automations(number, lead, text, raw_message):
    with db() as c:
        flows = c.execute("SELECT * FROM automations WHERE number_id=? AND enabled=1",
                          (number["id"],)).fetchall()
        last = c.execute("""SELECT created_at FROM messages WHERE number_id=? AND lead_id=?
                            AND direction='in' ORDER BY id DESC LIMIT 2""",
                         (number["id"],lead["id"])).fetchall()
    previous = None
    if len(last) > 1:
        try: previous = datetime.fromisoformat(last[1]["created_at"])
        except Exception: pass
    is_first_after_24h = previous is None or (datetime.now(timezone.utc)-previous).total_seconds() >= 86400
    for f in flows:
        if f["trigger_type"] == "keyword":
            if f["keyword"].lower() not in (text or "").lower():
                continue
        elif f["trigger_type"] == "first_after_24h":
            if not is_first_after_24h:
                continue
        else:
            continue
        graph = json.loads(f["graph_json"] or "{}")
        trigger = next((n for n in graph.get("nodes",[]) if n.get("type")=="trigger"),None)
        start = ((trigger or {}).get("data") or {}).get("next_id")
        if start:
            execute_node(number, lead, graph, start, f["id"])

def process_meta_payload(payload):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            pid = str(metadata.get("phone_number_id") or "")
            number = number_by_phone_id(pid)
            # Strict tenant boundary: unknown number IDs are acknowledged and ignored.
            if not number:
                app.logger.info("Ignoring Meta event for unconnected phone_number_id=%s", pid)
                continue
            names = {str(x.get("wa_id")):(x.get("profile") or {}).get("name","")
                     for x in (value.get("contacts") or [])}
            for m in value.get("messages") or []:
                from_phone = digits(m.get("from"))
                wamid = m.get("id","")
                if not from_phone:
                    continue
                lead, _ = upsert_lead(number["id"], from_phone, names.get(from_phone,""))
                body = message_text(m)
                typ = m.get("type","unknown")
                inserted = save_message(number["id"], lead["id"], wamid, "in", typ, body,
                                        m, media_id=media_id_from(m))
                if inserted:
                    if typ == "interactive" and (m.get("interactive") or {}).get("type") == "nfm_reply":
                        nfm = (m.get("interactive") or {}).get("nfm_reply") or {}
                        response_json = nfm.get("response_json") or "{}"
                        try:
                            submitted = json.loads(response_json) if isinstance(response_json, str) else response_json
                        except (TypeError, ValueError):
                            submitted = {"raw_response": str(response_json)}
                        token = str(submitted.pop("flow_token", "") or "")
                        token_match = re.fullmatch(r"dmvora:(\d+):(\d+):(\d+):[^:]+", token)
                        if token_match:
                            token_number, token_lead, token_form = map(int, token_match.groups())
                            if token_number == number["id"] and token_lead == lead["id"]:
                                with db() as c:
                                    form = c.execute("SELECT id FROM forms WHERE id=? AND (number_id=? OR number_id IS NULL)", (token_form, number["id"])).fetchone()
                                    if form:
                                        old_lead = c.execute("SELECT form_data FROM leads WHERE id=? AND number_id=?", (lead["id"], number["id"])).fetchone()
                                        existing = json.loads(old_lead["form_data"] or "{}") if old_lead else {}
                                        existing.update(submitted)
                                        c.execute("UPDATE leads SET form_data=?,updated_at=? WHERE id=? AND number_id=?",
                                                  (json.dumps(existing, ensure_ascii=False), now_iso(), lead["id"], number["id"]))
                        app.logger.info("WhatsApp Flow response received for lead=%s", lead["id"])
                    elif typ == "interactive":
                        reply = ((m.get("interactive") or {}).get("button_reply") or {})
                        bid = reply.get("id","")
                        match = re.fullmatch(r"dmv:(\d+):([^:]+):(\d+)", bid)
                        if match:
                            flow_id, node_id, idx = match.groups()
                            with db() as c:
                                f = c.execute("SELECT * FROM automations WHERE id=? AND number_id=?",
                                              (flow_id,number["id"])).fetchone()
                            if f:
                                graph = json.loads(f["graph_json"] or "{}")
                                node = next((n for n in graph.get("nodes",[]) if str(n.get("id"))==node_id),None)
                                buttons = ((node or {}).get("data") or {}).get("buttons") or []
                                if int(idx) < len(buttons):
                                    action = buttons[int(idx)]
                                    if action.get("action") == "form":
                                        form_node = {"type":"form","data":{"form_id":action.get("form_id"),
                                            "prompt":action.get("prompt") or "Please complete this form in WhatsApp."}}
                                        send_node(number,lead,form_node,flow_id)
                                    elif action.get("action") == "url":
                                        url = action.get("url","")
                                        send_text(number,from_phone,f"Here is the link: {url}")
                                        save_message(number["id"],lead["id"],"", "out","text",
                                                     f"Here is the link: {url}",{"automation":"url_button"})
                                    elif action.get("next_id"):
                                        execute_node(number,lead,graph,action["next_id"],flow_id)
                    else:
                        run_automations(number, lead, body, m)
            for status in value.get("statuses") or []:
                wamid = status.get("id")
                if wamid:
                    with db() as c:
                        c.execute("UPDATE messages SET status=? WHERE wa_message_id=?",
                                  (status.get("status","unknown"),wamid))

def verify_internal_signature(raw):
    if not PRAVAH_WEBHOOK_SECRET:
        return False
    ts = request.headers.get("X-Pravah-Timestamp","")
    sig = request.headers.get("X-Pravah-Signature","")
    try:
        if abs(int(time.time())-int(ts)) > WEBHOOK_MAX_SKEW:
            return False
    except (ValueError,TypeError):
        return False
    expected = hmac.new(PRAVAH_WEBHOOK_SECRET.encode(), ts.encode()+b"."+raw,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

@app.get("/login")
def login():
    if session.get("logged_in"): return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.post("/login")
def login_post():
    username = request.form.get("username","")
    password = request.form.get("password","")
    if not ADMIN_PASSWORD:
        flash("ADMIN_PASSWORD is missing in the server .env file.", "error")
        return redirect(url_for("login"))
    if hmac.compare_digest(username,ADMIN_USERNAME) and hmac.compare_digest(password,ADMIN_PASSWORD):
        session.clear(); session["logged_in"]=True; session["username"]=ADMIN_USERNAME
        session.permanent=True
        return redirect(url_for("dashboard"))
    flash("Incorrect username or password.", "error")
    return redirect(url_for("login"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/")
@login_required
def dashboard():
    return redirect(url_for("workspace"))

@app.get("/app")
@login_required
def workspace():
    return render_template("app.html", username=session.get("username","Admin"))

@app.get("/health")
def health():
    return jsonify(status="ok", service="dmvora", time=now_iso())

@app.get("/api/meta/config")
@login_required
def meta_config():
    return jsonify(ready=bool(META_APP_ID and META_APP_SECRET and META_ES_CONFIG_ID),
                   app_id=META_APP_ID, config_id=META_ES_CONFIG_ID,
                   api_version=META_GRAPH_VERSION)

@app.post("/api/meta/complete")
@login_required
def meta_complete():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    waba_id, pid = digits(data.get("waba_id")), digits(data.get("phone_number_id"))
    business_id = digits(data.get("business_id"))
    if not code: return jsonify(error="Meta authorization code missing"),400
    if not (META_APP_ID and META_APP_SECRET): return jsonify(error="Meta app credentials missing"),500
    try:
        r=requests.get(f"{META_GRAPH}/{META_GRAPH_VERSION}/oauth/access_token",
            params={"client_id":META_APP_ID,"client_secret":META_APP_SECRET,"code":code},timeout=25)
        token_data=r.json()
    except requests.RequestException as e:
        return jsonify(error=str(e)),502
    if r.status_code>=400 or not token_data.get("access_token"):
        return jsonify(error=meta_error(token_data)),400
    token=token_data["access_token"]
    # Same fallback approach used by PravahAI if the browser message did not include IDs.
    if not (waba_id and pid):
        try:
            d=requests.get(f"{META_GRAPH}/{META_GRAPH_VERSION}/debug_token",
                params={"input_token":token,"access_token":f"{META_APP_ID}|{META_APP_SECRET}"},timeout=20).json()
            granular=((d.get("data") or {}).get("granular_scopes") or [])
            for item in granular:
                target=(item.get("target_ids") or [])
                if target and not waba_id: waba_id=digits(target[0])
        except Exception: pass
    if not waba_id:
        return jsonify(error="Meta did not return a WhatsApp Business Account ID. Finish Embedded Signup and retry."),400
    # Subscribe app to the WABA. The Meta callback URL remains PravahAI's one app-level webhook.
    ok, sub = meta_request({"access_token":token,"api_version":META_GRAPH_VERSION},"POST",f"{waba_id}/subscribed_apps")
    if not ok:
        return jsonify(error=f"Could not subscribe this WABA: {meta_error(sub)}"),400
    # When phone_number_id is absent, discover all phone numbers belonging to this WABA.
    phone_rows=[]
    if pid:
        phone_rows=[{"id":pid}]
    else:
        ok, listing=meta_request({"access_token":token,"api_version":META_GRAPH_VERSION},
                                 "GET",f"{waba_id}/phone_numbers",params={"fields":"id,display_phone_number,verified_name,quality_rating"})
        if ok: phone_rows=listing.get("data") or []
    if not phone_rows:
        return jsonify(error="No phone number was returned by Embedded Signup. Check that the number was fully added to the WABA."),400
    saved=[]
    for p in phone_rows:
        phone_id=digits(p.get("id") or p.get("phone_number_id"))
        if not phone_id: continue
        ok, info=meta_request({"access_token":token,"api_version":META_GRAPH_VERSION},"GET",phone_id,
            params={"fields":"display_phone_number,verified_name,quality_rating"})
        detail=info if ok else p
        with db() as c:
            c.execute("""INSERT INTO numbers(phone_number_id,waba_id,business_id,display_phone,verified_name,
                       quality,access_token,api_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(phone_number_id) DO UPDATE SET waba_id=excluded.waba_id,
                       business_id=excluded.business_id,display_phone=excluded.display_phone,
                       verified_name=excluded.verified_name,quality=excluded.quality,
                       access_token=excluded.access_token,api_version=excluded.api_version,active=1""",
                (phone_id,waba_id,business_id,detail.get("display_phone_number",p.get("display_phone_number","")),
                 detail.get("verified_name",p.get("verified_name","")),detail.get("quality_rating",""),
                 token,META_GRAPH_VERSION,now_iso()))
            row=c.execute("SELECT id,phone_number_id,display_phone,verified_name FROM numbers WHERE phone_number_id=?",
                          (phone_id,)).fetchone()
            saved.append(dict(row))
    if not saved: return jsonify(error="No usable phone number IDs were saved"),400
    return jsonify(connected=True,numbers=saved)

@app.get("/api/numbers")
@login_required
def api_numbers():
    with db() as c:
        rows=c.execute("""SELECT id,phone_number_id,waba_id,business_id,display_phone,verified_name,
                          quality,active,created_at FROM numbers ORDER BY id DESC""").fetchall()
    return jsonify(numbers=[dict(x) for x in rows])

@app.delete("/api/numbers/<int:number_id>")
@login_required
def delete_number(number_id):
    with db() as c: c.execute("DELETE FROM numbers WHERE id=?",(number_id,))
    return jsonify(deleted=True)

@app.post("/internal/meta/events")
def pravah_forwarded_events():
    raw=request.get_data()
    if not verify_internal_signature(raw):
        return jsonify(error="Invalid internal signature"),401
    try: payload=json.loads(raw.decode("utf-8"))
    except ValueError: return jsonify(error="Invalid JSON"),400
    # Deduplicate the entire forwarded event; message IDs are additionally unique in messages.
    key=hashlib.sha256(raw).hexdigest()
    with db() as c:
        try: c.execute("INSERT INTO event_dedup(event_key,created_at) VALUES(?,?)",(key,now_iso()))
        except sqlite3.IntegrityError: return jsonify(ok=True,duplicate=True),200
    # Process after acknowledging. For a small single-user app a daemon thread is sufficient.
    threading.Thread(target=process_meta_payload,args=(payload,),daemon=True).start()
    return jsonify(ok=True),200

@app.get("/api/leads")
@login_required
def api_leads():
    number_id=request.args.get("number_id",type=int)
    q=request.args.get("q","").strip()
    sql="""SELECT l.*,n.display_phone FROM leads l JOIN numbers n ON n.id=l.number_id WHERE 1=1"""
    args=[]
    if number_id: sql+=" AND l.number_id=?"; args.append(number_id)
    if q: sql+=" AND (l.name LIKE ? OR l.phone LIKE ?)"; args.extend([f"%{q}%",f"%{q}%"])
    sql+=" ORDER BY l.updated_at DESC LIMIT 500"
    with db() as c: rows=c.execute(sql,args).fetchall()
    return jsonify(leads=[dict(x) for x in rows])

@app.get("/api/inbox")
@login_required
def api_inbox():
    number_id=request.args.get("number_id",type=int)
    if not number_id: return jsonify(conversations=[])
    with db() as c:
        rows=c.execute("""SELECT l.id lead_id,l.phone,l.name,l.status,l.updated_at,
          (SELECT body FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_message,
          (SELECT created_at FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) last_at
          FROM leads l WHERE l.number_id=? ORDER BY last_at DESC""",(number_id,)).fetchall()
    return jsonify(conversations=[dict(x) for x in rows])

@app.get("/api/inbox/<int:lead_id>/messages")
@login_required
def api_thread(lead_id):
    with db() as c:
        lead=c.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone()
        if not lead: return jsonify(error="Lead not found"),404
        rows=c.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY id ASC LIMIT 500",(lead_id,)).fetchall()
    return jsonify(lead=dict(lead),messages=[dict(x) for x in rows])

@app.post("/api/inbox/<int:lead_id>/send")
@login_required
def api_send(lead_id):
    data=request.get_json(silent=True) or {}
    with db() as c:
        leadrow=c.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone()
    if not leadrow: return jsonify(error="Lead not found"),404
    lead=dict(leadrow); number=get_number(lead["number_id"])
    if not number: return jsonify(error="WhatsApp number is disconnected"),400
    body=(data.get("body") or "").strip()
    if not body: return jsonify(error="Message is empty"),400
    result=send_text(number,lead["phone"],body)
    if not result["success"]: return jsonify(error=result["error"],details=result.get("details")),400
    store_outgoing(number,lead,result,"text",body)
    return jsonify(sent=True,message_id=result.get("message_id"))

@app.get("/api/automations")
@login_required
def api_automations():
    number_id=request.args.get("number_id",type=int)
    with db() as c:
        rows=c.execute("SELECT * FROM automations WHERE number_id=? ORDER BY id DESC",(number_id,)).fetchall() if number_id else []
    return jsonify(automations=[{**dict(x),"graph":json.loads(x["graph_json"])} for x in rows])

@app.post("/api/automations")
@login_required
def save_automation():
    d=request.get_json(silent=True) or {}
    number_id=int(d.get("number_id") or 0); name=(d.get("name") or "New automation").strip()
    trigger=d.get("trigger_type","keyword"); keyword=(d.get("keyword") or "").strip()
    graph=d.get("graph") or {}
    if not get_number(number_id): return jsonify(error="Select a connected number"),400
    if trigger not in ("keyword","first_after_24h"): return jsonify(error="Invalid trigger"),400
    if trigger=="keyword" and not keyword: return jsonify(error="Enter a keyword"),400
    if not isinstance(graph.get("nodes"),list) or not graph["nodes"]:
        return jsonify(error="Add nodes to the flow"),400
    fid=d.get("id")
    with db() as c:
        if fid:
            c.execute("""UPDATE automations SET name=?,trigger_type=?,keyword=?,enabled=?,graph_json=?,updated_at=?
                         WHERE id=? AND number_id=?""",
                      (name,trigger,keyword,1 if d.get("enabled",True) else 0,
                       json.dumps(graph),now_iso(),fid,number_id))
        else:
            cur=c.execute("""INSERT INTO automations(number_id,name,trigger_type,keyword,enabled,graph_json,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (number_id,name,trigger,keyword,1 if d.get("enabled",True) else 0,
                           json.dumps(graph),now_iso(),now_iso()))
            fid=cur.lastrowid
    return jsonify(saved=True,id=fid)

@app.delete("/api/automations/<int:flow_id>")
@login_required
def delete_automation(flow_id):
    with db() as c: c.execute("DELETE FROM automations WHERE id=?",(flow_id,))
    return jsonify(deleted=True)

def clean_field_key(value):
    key = re.sub(r"[^a-zA-Z0-9_]", "_", str(value or "field").strip()).strip("_").lower()
    if not key or key[0].isdigit(): key = "field_" + key
    return key[:32]

def build_meta_flow_json(form_name, screen_title, fields):
    """Build a single-screen, native WhatsApp Flow using supported input components."""
    children = [{"type": "TextHeading", "text": str(screen_title or form_name)[:80]}]
    payload = {}
    for index, field in enumerate(fields):
        kind = field.get("type", "text")
        label = str(field.get("label") or f"Field {index+1}").strip()[:80]
        key = clean_field_key(field.get("key") or label)
        required = bool(field.get("required", False))
        if kind in ("text", "email", "phone", "number"):
            comp = {"type":"TextInput", "name":key, "label":label,
                    "required":required, "input-type": {"text":"text","email":"email","phone":"phone","number":"number"}[kind]}
        elif kind == "date":
            comp = {"type":"DatePicker", "name":key, "label":label, "required":required}
        elif kind in ("dropdown", "radio", "checkbox"):
            options = field.get("options") or []
            choices = []
            seen = set()
            for opt in options[:20]:
                title = str(opt).strip()[:30]
                oid = clean_field_key(title)
                if title and oid not in seen:
                    seen.add(oid); choices.append({"id":oid, "title":title})
            if len(choices) < 2:
                raise ValueError(f"{label}: add at least two unique options")
            ctype = {"dropdown":"Dropdown", "radio":"RadioButtonsGroup", "checkbox":"CheckboxGroup"}[kind]
            comp = {"type":ctype, "name":key, "label":label, "required":required, "data-source":choices}
        else:
            raise ValueError(f"Unsupported field type: {kind}")
        children.append(comp)
        payload[key] = "${form." + key + "}"
    if not fields:
        raise ValueError("Add at least one field before creating a WhatsApp Flow")
    children.append({"type":"Footer", "label":"Submit", "on-click-action":{"name":"complete", "payload":payload}})
    return {
        "version":"7.3", "data_api_version":"3.0",
        "routing_model":{"CONTACT":[]},
        "screens":[{"id":"CONTACT", "title":str(form_name)[:80], "terminal":True,
                    "layout":{"type":"SingleColumnLayout", "children":[{"type":"Form", "name":"lead_form", "children":children}]}}]
    }

@app.get("/api/forms")
@login_required
def api_forms():
    number_id = request.args.get("number_id", type=int)
    with db() as c:
        if number_id:
            rows = c.execute("SELECT * FROM forms WHERE number_id=? OR number_id IS NULL ORDER BY id DESC", (number_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM forms ORDER BY id DESC").fetchall()
    return jsonify(forms=[{
        "id":x["id"], "name":x["name"], "meta_flow_id":x["meta_flow_id"],
        "first_screen_id":x["first_screen_id"], "cta_text":x["cta_text"], "prompt":x["prompt"],
        "status":x["status"] if "status" in x.keys() else ("PUBLISHED" if x["meta_flow_id"] else "LOCAL"),
        "category":x["category"] if "category" in x.keys() else "LEAD_GENERATION",
        "number_id":x["number_id"] if "number_id" in x.keys() else None,
        "fields":json.loads(x["fields_json"] or "[]"), "created_at":x["created_at"]
    } for x in rows])

@app.post("/api/forms")
@login_required
def save_form():
    """Create the form in DMvora, then create/upload a DRAFT Flow in Meta."""
    d=request.get_json(silent=True) or {}
    name=str(d.get("name") or "").strip()[:200]
    number=get_number(int(d.get("number_id") or 0))
    fields=d.get("fields") or []
    category=str(d.get("category") or "LEAD_GENERATION").upper()
    allowed={"SIGN_UP","SIGN_IN","APPOINTMENT_BOOKING","LEAD_GENERATION","SHOPPING","CONTACT_US","CUSTOMER_SUPPORT","SURVEY","OTHER"}
    if not number: return jsonify(error="Select a connected WhatsApp number"),400
    if not name: return jsonify(error="Enter a form name"),400
    if category not in allowed: return jsonify(error="Invalid Flow category"),400
    if not isinstance(fields,list) or len(fields)>20: return jsonify(error="Add between 1 and 20 supported fields"),400
    try:
        flow_json=build_meta_flow_json(name, d.get("screen_title") or "A few details", fields)
    except ValueError as e:
        return jsonify(error=str(e)),400
    cta=str(d.get("cta_text") or "Continue").strip()
    prompt=str(d.get("prompt") or "Please share a few details so we can help you.").strip()
    if not cta or len(cta)>20: return jsonify(error="Button text must be 1–20 characters"),400
    with db() as c:
        cur=c.execute("""INSERT INTO forms(name,fields_json,public_token,created_at,meta_flow_id,
            first_screen_id,cta_text,prompt,number_id,status,category,screen_title)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name,json.dumps(fields,ensure_ascii=False),secrets.token_urlsafe(18),now_iso(),"","CONTACT",cta,prompt,
             number["id"],"CREATING",category,str(d.get("screen_title") or "A few details")[:80]))
        form_id=cur.lastrowid
    # Meta Flow lifecycle: create draft -> upload flow.json (Meta validates it) -> user publishes.
    ok,created=meta_request(number,"POST",f"{number['waba_id']}/flows",
        data={"name":name,"categories":json.dumps([category])})
    if not ok:
        with db() as c: c.execute("UPDATE forms SET status='ERROR' WHERE id=?",(form_id,))
        return jsonify(error=meta_error(created),details=created,form_id=form_id),400
    meta_id=str(created.get("id") or "")
    if not meta_id:
        return jsonify(error="Meta did not return a Flow ID",details=created,form_id=form_id),502
    upload_files={"file":("flow.json",json.dumps(flow_json,ensure_ascii=False),"application/json")}
    ok,uploaded=meta_request(number,"POST",f"{meta_id}/assets",
        data={"name":"flow.json","asset_type":"FLOW_JSON"},files=upload_files)
    errors=uploaded.get("validation_errors") or uploaded.get("validationErrors") or []
    status="DRAFT" if ok and not errors else "INVALID"
    with db() as c:
        c.execute("UPDATE forms SET meta_flow_id=?,status=? WHERE id=?",(meta_id,status,form_id))
    if not ok or errors:
        return jsonify(error=meta_error(uploaded) if not ok else "Meta Flow validation failed",
                       validation_errors=errors,details=uploaded,form_id=form_id,meta_flow_id=meta_id),400
    return jsonify(saved=True,id=form_id,meta_flow_id=meta_id,status="DRAFT",
                   first_screen_id="CONTACT",validation=uploaded)

@app.post("/api/forms/<int:form_id>/publish")
@login_required
def publish_form(form_id):
    with db() as c: row=c.execute("SELECT * FROM forms WHERE id=?",(form_id,)).fetchone()
    if not row: return jsonify(error="Form not found"),404
    form=dict(row); number=get_number(form.get("number_id") or 0)
    if not number: return jsonify(error="The WhatsApp number for this form is no longer connected"),400
    if not form.get("meta_flow_id"): return jsonify(error="This form has no Meta Flow ID"),400
    if form.get("status")=="PUBLISHED": return jsonify(published=True,status="PUBLISHED")
    ok,result=meta_request(number,"POST",f"{form['meta_flow_id']}/publish")
    if not ok: return jsonify(error=meta_error(result),details=result),400
    with db() as c: c.execute("UPDATE forms SET status='PUBLISHED' WHERE id=?",(form_id,))
    return jsonify(published=True,status="PUBLISHED",meta_flow_id=form["meta_flow_id"])

@app.delete("/api/forms/<int:form_id>")
@login_required
def delete_form(form_id):
    with db() as c: row=c.execute("SELECT * FROM forms WHERE id=?",(form_id,)).fetchone()
    if not row: return jsonify(error="Form not found"),404
    form=dict(row); number=get_number(form.get("number_id") or 0)
    # Meta only permits deleting draft Flows. Published Flows must be deprecated in Meta.
    if form.get("status")=="PUBLISHED": return jsonify(error="Published Meta Flows are immutable. Deprecate them in Meta; remove the local form only after confirming it is no longer used."),409
    if number and form.get("meta_flow_id"):
        ok,result=meta_request(number,"DELETE",form["meta_flow_id"])
        if not ok: return jsonify(error=meta_error(result),details=result),400
    with db() as c: c.execute("DELETE FROM forms WHERE id=?",(form_id,))
    return jsonify(deleted=True)

@app.get("/f/<token>")
def public_form(token):
    with db() as c: form=c.execute("SELECT * FROM forms WHERE public_token=?",(token,)).fetchone()
    if not form: abort(404)
    return render_template("public_form.html",form=dict(form),fields=json.loads(form["fields_json"]),
                           phone=request.args.get("phone",""),number_id=request.args.get("number",""))

@app.post("/f/<token>")
def submit_public_form(token):
    with db() as c: form=c.execute("SELECT * FROM forms WHERE public_token=?",(token,)).fetchone()
    if not form: abort(404)
    number_id=request.form.get("number",type=int); phone=digits(request.form.get("phone",""))
    if not number_id or not phone: return "Invalid form link",400
    lead,_=upsert_lead(number_id,phone)
    values={}
    for field in json.loads(form["fields_json"]):
        key=field.get("key") or field.get("label")
        values[key]=request.form.get(key,"")[:2000]
    with db() as c:
        c.execute("UPDATE leads SET form_data=?,updated_at=? WHERE id=?",
                  (json.dumps(values,ensure_ascii=False),now_iso(),lead["id"]))
    return render_template("form_success.html")

@app.get("/api/templates")
@login_required
def api_templates():
    number_id=request.args.get("number_id",type=int)
    number=get_number(number_id) if number_id else None
    if not number: return jsonify(templates=[])
    with db() as c: rows=c.execute("SELECT * FROM templates WHERE number_id=? ORDER BY meta_name",(number_id,)).fetchall()
    return jsonify(templates=[dict(x) for x in rows])

@app.post("/api/templates/sync")
@login_required
def sync_templates():
    d=request.get_json(silent=True) or {}; number=get_number(int(d.get("number_id") or 0))
    if not number: return jsonify(error="Select a number"),400
    ok,waba=meta_request(number,"GET",f"{number['waba_id']}/message_templates",
                         params={"fields":"name,language,category,status,components","limit":100})
    if not ok: return jsonify(error=meta_error(waba)),400
    with db() as c:
        for t in waba.get("data") or []:
            c.execute("""INSERT INTO templates(number_id,meta_name,language,category,status,components_json)
              VALUES(?,?,?,?,?,?) ON CONFLICT(number_id,meta_name,language) DO UPDATE SET
              category=excluded.category,status=excluded.status,components_json=excluded.components_json""",
              (number["id"],t.get("name",""),(t.get("language") if isinstance(t.get("language"), str) else ((t.get("language") or {}).get("code","en_US"))),
               t.get("category",""),t.get("status",""),json.dumps(t.get("components") or [])))
    return jsonify(synced=len(waba.get("data") or []))

@app.post("/api/templates")
@login_required
def create_template():
    d=request.get_json(silent=True) or {}; number=get_number(int(d.get("number_id") or 0))
    if not number: return jsonify(error="Select a number"),400
    name=re.sub(r"[^a-z0-9_]", "_",(d.get("name") or "").lower()).strip("_")
    body=(d.get("body") or "").strip()
    if not name or not body: return jsonify(error="Template name and body are required"),400
    components=[{"type":"BODY","text":body}]
    if d.get("footer"): components.append({"type":"FOOTER","text":str(d["footer"])[:60]})
    buttons=[]
    for b in (d.get("buttons") or [])[:3]:
        title=(b.get("title") or "").strip()
        if not title: continue
        if b.get("type")=="URL":
            url=(b.get("url") or "").strip()
            if not url.startswith("https://"): return jsonify(error="Template URL must start with https://"),400
            buttons.append({"type":"URL","text":title[:25],"url":url})
        else: buttons.append({"type":"QUICK_REPLY","text":title[:25]})
    if buttons: components.append({"type":"BUTTONS","buttons":buttons})
    payload={"name":name,"language":d.get("language") or "en_US",
             "category":d.get("category") or "UTILITY","components":components}
    ok,result=meta_request(number,"POST",f"{number['waba_id']}/message_templates",json=payload)
    if not ok: return jsonify(error=meta_error(result),details=result),400
    return jsonify(submitted=True,result=result)

@app.errorhandler(413)
def too_large(_): return jsonify(error="Upload/request too large"),413

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8822")),debug=False)
