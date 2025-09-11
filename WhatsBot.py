import os
import requests
import mimetypes
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, Response, g, redirect, url_for

# ========= Environment variables (بدون قيم صلبة في الكود) =========
def _must_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v

VERIFY_TOKEN     = _must_env("VERIFY_TOKEN")
WHATSAPP_TOKEN   = _must_env("WHATSAPP_TOKEN")
PHONE_NUMBER_ID  = _must_env("PHONE_NUMBER_ID")

GRAPH_VERSION    = os.getenv("GRAPH_VERSION", "v21.0")
TEMPLATE_NAME    = os.getenv("TEMPLATE_NAME", "send_photo")

# Admin panel auth (لا قيم افتراضية هنا لأمان أعلى)
ADMIN_USERNAME   = _must_env("ADMIN_USERNAME")
ADMIN_PASSWORD   = _must_env("ADMIN_PASSWORD")

# Timezone
TZ_NAME          = os.getenv("TZ", "Asia/Baghdad")

# تخزين الملفات المؤقتة
UPLOAD_FOLDER    = "/tmp/whatsapp_images"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# مسار القرص الدائم على Render
DISK_MOUNT_PATH  = os.getenv("DISK_MOUNT_PATH", "/var/data")  # عدّل عند إنشاء القرص
DB_FILE_NAME     = os.getenv("DB_FILE_NAME", "whatsapp_stats.db")
DB_PATH          = os.path.join(DISK_MOUNT_PATH, DB_FILE_NAME)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ========= Flask =========
app = Flask(__name__)

# ========= Timezone helper =========
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = None

def now_local():
    if TZ:
        return datetime.now(TZ)
    return datetime.now()

def today_str():
    return now_local().strftime("%Y-%m-%d")

# ========= Database (SQLite على القرص الدائم) =========
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,     -- ISO timestamp
            day TEXT NOT NULL,    -- YYYY-MM-DD (حسب التوقيت المحلي)
            phone TEXT NOT NULL,
            name TEXT
        )
        """
    )
    db.commit()

def record_send(phone, name):
    db = get_db()
    t = now_local().isoformat(timespec="seconds")
    d = today_str()
    db.execute(
        "INSERT INTO sent_images (ts, day, phone, name) VALUES (?, ?, ?, ?)",
        (t, d, phone, name or None),
    )
    db.commit()

def daily_counts(limit_days=60):
    db = get_db()
    cur = db.execute(
        "SELECT day, COUNT(*) as cnt FROM sent_images GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit_days,)
    )
    rows = cur.fetchall()
    return list(reversed([(r["day"], r["cnt"]) for r in rows]))

def today_rows():
    db = get_db()
    cur = db.execute(
        "SELECT ts, phone, name FROM sent_images WHERE day = ? ORDER BY ts DESC",
        (today_str(),)
    )
    return cur.fetchall()

# ========= WhatsApp API helpers (أصلي) =========
def upload_media(file_path):
    """Uploads media to WhatsApp and returns media_id."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {
        'file': (os.path.basename(file_path), open(file_path, 'rb'), mime_type)
    }
    data = {"messaging_product": "whatsapp"}
    resp = requests.post(url, headers=headers, files=files, data=data)
    print("Upload response:", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()["id"]

def send_template_with_media_id(to_number, media_id, name_param):
    """Send approved template with uploaded image."""
    if not name_param:
        name_param = "User"
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": "en"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"id": media_id}}
                    ]
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": name_param}
                    ]
                }
            ]
        }
    }
    resp = requests.post(url, headers=headers, json=payload)
    print("Send response:", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()

# ========= API endpoint (أصلي بدون تغيير) =========
@app.route("/send-image", methods=["POST"])
def send_image():
    """
    Upload image and send it instantly via template.
    Required form fields:
      file: image file
      to: recipient phone number (international format, no +)
    Optional:
      name: name placeholder for template (defaults to "User" if missing/empty)
    """
    if "file" not in request.files or "to" not in request.form:
        return jsonify(error="Missing file or to"), 400

    phone_number = request.form["to"].strip()
    user_name = (request.form.get("name") or "").strip()
    file = request.files["file"]

    # Save file temporarily
    save_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(save_path)

    try:
        media_id = upload_media(save_path)
        result = send_template_with_media_id(phone_number, media_id, user_name or "User")
        # نسجّل فقط عند النجاح
        try:
            init_db()
            record_send(phone_number, user_name or "User")
        except Exception as log_err:
            print("Stats logging error:", log_err)
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        try:
            os.remove(save_path)
        except Exception:
            pass

# ========= Webhook verification (أصلي) =========
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ========= Basic Auth (لوحة التحكم) =========
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="Admin Panel"'}
    )

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return wrapper

# ========= Admin Panel =========
@app.route("/")
def root_redirect():
    return redirect(url_for("admin_panel"))

@app.route("/admin")
@requires_auth
def admin_panel():
    init_db()
    rows = today_rows()
    count_today = len(rows)
    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>لوحة التحكم - WhatsApp Images</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    body {{ background:#0f1220; color:#eaeaf2; }}
    .card {{ background:#1a1f36; border:none; }}
    .table thead th {{ color:#9aa4bd; }}
    .muted {{ color:#9aa4bd; font-size:0.9rem; }}
    a, a:visited {{ color:#8ab4ff; }}
    .chip {{ background:#2a2f45; border-radius:999px; padding:.25rem .75rem; display:inline-block; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex flex-wrap align-items-center justify-content-between mb-4">
      <h3 class="m-0">لوحة التحكم</h3>
      <div class="muted">تاريخ اليوم: {today_str()}</div>
    </div>

    <div class="row g-4">
      <div class="col-12 col-lg-4">
        <div class="card p-3">
          <div class="d-flex align-items-center justify-content-between">
            <div>
              <div class="muted">إجمالي الصور اليوم</div>
              <div class="display-6">{count_today}</div>
            </div>
            <div class="chip">{TZ_NAME}</div>
          </div>
          <div class="mt-2 muted">عدد الرسائل المُسجلة في قاعدة البيانات لليوم الحالي.</div>
        </div>
      </div>

      <div class="col-12 col-lg-8">
        <div class="card p-3">
          <div class="d-flex align-items-center justify-content-between mb-2">
            <div class="muted">الرسم البياني (عدد الصور اليومية)</div>
            <a href="{url_for('daily_json')}" target="_blank">JSON</a>
          </div>
          <canvas id="dailyChart" height="130"></canvas>
        </div>
      </div>

      <div class="col-12">
        <div class="card p-3">
          <div class="d-flex align-items-center justify-content-between">
            <div class="muted">عمليات اليوم (الأحدث أولاً)</div>
            <span class="chip">{count_today} عملية</span>
          </div>
          <div class="table-responsive mt-3">
            <table class="table table-dark table-hover align-middle">
              <thead>
                <tr>
                  <th style="width: 220px;">الوقت</th>
                  <th style="width: 220px;">الرقم</th>
                  <th>الاسم</th>
                </tr>
              </thead>
              <tbody>
                {''.join(f"<tr><td>{{r['ts']}}</td><td>{{r['phone']}}</td><td>{{(r['name'] or '')}}</td></tr>" for r in rows) or '<tr><td colspan="3" class="muted">لا توجد بيانات بعد اليوم.</td></tr>'}
              </tbody>
            </table>
          </div>
          <div class="muted">* يتم تسجيل العملية فقط عند نجاح الإرسال.</div>
        </div>
      </div>
    </div>
  </div>

<script>
async function loadDaily() {{
  const res = await fetch("{url_for('daily_json')}");
  const data = await res.json();
  const labels = data.map(d => d.day);
  const counts = data.map(d => d.count);

  const ctx = document.getElementById('dailyChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [{{
        label: 'عدد الصور',
        data: counts
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#c7d2fe' }} }},
        y: {{ ticks: {{ color: '#c7d2fe' }}, beginAtZero: true, precision: 0 }}
      }}
    }}
  }});
}}
loadDaily();
</script>
</body>
</html>
    """
    return html

@app.route("/admin/daily.json")
@requires_auth
def daily_json():
    init_db()
    data = [{"day": d, "count": c} for d, c in daily_counts(limit_days=60)]
    return jsonify(data)

# ========= Main =========
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=8000)
