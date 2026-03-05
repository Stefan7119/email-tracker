"""
Email Open Tracker v3 - Pixel + Link Tracking + Gmail Auto-Track
Deploy to Render.com (free tier)
"""

import os
import json
import re
import threading
import time
import base64
import logging
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

from flask import Flask, request, send_file, jsonify, render_template_string, redirect, abort
import sqlite3
import uuid
import io

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


DATA_DIR = os.environ.get("DATA_DIR", ".")
DATABASE = os.path.join(DATA_DIR, "tracking.db")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

LAST_PROCESSED_FILE = os.path.join(DATA_DIR, "last_processed.txt")


def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            gmail_msg_id TEXT,
            auto_tracked INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT NOT NULL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT,
            user_agent TEXT,
            method TEXT DEFAULT 'pixel',
            FOREIGN KEY (email_id) REFERENCES emails(id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id TEXT PRIMARY KEY,
            email_id TEXT NOT NULL,
            original_url TEXT NOT NULL,
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (email_id) REFERENCES emails(id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id TEXT NOT NULL,
            email_id TEXT NOT NULL,
            clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT,
            user_agent TEXT,
            FOREIGN KEY (link_id) REFERENCES links(id),
            FOREIGN KEY (email_id) REFERENCES emails(id)
        )
    """)
    db.commit()
    db.close()


def get_base_url():
    return os.environ.get("BASE_URL", "https://email-tracker-941n.onrender.com").rstrip("/")


# ─── GMAIL INTEGRATION (optional) ─────────────────────────────────────

def get_google_flow():
    base_url = get_base_url()
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{base_url}/oauth/callback"],
            }
        },
        scopes=SCOPES,
        redirect_uri=f"{base_url}/oauth/callback",
    )


def get_gmail_credentials():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, "r") as f:
            token_data = json.load(f)
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        if creds and creds.valid:
            return creds
    except Exception as e:
        logger.error(f"Error loading credentials: {e}")
    return None


def save_credentials(creds):
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def get_gmail_service():
    creds = get_gmail_credentials()
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def get_last_processed_id():
    try:
        if os.path.exists(LAST_PROCESSED_FILE):
            with open(LAST_PROCESSED_FILE, "r") as f:
                return f.read().strip()
    except:
        pass
    return None


def set_last_processed_id(msg_id):
    with open(LAST_PROCESSED_FILE, "w") as f:
        f.write(msg_id)


def process_new_sent_emails():
    service = get_gmail_service()
    if not service:
        return

    last_id = get_last_processed_id()

    try:
        results = service.users().messages().list(
            userId="me", q="in:sent", maxResults=10
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return

        messages.reverse()
        new_last_id = last_id

        for msg_summary in messages:
            msg_id = msg_summary["id"]
            if last_id and msg_id <= last_id:
                continue

            try:
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()

                headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                to_addr = headers.get("to", "unknown")
                subject = headers.get("subject", "(no subject)")

                db = get_db()
                existing = db.execute(
                    "SELECT * FROM emails WHERE gmail_msg_id = ?", (msg_id,)
                ).fetchone()
                db.close()

                if existing:
                    new_last_id = msg_id
                    continue

                email_id = uuid.uuid4().hex[:12]
                db = get_db()
                db.execute(
                    "INSERT INTO emails (id, recipient, subject, gmail_msg_id, auto_tracked) VALUES (?, ?, ?, ?, 1)",
                    (email_id, to_addr, subject, msg_id),
                )
                db.commit()
                db.close()

                logger.info(f"Registered sent email: {subject} -> {to_addr}")
                new_last_id = msg_id

            except Exception as e:
                logger.error(f"Error processing message {msg_id}: {e}")
                continue

        if new_last_id and new_last_id != last_id:
            set_last_processed_id(new_last_id)

    except Exception as e:
        logger.error(f"Error checking sent emails: {e}")


def gmail_monitor_loop():
    logger.info("Gmail monitor started")
    time.sleep(10)
    while True:
        try:
            if get_gmail_credentials():
                process_new_sent_emails()
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        time.sleep(60)


# ─── TRACKING ENDPOINTS ───────────────────────────────────────────────

@app.route("/p/<email_id>.gif")
def track_open(email_id):
    db = get_db()
    email = db.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if email:
        db.execute(
            "INSERT INTO opens (email_id, ip_address, user_agent, method) VALUES (?, ?, ?, ?)",
            (email_id, request.headers.get("X-Forwarded-For", request.remote_addr),
             request.headers.get("User-Agent", ""), "pixel"),
        )
        db.commit()
    db.close()

    return send_file(
        io.BytesIO(PIXEL), mimetype="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                 "Pragma": "no-cache", "Expires": "0"},
    )


@app.route("/l/<link_id>")
def track_click(link_id):
    db = get_db()
    link = db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        db.close()
        abort(404)

    db.execute(
        "INSERT INTO clicks (link_id, email_id, ip_address, user_agent) VALUES (?, ?, ?, ?)",
        (link_id, link["email_id"],
         request.headers.get("X-Forwarded-For", request.remote_addr),
         request.headers.get("User-Agent", "")),
    )
    db.execute(
        "INSERT INTO opens (email_id, ip_address, user_agent, method) VALUES (?, ?, ?, ?)",
        (link["email_id"],
         request.headers.get("X-Forwarded-For", request.remote_addr),
         request.headers.get("User-Agent", ""), "link"),
    )
    db.commit()
    original_url = link["original_url"]
    db.close()
    return redirect(original_url)


# ─── GMAIL OAUTH ENDPOINTS ────────────────────────────────────────────

@app.route("/gmail/connect")
def gmail_connect():
    if not GOOGLE_LIBS_AVAILABLE:
        return jsonify({"error": "Google libraries not installed"}), 500
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars"}), 500
    flow = get_google_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    flow = get_google_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_credentials(creds)
    return redirect("/?gmail=connected")


@app.route("/gmail/status")
def gmail_status():
    if not GOOGLE_LIBS_AVAILABLE:
        return jsonify({"connected": False, "reason": "libraries not installed"})
    creds = get_gmail_credentials()
    if creds and creds.valid:
        try:
            service = build("gmail", "v1", credentials=creds)
            profile = service.users().getProfile(userId="me").execute()
            return jsonify({"connected": True, "email": profile.get("emailAddress", "unknown")})
        except:
            return jsonify({"connected": False, "reason": "token expired"})
    return jsonify({"connected": False, "reason": "not authorized"})


@app.route("/gmail/disconnect")
def gmail_disconnect():
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    return redirect("/?gmail=disconnected")


# ─── API ENDPOINTS ────────────────────────────────────────────────────

@app.route("/api/track", methods=["POST"])
def create_tracked_email():
    data = request.json
    recipient = data.get("recipient", "")
    subject = data.get("subject", "")
    if not recipient or not subject:
        return jsonify({"error": "recipient and subject are required"}), 400
    email_id = uuid.uuid4().hex[:12]
    db = get_db()
    db.execute("INSERT INTO emails (id, recipient, subject) VALUES (?, ?, ?)", (email_id, recipient, subject))
    db.commit()
    db.close()
    base_url = request.host_url.rstrip("/")
    pixel_url = f"{base_url}/p/{email_id}.gif"
    img_tag = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="" />'
    return jsonify({"email_id": email_id, "pixel_url": pixel_url, "img_tag": img_tag})


@app.route("/api/link", methods=["POST"])
def create_tracked_link():
    data = request.json
    email_id = data.get("email_id", "")
    original_url = data.get("url", "")
    label = data.get("label", "")
    if not email_id or not original_url:
        return jsonify({"error": "email_id and url are required"}), 400
    link_id = uuid.uuid4().hex[:10]
    db = get_db()
    email = db.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if not email:
        db.close()
        return jsonify({"error": "email not found"}), 404
    db.execute("INSERT INTO links (id, email_id, original_url, label) VALUES (?, ?, ?, ?)", (link_id, email_id, original_url, label))
    db.commit()
    db.close()
    base_url = request.host_url.rstrip("/")
    return jsonify({"link_id": link_id, "tracked_url": f"{base_url}/l/{link_id}", "original_url": original_url})


@app.route("/api/emails")
def list_emails():
    db = get_db()
    emails = db.execute("""
        SELECT e.*, COUNT(DISTINCT o.id) as open_count, MAX(o.opened_at) as last_opened,
               COUNT(DISTINCT c.id) as click_count, MAX(c.clicked_at) as last_clicked
        FROM emails e LEFT JOIN opens o ON e.id = o.email_id
        LEFT JOIN clicks c ON e.id = c.email_id GROUP BY e.id ORDER BY e.created_at DESC
    """).fetchall()
    db.close()
    return jsonify([{
        "id": e["id"], "recipient": e["recipient"], "subject": e["subject"],
        "created_at": e["created_at"], "open_count": e["open_count"],
        "last_opened": e["last_opened"], "click_count": e["click_count"],
        "last_clicked": e["last_clicked"], "auto_tracked": e["auto_tracked"],
    } for e in emails])


@app.route("/api/emails/<email_id>")
def get_email_detail(email_id):
    db = get_db()
    opens = db.execute("SELECT * FROM opens WHERE email_id = ? ORDER BY opened_at DESC", (email_id,)).fetchall()
    links = db.execute("SELECT * FROM links WHERE email_id = ? ORDER BY created_at", (email_id,)).fetchall()
    clicks = db.execute(
        "SELECT c.*, l.original_url, l.label FROM clicks c JOIN links l ON c.link_id = l.id WHERE c.email_id = ? ORDER BY c.clicked_at DESC",
        (email_id,)).fetchall()
    db.close()
    return jsonify({
        "opens": [{"opened_at": o["opened_at"], "ip_address": o["ip_address"], "user_agent": o["user_agent"], "method": o["method"]} for o in opens],
        "links": [{"id": l["id"], "original_url": l["original_url"], "label": l["label"]} for l in links],
        "clicks": [{"clicked_at": c["clicked_at"], "ip_address": c["ip_address"], "original_url": c["original_url"], "label": c["label"]} for c in clicks],
    })


@app.route("/api/emails/<email_id>", methods=["DELETE"])
def delete_email(email_id):
    db = get_db()
    db.execute("DELETE FROM clicks WHERE email_id = ?", (email_id,))
    db.execute("DELETE FROM opens WHERE email_id = ?", (email_id,))
    db.execute("DELETE FROM links WHERE email_id = ?", (email_id,))
    db.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})


# ─── DASHBOARD ─────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Email Tracker</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--border:#2a2a3a;--text:#e8e8f0;--text-dim:#7a7a90;
    --accent:#00d4aa;--accent-glow:rgba(0,212,170,0.15);--blue:#5b8def;--blue-glow:rgba(91,141,239,0.15);
    --red:#ff5c6a;--orange:#ffaa40;--purple:#a78bfa;--purple-glow:rgba(167,139,250,0.15); }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Outfit',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
  .noise{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:0.03;
    background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
  .container{position:relative;z-index:1;max-width:960px;margin:0 auto;padding:40px 24px}
  header{display:flex;align-items:center;justify-content:space-between;margin-bottom:40px;padding-bottom:24px;border-bottom:1px solid var(--border)}
  .logo{display:flex;align-items:center;gap:12px}
  .logo-icon{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,var(--accent),#00a885);
    display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;color:var(--bg)}
  h1{font-size:22px;font-weight:600;letter-spacing:-0.5px}
  h1 span{color:var(--text-dim);font-weight:400}
  .gmail-banner{background:var(--surface);border:1px solid var(--border);border-radius:12px;
    padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;justify-content:space-between}
  .gmail-banner.connected{border-color:rgba(0,212,170,0.3)}
  .gmail-info{display:flex;align-items:center;gap:12px}
  .gmail-dot{width:8px;height:8px;border-radius:50%}
  .gmail-dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  .gmail-dot.off{background:var(--orange)}
  .gmail-text{font-size:14px}
  .gmail-text .email{color:var(--accent);font-family:'JetBrains Mono',monospace;font-size:12px}
  .gmail-btn{padding:8px 16px;border-radius:8px;border:none;font-family:'Outfit',sans-serif;
    font-size:13px;font-weight:600;cursor:pointer;transition:all 0.2s}
  .gmail-btn.connect{background:var(--purple);color:white}
  .gmail-btn.connect:hover{background:#b99cfc}
  .gmail-btn.disconnect{background:transparent;color:var(--red);border:1px solid var(--border)}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:32px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px}
  .stat-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);font-family:'JetBrains Mono',monospace;margin-bottom:8px}
  .stat-value{font-size:28px;font-weight:700;letter-spacing:-1px}
  .stat-value.accent{color:var(--accent)} .stat-value.blue{color:var(--blue)}
  .create-section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:32px}
  .create-section h2{font-size:16px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
  .create-section h2::before{content:'+';display:inline-flex;align-items:center;justify-content:center;
    width:22px;height:22px;border-radius:6px;background:var(--accent-glow);color:var(--accent);font-family:'JetBrains Mono',monospace;font-size:14px}
  .form-row{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:end}
  .form-group label{display:block;font-size:12px;color:var(--text-dim);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
  .form-group input{width:100%;padding:10px 14px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-family:'Outfit',sans-serif;font-size:14px;outline:none;transition:border 0.2s}
  .form-group input:focus{border-color:var(--accent)}
  .form-group input::placeholder{color:#4a4a5a}
  .btn{padding:10px 20px;border-radius:8px;border:none;font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;cursor:pointer;transition:all 0.2s;white-space:nowrap}
  .btn-primary{background:var(--accent);color:var(--bg)}.btn-primary:hover{background:#00eabc;transform:translateY(-1px)}
  .btn-blue{background:var(--blue);color:white}.btn-blue:hover{background:#7aa3f5}
  .btn-sm{padding:6px 12px;font-size:12px;border-radius:6px}
  .btn-ghost{background:transparent;color:var(--text-dim);border:1px solid var(--border)}.btn-ghost:hover{border-color:var(--text-dim);color:var(--text)}
  .btn-danger{background:transparent;color:var(--red);border:1px solid transparent}.btn-danger:hover{background:rgba(255,92,106,0.1)}
  .result-box{margin-top:16px;padding:16px;border-radius:8px;display:none}
  .result-box.show{display:block}
  .result-box.green{background:var(--accent-glow);border:1px solid rgba(0,212,170,0.2)}
  .result-box p{font-size:13px;margin-bottom:8px;font-weight:500}
  .result-box.green p{color:var(--accent)}
  .code-block{position:relative;background:var(--bg);border-radius:6px;padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text);word-break:break-all;line-height:1.5;margin-bottom:8px}
  .code-block .copy-btn{position:absolute;top:8px;right:8px;padding:4px 10px;border-radius:4px;background:var(--surface2);border:1px solid var(--border);color:var(--text-dim);font-size:11px;cursor:pointer;font-family:'JetBrains Mono',monospace}
  .code-block .copy-btn:hover{color:var(--accent);border-color:var(--accent)}
  .link-section{margin-top:16px;padding-top:16px;border-top:1px solid var(--border);display:none}
  .link-section.show{display:block}
  .link-section h3{font-size:14px;font-weight:600;margin-bottom:12px;color:var(--blue)}
  .link-row{display:grid;grid-template-columns:1fr 200px auto;gap:12px;align-items:end;margin-bottom:8px}
  .tracked-links-list{margin-top:12px}
  .tracked-link-item{display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--bg);border-radius:6px;margin-bottom:6px;font-size:12px;font-family:'JetBrains Mono',monospace}
  .tracked-link-item .label{color:var(--blue);min-width:100px}
  .tracked-link-item .url{color:var(--text-dim);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tracked-link-item .copy-small{padding:2px 8px;border-radius:3px;background:var(--surface2);border:1px solid var(--border);color:var(--text-dim);cursor:pointer;font-size:10px}
  .email-list h2{font-size:16px;font-weight:600;margin-bottom:16px;color:var(--text-dim)}
  .email-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:10px;display:grid;grid-template-columns:1fr auto;align-items:center;gap:16px;transition:border-color 0.2s}
  .email-card:hover{border-color:#3a3a4a}
  .email-info h3{font-size:15px;font-weight:500;margin-bottom:4px}
  .email-meta{font-size:12px;color:var(--text-dim);font-family:'JetBrains Mono',monospace;display:flex;gap:16px;flex-wrap:wrap}
  .email-actions{display:flex;align-items:center;gap:10px}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:20px;font-size:12px;font-weight:600}
  .badge .dot{width:6px;height:6px;border-radius:50%}
  .badge.opened{background:var(--accent-glow);color:var(--accent)}.badge.opened .dot{background:var(--accent)}
  .badge.clicked{background:var(--blue-glow);color:var(--blue)}.badge.clicked .dot{background:var(--blue)}
  .badge.unopened{background:rgba(255,170,64,0.1);color:var(--orange)}.badge.unopened .dot{background:var(--orange)}
  .badge.auto{background:var(--purple-glow);color:var(--purple);font-size:10px}
  .empty-state{text-align:center;padding:60px 20px;color:var(--text-dim)}
  .modal-overlay{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center}
  .modal-overlay.show{display:flex}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px;max-width:560px;width:90%;max-height:80vh;overflow-y:auto}
  .modal h3{font-size:18px;margin-bottom:4px}
  .modal .modal-sub{font-size:13px;color:var(--text-dim);margin-bottom:20px;font-family:'JetBrains Mono',monospace}
  .tab-bar{display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border)}
  .tab{padding:8px 16px;font-size:13px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;color:var(--text-dim);transition:all 0.2s}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent)}.tab:hover{color:var(--text)}
  .tab-content{display:none}.tab-content.active{display:block}
  .open-entry{padding:12px 0;border-bottom:1px solid var(--border)}.open-entry:last-child{border-bottom:none}
  .open-time{font-size:14px;font-weight:500;margin-bottom:4px}
  .method-tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-family:'JetBrains Mono',monospace;text-transform:uppercase;margin-left:8px}
  .method-tag.pixel{background:var(--accent-glow);color:var(--accent)}.method-tag.link{background:var(--blue-glow);color:var(--blue)}
  .open-details{font-size:11px;color:var(--text-dim);font-family:'JetBrains Mono',monospace;word-break:break-all}
  @media(max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}.form-row,.link-row{grid-template-columns:1fr}.email-card{grid-template-columns:1fr}.email-actions{justify-content:flex-start;flex-wrap:wrap}}
</style>
</head>
<body>
<div class="noise"></div>
<div class="container">
  <header><div class="logo"><div class="logo-icon">T</div><h1>Tracker <span>/ email opens + clicks</span></h1></div></header>
  <div class="gmail-banner" id="gmailBanner">
    <div class="gmail-info"><div class="gmail-dot off" id="gmailDot"></div><div class="gmail-text"><span id="gmailStatus">Checking Gmail...</span></div></div>
    <button class="gmail-btn connect" id="gmailBtn" onclick="connectGmail()">Connect Gmail</button>
  </div>
  <div class="stats">
    <div class="stat-card"><div class="stat-label">Tracked</div><div class="stat-value" id="totalEmails">0</div></div>
    <div class="stat-card"><div class="stat-label">Pixel Opens</div><div class="stat-value accent" id="totalOpens">0</div></div>
    <div class="stat-card"><div class="stat-label">Link Clicks</div><div class="stat-value blue" id="totalClicks">0</div></div>
    <div class="stat-card"><div class="stat-label">Engaged</div><div class="stat-value" id="openRate">0%</div></div>
  </div>
  <div class="create-section">
    <h2>Manual Tracking</h2>
    <div class="form-row">
      <div class="form-group"><label>Recipient</label><input type="text" id="recipient" placeholder="john@example.com"/></div>
      <div class="form-group"><label>Subject</label><input type="text" id="subject" placeholder="Project proposal"/></div>
      <button class="btn btn-primary" onclick="createTracker()">Create</button>
    </div>
    <div class="result-box green" id="pixelResult"><p>Tracking pixel:</p><div class="code-block"><code id="pixelCode"></code><button class="copy-btn" onclick="copyText('pixelCode')">Copy</button></div></div>
    <div class="link-section" id="linkSection">
      <h3>Create Tracked Links</h3>
      <div class="link-row">
        <div class="form-group"><label>URL</label><input type="text" id="linkUrl" placeholder="https://example.com/doc.pdf"/></div>
        <div class="form-group"><label>Label</label><input type="text" id="linkLabel" placeholder="Proposal"/></div>
        <button class="btn btn-blue" onclick="createLink()">Track</button>
      </div>
      <div class="tracked-links-list" id="trackedLinks"></div>
    </div>
  </div>
  <div class="email-list"><h2>Tracked Emails</h2><div id="emailList"><div class="empty-state"><p>No emails tracked yet.</p></div></div></div>
</div>
<div class="modal-overlay" id="modalOverlay" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3 id="modalTitle"></h3><div class="modal-sub" id="modalSub"></div>
    <div class="tab-bar"><div class="tab active" onclick="switchTab('opens',this)">Opens</div><div class="tab" onclick="switchTab('clicks',this)">Clicks</div></div>
    <div class="tab-content active" id="tab-opens"></div><div class="tab-content" id="tab-clicks"></div>
  </div>
</div>
<script>
let currentEmailId='';
async function checkGmail(){try{const r=await fetch('/gmail/status');const d=await r.json();const dot=document.getElementById('gmailDot'),s=document.getElementById('gmailStatus'),b=document.getElementById('gmailBtn'),bn=document.getElementById('gmailBanner');if(d.connected){dot.className='gmail-dot on';s.innerHTML='Gmail connected: <span class="email">'+d.email+'</span> — auto-tracking';b.textContent='Disconnect';b.className='gmail-btn disconnect';b.onclick=()=>{location.href='/gmail/disconnect'};bn.classList.add('connected')}else{dot.className='gmail-dot off';s.textContent='Gmail not connected';b.textContent='Connect Gmail';b.className='gmail-btn connect';b.onclick=connectGmail}}catch(e){document.getElementById('gmailStatus').textContent='Gmail integration available'}}
function connectGmail(){location.href='/gmail/connect'}
async function loadEmails(){const r=await fetch('/api/emails');const emails=await r.json();const t=emails.length,o=emails.reduce((s,e)=>s+e.open_count,0),c=emails.reduce((s,e)=>s+e.click_count,0),g=emails.filter(e=>e.open_count>0||e.click_count>0).length;document.getElementById('totalEmails').textContent=t;document.getElementById('totalOpens').textContent=o;document.getElementById('totalClicks').textContent=c;document.getElementById('openRate').textContent=t>0?Math.round(g/t*100)+'%':'0%';const l=document.getElementById('emailList');if(!emails.length){l.innerHTML='<div class="empty-state"><p>No emails tracked yet.</p></div>';return}l.innerHTML=emails.map(e=>{const ho=e.open_count>0,hc=e.click_count>0;const cr=new Date(e.created_at+'Z').toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});let b='';if(e.auto_tracked)b+='<div class="badge auto">AUTO</div>';if(ho)b+=`<div class="badge opened"><span class="dot"></span>${e.open_count} open${e.open_count>1?'s':''}</div>`;if(hc)b+=`<div class="badge clicked"><span class="dot"></span>${e.click_count} click${e.click_count>1?'s':''}</div>`;if(!ho&&!hc)b+='<div class="badge unopened"><span class="dot"></span>No activity</div>';return`<div class="email-card"><div class="email-info"><h3>${esc(e.subject)}</h3><div class="email-meta"><span>To: ${esc(e.recipient)}</span><span>${cr}</span></div></div><div class="email-actions">${b}${(ho||hc)?`<button class="btn btn-sm btn-ghost" onclick="viewDetail('${e.id}','${esc(e.subject)}','${esc(e.recipient)}')">Details</button>`:''}<button class="btn btn-sm btn-danger" onclick="deleteEmail('${e.id}')">×</button></div></div>`}).join('')}
async function createTracker(){const re=document.getElementById('recipient').value.trim(),su=document.getElementById('subject').value.trim();if(!re||!su)return;const r=await fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient:re,subject:su})});const d=await r.json();currentEmailId=d.email_id;document.getElementById('pixelCode').textContent=d.img_tag;document.getElementById('pixelResult').classList.add('show');document.getElementById('linkSection').classList.add('show');document.getElementById('trackedLinks').innerHTML='';document.getElementById('recipient').value='';document.getElementById('subject').value='';loadEmails()}
async function createLink(){const u=document.getElementById('linkUrl').value.trim(),lb=document.getElementById('linkLabel').value.trim();if(!u||!currentEmailId)return;const r=await fetch('/api/link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email_id:currentEmailId,url:u,label:lb})});const d=await r.json();document.getElementById('trackedLinks').innerHTML+=`<div class="tracked-link-item"><span class="label">${esc(lb||'Link')}</span><span class="url">${esc(d.tracked_url)}</span><button class="copy-small" onclick="navigator.clipboard.writeText('${d.tracked_url}');this.textContent='Done!';setTimeout(()=>this.textContent='Copy',1500)">Copy</button></div>`;document.getElementById('linkUrl').value='';document.getElementById('linkLabel').value=''}
async function viewDetail(id,sub,rec){const r=await fetch(`/api/emails/${id}`);const d=await r.json();document.getElementById('modalTitle').textContent=sub;document.getElementById('modalSub').textContent='To: '+rec;document.getElementById('tab-opens').innerHTML=d.opens.length?d.opens.map(o=>`<div class="open-entry"><div class="open-time">${new Date(o.opened_at+'Z').toLocaleString()}<span class="method-tag ${o.method}">${o.method}</span></div><div class="open-details">IP: ${o.ip_address}<br>${o.user_agent}</div></div>`).join(''):'<p style="color:var(--text-dim);padding:20px 0">No opens yet.</p>';document.getElementById('tab-clicks').innerHTML=d.clicks.length?d.clicks.map(c=>`<div class="open-entry"><div class="open-time">${new Date(c.clicked_at+'Z').toLocaleString()}<span class="method-tag link">${esc(c.label||'link')}</span></div><div class="open-details">URL: ${esc(c.original_url)}<br>IP: ${c.ip_address}</div></div>`).join(''):'<p style="color:var(--text-dim);padding:20px 0">No clicks yet.</p>';document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));document.querySelector('.tab').classList.add('active');document.getElementById('tab-opens').classList.add('active');document.getElementById('modalOverlay').classList.add('show')}
function switchTab(n,el){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));el.classList.add('active');document.getElementById('tab-'+n).classList.add('active')}
function closeModal(e){if(e.target===document.getElementById('modalOverlay'))document.getElementById('modalOverlay').classList.remove('show')}
async function deleteEmail(id){await fetch(`/api/emails/${id}`,{method:'DELETE'});loadEmails()}
function copyText(id){navigator.clipboard.writeText(document.getElementById(id).textContent).then(()=>{const b=document.getElementById(id).parentElement.querySelector('.copy-btn');b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',2000)})}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
checkGmail();if(location.search.includes('gmail=connected'))history.replaceState({},'','/');loadEmails();setInterval(loadEmails,30000);
</script>
</body></html>
"""

init_db()

if GOOGLE_LIBS_AVAILABLE and GOOGLE_CLIENT_ID:
    monitor_thread = threading.Thread(target=gmail_monitor_loop, daemon=True)
    monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
