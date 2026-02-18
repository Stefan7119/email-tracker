"""
Email Open Tracker v2 - Pixel + Link Tracking
Deploy to Render.com (free tier)

Features:
- Invisible tracking pixel (catches ~60-70% of opens)
- Link tracking with redirect (catches clicks even when images blocked)
- Dashboard with real-time stats
- Works with Outlook, Gmail, or any email client
"""

import os
from flask import Flask, request, send_file, jsonify, render_template_string, redirect, abort
import sqlite3
import uuid
import io
import base64
from datetime import datetime
from urllib.parse import quote, unquote

app = Flask(__name__)

# Use persistent disk on Render if available, otherwise local
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATABASE = os.path.join(DATA_DIR, "tracking.db")

# 1x1 transparent GIF
PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


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


# ─── TRACKING PIXEL ENDPOINT ──────────────────────────────────────────
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
        io.BytesIO(PIXEL),
        mimetype="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ─── LINK TRACKING ENDPOINT ───────────────────────────────────────────
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


# ─── API: CREATE A NEW TRACKED EMAIL ──────────────────────────────────
@app.route("/api/track", methods=["POST"])
def create_tracked_email():
    data = request.json
    recipient = data.get("recipient", "")
    subject = data.get("subject", "")

    if not recipient or not subject:
        return jsonify({"error": "recipient and subject are required"}), 400

    email_id = uuid.uuid4().hex[:12]
    db = get_db()
    db.execute(
        "INSERT INTO emails (id, recipient, subject) VALUES (?, ?, ?)",
        (email_id, recipient, subject),
    )
    db.commit()
    db.close()

    base_url = request.host_url.rstrip("/")
    pixel_url = f"{base_url}/p/{email_id}.gif"
    img_tag = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="" />'

    return jsonify({
        "email_id": email_id,
        "pixel_url": pixel_url,
        "img_tag": img_tag,
    })


# ─── API: CREATE A TRACKED LINK ───────────────────────────────────────
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

    db.execute(
        "INSERT INTO links (id, email_id, original_url, label) VALUES (?, ?, ?, ?)",
        (link_id, email_id, original_url, label),
    )
    db.commit()
    db.close()

    base_url = request.host_url.rstrip("/")
    tracked_url = f"{base_url}/l/{link_id}"

    return jsonify({
        "link_id": link_id,
        "tracked_url": tracked_url,
        "original_url": original_url,
    })


# ─── API: LIST ALL TRACKED EMAILS ─────────────────────────────────────
@app.route("/api/emails")
def list_emails():
    db = get_db()
    emails = db.execute("""
        SELECT e.*,
               COUNT(DISTINCT o.id) as open_count,
               MAX(o.opened_at) as last_opened,
               COUNT(DISTINCT c.id) as click_count,
               MAX(c.clicked_at) as last_clicked
        FROM emails e
        LEFT JOIN opens o ON e.id = o.email_id
        LEFT JOIN clicks c ON e.id = c.email_id
        GROUP BY e.id
        ORDER BY e.created_at DESC
    """).fetchall()
    db.close()

    return jsonify([{
        "id": e["id"],
        "recipient": e["recipient"],
        "subject": e["subject"],
        "created_at": e["created_at"],
        "open_count": e["open_count"],
        "last_opened": e["last_opened"],
        "click_count": e["click_count"],
        "last_clicked": e["last_clicked"],
    } for e in emails])


# ─── API: GET DETAIL FOR A SPECIFIC EMAIL ─────────────────────────────
@app.route("/api/emails/<email_id>")
def get_email_detail(email_id):
    db = get_db()
    opens = db.execute(
        "SELECT * FROM opens WHERE email_id = ? ORDER BY opened_at DESC", (email_id,)
    ).fetchall()
    links = db.execute(
        "SELECT * FROM links WHERE email_id = ? ORDER BY created_at", (email_id,)
    ).fetchall()
    clicks = db.execute(
        "SELECT c.*, l.original_url, l.label FROM clicks c JOIN links l ON c.link_id = l.id WHERE c.email_id = ? ORDER BY c.clicked_at DESC",
        (email_id,),
    ).fetchall()
    db.close()

    return jsonify({
        "opens": [{"opened_at": o["opened_at"], "ip_address": o["ip_address"],
                    "user_agent": o["user_agent"], "method": o["method"]} for o in opens],
        "links": [{"id": l["id"], "original_url": l["original_url"],
                    "label": l["label"]} for l in links],
        "clicks": [{"clicked_at": c["clicked_at"], "ip_address": c["ip_address"],
                     "original_url": c["original_url"], "label": c["label"]} for c in clicks],
    })


# ─── API: DELETE A TRACKED EMAIL ───────────────────────────────────────
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
  :root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --surface2: #1a1a26;
    --border: #2a2a3a;
    --text: #e8e8f0;
    --text-dim: #7a7a90;
    --accent: #00d4aa;
    --accent-glow: rgba(0, 212, 170, 0.15);
    --blue: #5b8def;
    --blue-glow: rgba(91, 141, 239, 0.15);
    --red: #ff5c6a;
    --orange: #ffaa40;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Outfit', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  .noise {
    position: fixed; inset: 0; z-index: 0; pointer-events: none; opacity: 0.03;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }

  .container {
    position: relative; z-index: 1;
    max-width: 960px; margin: 0 auto; padding: 40px 24px;
  }

  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 40px; padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
  }

  .logo { display: flex; align-items: center; gap: 12px; }

  .logo-icon {
    width: 36px; height: 36px; border-radius: 8px;
    background: linear-gradient(135deg, var(--accent), #00a885);
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; font-weight: 700; color: var(--bg);
  }

  h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.5px; }
  h1 span { color: var(--text-dim); font-weight: 400; }

  .stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
    margin-bottom: 32px;
  }

  .stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
  }

  .stat-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-dim); font-family: 'JetBrains Mono', monospace;
    margin-bottom: 8px;
  }

  .stat-value { font-size: 28px; font-weight: 700; letter-spacing: -1px; }
  .stat-value.accent { color: var(--accent); }
  .stat-value.blue { color: var(--blue); }

  .create-section {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px; margin-bottom: 32px;
  }

  .create-section h2 {
    font-size: 16px; font-weight: 600; margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
  }

  .create-section h2::before {
    content: '+'; display: inline-flex; align-items: center; justify-content: center;
    width: 22px; height: 22px; border-radius: 6px;
    background: var(--accent-glow); color: var(--accent);
    font-family: 'JetBrains Mono', monospace; font-size: 14px;
  }

  .form-row {
    display: grid; grid-template-columns: 1fr 1fr auto; gap: 12px;
    align-items: end;
  }

  .form-group label {
    display: block; font-size: 12px; color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 6px;
  }

  .form-group input {
    width: 100%; padding: 10px 14px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--text); font-family: 'Outfit', sans-serif; font-size: 14px;
    outline: none; transition: border 0.2s;
  }

  .form-group input:focus { border-color: var(--accent); }
  .form-group input::placeholder { color: #4a4a5a; }

  .btn {
    padding: 10px 20px; border-radius: 8px; border: none;
    font-family: 'Outfit', sans-serif; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: all 0.2s; white-space: nowrap;
  }

  .btn-primary { background: var(--accent); color: var(--bg); }
  .btn-primary:hover { background: #00eabc; transform: translateY(-1px); }
  .btn-blue { background: var(--blue); color: white; }
  .btn-blue:hover { background: #7aa3f5; transform: translateY(-1px); }
  .btn-sm { padding: 6px 12px; font-size: 12px; border-radius: 6px; }
  .btn-ghost { background: transparent; color: var(--text-dim); border: 1px solid var(--border); }
  .btn-ghost:hover { border-color: var(--text-dim); color: var(--text); }
  .btn-danger { background: transparent; color: var(--red); border: 1px solid transparent; }
  .btn-danger:hover { background: rgba(255, 92, 106, 0.1); }

  .result-box {
    margin-top: 16px; padding: 16px; border-radius: 8px;
    display: none;
  }

  .result-box.show { display: block; }

  .result-box.green {
    background: var(--accent-glow); border: 1px solid rgba(0, 212, 170, 0.2);
  }

  .result-box.blue-bg {
    background: var(--blue-glow); border: 1px solid rgba(91, 141, 239, 0.2);
  }

  .result-box p { font-size: 13px; margin-bottom: 8px; font-weight: 500; }
  .result-box.green p { color: var(--accent); }
  .result-box.blue-bg p { color: var(--blue); }

  .code-block {
    position: relative; background: var(--bg); border-radius: 6px;
    padding: 12px 14px; font-family: 'JetBrains Mono', monospace;
    font-size: 12px; color: var(--text); word-break: break-all;
    line-height: 1.5; margin-bottom: 8px;
  }

  .code-block .copy-btn {
    position: absolute; top: 8px; right: 8px; padding: 4px 10px;
    border-radius: 4px; background: var(--surface2); border: 1px solid var(--border);
    color: var(--text-dim); font-size: 11px; cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
  }

  .code-block .copy-btn:hover { color: var(--accent); border-color: var(--accent); }

  .link-section {
    margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border);
    display: none;
  }

  .link-section.show { display: block; }

  .link-section h3 {
    font-size: 14px; font-weight: 600; margin-bottom: 12px;
    color: var(--blue); display: flex; align-items: center; gap: 8px;
  }

  .link-row {
    display: grid; grid-template-columns: 1fr 200px auto; gap: 12px;
    align-items: end; margin-bottom: 8px;
  }

  .tracked-links-list { margin-top: 12px; }

  .tracked-link-item {
    display: flex; align-items: center; gap: 12px;
    padding: 8px 12px; background: var(--bg); border-radius: 6px;
    margin-bottom: 6px; font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
  }

  .tracked-link-item .label { color: var(--blue); min-width: 100px; }
  .tracked-link-item .url { color: var(--text-dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tracked-link-item .copy-small {
    padding: 2px 8px; border-radius: 3px; background: var(--surface2);
    border: 1px solid var(--border); color: var(--text-dim); cursor: pointer; font-size: 10px;
  }

  .instructions {
    margin-top: 12px; font-size: 13px; color: var(--text-dim); line-height: 1.6;
  }
  .instructions strong { color: var(--text); }

  .email-list h2 {
    font-size: 16px; font-weight: 600; margin-bottom: 16px; color: var(--text-dim);
  }

  .email-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 20px; margin-bottom: 10px;
    display: grid; grid-template-columns: 1fr auto;
    align-items: center; gap: 16px; transition: border-color 0.2s;
  }

  .email-card:hover { border-color: #3a3a4a; }
  .email-info h3 { font-size: 15px; font-weight: 500; margin-bottom: 4px; }

  .email-meta {
    font-size: 12px; color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace;
    display: flex; gap: 16px; flex-wrap: wrap;
  }

  .email-actions { display: flex; align-items: center; gap: 10px; }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px; border-radius: 20px; font-size: 12px; font-weight: 600;
  }

  .badge .dot { width: 6px; height: 6px; border-radius: 50%; }
  .badge.opened { background: var(--accent-glow); color: var(--accent); }
  .badge.opened .dot { background: var(--accent); }
  .badge.clicked { background: var(--blue-glow); color: var(--blue); }
  .badge.clicked .dot { background: var(--blue); }
  .badge.unopened { background: rgba(255, 170, 64, 0.1); color: var(--orange); }
  .badge.unopened .dot { background: var(--orange); }

  .empty-state { text-align: center; padding: 60px 20px; color: var(--text-dim); }
  .empty-state p { font-size: 14px; margin-top: 8px; }

  .modal-overlay {
    display: none; position: fixed; inset: 0; z-index: 100;
    background: rgba(0, 0, 0, 0.7); backdrop-filter: blur(4px);
    align-items: center; justify-content: center;
  }

  .modal-overlay.show { display: flex; }

  .modal {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; padding: 28px; max-width: 560px; width: 90%;
    max-height: 80vh; overflow-y: auto;
  }

  .modal h3 { font-size: 18px; margin-bottom: 4px; }

  .modal .modal-sub {
    font-size: 13px; color: var(--text-dim); margin-bottom: 20px;
    font-family: 'JetBrains Mono', monospace;
  }

  .tab-bar {
    display: flex; gap: 0; margin-bottom: 16px; border-bottom: 1px solid var(--border);
  }

  .tab {
    padding: 8px 16px; font-size: 13px; font-weight: 500; cursor: pointer;
    border-bottom: 2px solid transparent; color: var(--text-dim); transition: all 0.2s;
  }

  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab:hover { color: var(--text); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .open-entry { padding: 12px 0; border-bottom: 1px solid var(--border); }
  .open-entry:last-child { border-bottom: none; }
  .open-time { font-size: 14px; font-weight: 500; margin-bottom: 4px; }

  .method-tag {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-family: 'JetBrains Mono', monospace;
    text-transform: uppercase; margin-left: 8px;
  }

  .method-tag.pixel { background: var(--accent-glow); color: var(--accent); }
  .method-tag.link { background: var(--blue-glow); color: var(--blue); }

  .open-details {
    font-size: 11px; color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace; word-break: break-all;
  }

  @media (max-width: 700px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .form-row, .link-row { grid-template-columns: 1fr; }
    .email-card { grid-template-columns: 1fr; }
    .email-actions { justify-content: flex-start; flex-wrap: wrap; }
  }
</style>
</head>
<body>
<div class="noise"></div>
<div class="container">

  <header>
    <div class="logo">
      <div class="logo-icon">T</div>
      <h1>Tracker <span>/ email opens + clicks</span></h1>
    </div>
  </header>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-label">Tracked</div>
      <div class="stat-value" id="totalEmails">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Pixel Opens</div>
      <div class="stat-value accent" id="totalOpens">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Link Clicks</div>
      <div class="stat-value blue" id="totalClicks">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Rate</div>
      <div class="stat-value" id="openRate">0%</div>
    </div>
  </div>

  <div class="create-section">
    <h2>Step 1: Register an Email</h2>
    <div class="form-row">
      <div class="form-group">
        <label>Recipient</label>
        <input type="text" id="recipient" placeholder="john@example.com" />
      </div>
      <div class="form-group">
        <label>Subject</label>
        <input type="text" id="subject" placeholder="Project proposal" />
      </div>
      <button class="btn btn-primary" onclick="createTracker()">Create</button>
    </div>

    <div class="result-box green" id="pixelResult">
      <p>Tracking pixel (paste this into your email as HTML):</p>
      <div class="code-block">
        <code id="pixelCode"></code>
        <button class="copy-btn" onclick="copyText('pixelCode')">Copy</button>
      </div>
    </div>

    <div class="link-section" id="linkSection">
      <h3>Step 2: Create Tracked Links (optional but recommended)</h3>
      <p style="font-size:13px; color:var(--text-dim); margin-bottom:12px;">
        Wrap any URL you'd put in your email. When they click it, you'll know — even if the pixel was blocked.
      </p>
      <div class="link-row">
        <div class="form-group">
          <label>URL to track</label>
          <input type="text" id="linkUrl" placeholder="https://example.com/proposal.pdf" />
        </div>
        <div class="form-group">
          <label>Label</label>
          <input type="text" id="linkLabel" placeholder="Proposal PDF" />
        </div>
        <button class="btn btn-blue" onclick="createLink()">Track Link</button>
      </div>
      <div class="tracked-links-list" id="trackedLinks"></div>

      <div class="instructions">
        <strong>How to use in Outlook:</strong><br>
        1. Compose your email in <strong>Outlook Web</strong> (outlook.com)<br>
        2. For tracked links: use the tracked URL when adding hyperlinks (select text → Insert Link → paste tracked URL)<br>
        3. For the pixel: open DevTools (F12), find the email body, paste the <code>&lt;img&gt;</code> tag at the end<br>
        4. Send!<br><br>
        <strong>Tip:</strong> Tracked links are the most reliable method. Just use them as your normal links — no DevTools needed.
      </div>
    </div>
  </div>

  <div class="email-list">
    <h2>Tracked Emails</h2>
    <div id="emailList">
      <div class="empty-state">
        <p>No emails tracked yet. Create one above to get started.</p>
      </div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modalOverlay" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3 id="modalTitle"></h3>
    <div class="modal-sub" id="modalSub"></div>
    <div class="tab-bar">
      <div class="tab active" onclick="switchTab('opens', this)">Opens</div>
      <div class="tab" onclick="switchTab('clicks', this)">Clicks</div>
    </div>
    <div class="tab-content active" id="tab-opens"></div>
    <div class="tab-content" id="tab-clicks"></div>
  </div>
</div>

<script>
  let currentEmailId = '';
  let currentPixelHTML = '';

  async function loadEmails() {
    const res = await fetch('/api/emails');
    const emails = await res.json();

    const totalEmails = emails.length;
    const totalOpens = emails.reduce((s, e) => s + e.open_count, 0);
    const totalClicks = emails.reduce((s, e) => s + e.click_count, 0);
    const engaged = emails.filter(e => e.open_count > 0 || e.click_count > 0).length;

    document.getElementById('totalEmails').textContent = totalEmails;
    document.getElementById('totalOpens').textContent = totalOpens;
    document.getElementById('totalClicks').textContent = totalClicks;
    document.getElementById('openRate').textContent =
      totalEmails > 0 ? Math.round((engaged / totalEmails) * 100) + '%' : '0%';

    const list = document.getElementById('emailList');
    if (emails.length === 0) {
      list.innerHTML = '<div class="empty-state"><p>No emails tracked yet.</p></div>';
      return;
    }

    list.innerHTML = emails.map(e => {
      const hasOpens = e.open_count > 0;
      const hasClicks = e.click_count > 0;
      const created = new Date(e.created_at + 'Z').toLocaleDateString('en-US', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
      });

      let badges = '';
      if (hasOpens) badges += `<div class="badge opened"><span class="dot"></span>${e.open_count} open${e.open_count>1?'s':''}</div>`;
      if (hasClicks) badges += `<div class="badge clicked"><span class="dot"></span>${e.click_count} click${e.click_count>1?'s':''}</div>`;
      if (!hasOpens && !hasClicks) badges += `<div class="badge unopened"><span class="dot"></span>No activity</div>`;

      return `
        <div class="email-card">
          <div class="email-info">
            <h3>${esc(e.subject)}</h3>
            <div class="email-meta">
              <span>To: ${esc(e.recipient)}</span>
              <span>${created}</span>
            </div>
          </div>
          <div class="email-actions">
            ${badges}
            ${(hasOpens || hasClicks) ? `<button class="btn btn-sm btn-ghost" onclick="viewDetail('${e.id}','${esc(e.subject)}','${esc(e.recipient)}')">Details</button>` : ''}
            <button class="btn btn-sm btn-danger" onclick="deleteEmail('${e.id}')">×</button>
          </div>
        </div>
      `;
    }).join('');
  }

  async function createTracker() {
    const recipient = document.getElementById('recipient').value.trim();
    const subject = document.getElementById('subject').value.trim();
    if (!recipient || !subject) return;

    const res = await fetch('/api/track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recipient, subject })
    });

    const data = await res.json();
    currentEmailId = data.email_id;
    currentPixelHTML = data.img_tag;

    document.getElementById('pixelCode').textContent = data.img_tag;
    document.getElementById('pixelResult').classList.add('show');
    document.getElementById('linkSection').classList.add('show');
    document.getElementById('trackedLinks').innerHTML = '';
    document.getElementById('recipient').value = '';
    document.getElementById('subject').value = '';
    loadEmails();
  }

  async function createLink() {
    const url = document.getElementById('linkUrl').value.trim();
    const label = document.getElementById('linkLabel').value.trim();
    if (!url || !currentEmailId) return;

    const res = await fetch('/api/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email_id: currentEmailId, url, label })
    });

    const data = await res.json();
    const container = document.getElementById('trackedLinks');
    container.innerHTML += `
      <div class="tracked-link-item">
        <span class="label">${esc(label || 'Link')}</span>
        <span class="url" title="${esc(data.tracked_url)}">${esc(data.tracked_url)}</span>
        <button class="copy-small" onclick="navigator.clipboard.writeText('${data.tracked_url}');this.textContent='Done!';setTimeout(()=>this.textContent='Copy',1500)">Copy</button>
      </div>
    `;

    document.getElementById('linkUrl').value = '';
    document.getElementById('linkLabel').value = '';
  }

  async function viewDetail(emailId, subject, recipient) {
    const res = await fetch(`/api/emails/${emailId}`);
    const data = await res.json();

    document.getElementById('modalTitle').textContent = subject;
    document.getElementById('modalSub').textContent = `To: ${recipient}`;

    document.getElementById('tab-opens').innerHTML = data.opens.length
      ? data.opens.map(o => {
          const time = new Date(o.opened_at + 'Z').toLocaleString();
          return `<div class="open-entry">
            <div class="open-time">${time}<span class="method-tag ${o.method}">${o.method}</span></div>
            <div class="open-details">IP: ${o.ip_address}<br>${o.user_agent}</div>
          </div>`;
        }).join('')
      : '<p style="color:var(--text-dim);padding:20px 0">No opens recorded yet.</p>';

    document.getElementById('tab-clicks').innerHTML = data.clicks.length
      ? data.clicks.map(c => {
          const time = new Date(c.clicked_at + 'Z').toLocaleString();
          return `<div class="open-entry">
            <div class="open-time">${time}<span class="method-tag link">${esc(c.label || 'link')}</span></div>
            <div class="open-details">URL: ${esc(c.original_url)}<br>IP: ${c.ip_address}</div>
          </div>`;
        }).join('')
      : '<p style="color:var(--text-dim);padding:20px 0">No clicks recorded yet.</p>';

    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector('.tab').classList.add('active');
    document.getElementById('tab-opens').classList.add('active');

    document.getElementById('modalOverlay').classList.add('show');
  }

  function switchTab(name, el) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
  }

  function closeModal(e) {
    if (e.target === document.getElementById('modalOverlay'))
      document.getElementById('modalOverlay').classList.remove('show');
  }

  async function deleteEmail(id) {
    await fetch(`/api/emails/${id}`, { method: 'DELETE' });
    loadEmails();
  }

  function copyText(id) {
    const text = document.getElementById(id).textContent;
    navigator.clipboard.writeText(text).then(() => {
      const btn = document.getElementById(id).parentElement.querySelector('.copy-btn');
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 2000);
    });
  }

  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  loadEmails();
  setInterval(loadEmails, 30000);
</script>
</body>
</html>
"""

# Initialize DB on startup
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
