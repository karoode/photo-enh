import os
import requests
import mimetypes
import sqlite3
import json
from datetime import datetime
from functools import wraps
import re

from flask import Flask, request, jsonify, Response, g, redirect, url_for, abort

# ========= Environment variables =========
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

ADMIN_USERNAME   = _must_env("ADMIN_USERNAME")
ADMIN_PASSWORD   = _must_env("ADMIN_PASSWORD")

TZ_NAME          = os.getenv("TZ", "Asia/Baghdad")
WEBHOOK_DEBUG    = os.getenv("WEBHOOK_DEBUG", "0") == "1"

UPLOAD_FOLDER    = "/tmp/whatsapp_images"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DISK_MOUNT_PATH  = os.getenv("DISK_MOUNT_PATH", "/var/data")
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
    return datetime.now(TZ) if TZ else datetime.now()

def today_str():
    return now_local().strftime("%Y-%m-%d")

# ========= Database =========
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
            ts   TEXT NOT NULL,
            day  TEXT NOT NULL,
            phone TEXT NOT NULL,
            name  TEXT
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

def daily_counts(limit_days=365):
    db = get_db()
    cur = db.execute(
        "SELECT day, COUNT(*) as cnt FROM sent_images GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit_days,)
    )
    rows = cur.fetchall()
    return list(reversed([(r["day"], r["cnt"]) for r in rows]))

def list_days(limit_days=365):
    db = get_db()
    cur = db.execute(
        "SELECT day, COUNT(*) as cnt FROM sent_images GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit_days,)
    )
    return cur.fetchall()

def rows_by_day(day):
    db = get_db()
    cur = db.execute(
        "SELECT ts, phone, name FROM sent_images WHERE day = ? ORDER BY ts DESC",
        (day,)
    )
    return cur.fetchall()

def today_rows():
    return rows_by_day(today_str())

# ========= WhatsApp API helpers =========
def upload_media(file_path):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with open(file_path, 'rb') as f:
        files = {'file': (os.path.basename(file_path), f, mime_type)}
        data = {"messaging_product": "whatsapp"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    print("Upload response:", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()["id"]

def send_template_with_media_id(to_number, media_id, name_param):
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
                {"type": "header","parameters":[{"type":"image","image":{"id": media_id}}]},
                {"type": "body","parameters":[{"type":"text","text": name_param}]}
            ]
        }
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    print("Send response:", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()

# ========= Webhook (GET + POST filtered) =========
def _is_for_this_number(value: dict) -> bool:
    try:
        meta = value.get("metadata") or {}
        pnid = str(meta.get("phone_number_id") or "").strip()
        return pnid == str(PHONE_NUMBER_ID).strip()
    except Exception:
        return False

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    # POST
    payload = request.get_json(force=True, silent=True) or {}
    entries = payload.get("entry", []) or []

    for entry in entries:
        for ch in (entry.get("changes", []) or []):
            value = ch.get("value") or {}
            if not _is_for_this_number(value):
                # صامت: لا نطبع شيء نهائياً
                continue
            if WEBHOOK_DEBUG:
                # طباعة مختصرة عند الحاجة فقط
                if isinstance(value.get("statuses"), list):
                    for st in value["statuses"]:
                        print(f"[webhook] status id={st.get('id')} status={st.get('status')} ts={st.get('timestamp')}")
                if isinstance(value.get("messages"), list):
                    for msg in value["messages"]:
                        print(f"[webhook] inbound id={msg.get('id')} from={msg.get('from')} type={msg.get('type')}")

    return "OK", 200

# ========= API endpoint =========
@app.route("/send-image", methods=["POST"])
def send_image():
    if "file" not in request.files or "to" not in request.form:
        return jsonify(error="Missing file or to"), 400

    phone_number = request.form["to"].strip()
    user_name = (request.form.get("name") or "").strip()
    file = request.files["file"]

    safe_name = re.sub(r"[^\w\.\-]+", "_", file.filename or "upload.jpg")
    save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(save_path)

    try:
        media_id = upload_media(save_path)
        result = send_template_with_media_id(phone_number, media_id, user_name or "User")
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

# ========= Auth =========
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response("Authentication required", 401,
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

# ========= Admin HTML helpers =========
def _rows_table_html(rows):
    if not rows:
        return '<tr><td colspan="3" class="text-muted">لا توجد بيانات بعد اليوم.</td></tr>'
    parts = []
    for r in rows:
        parts.append(
            f"<tr><td>{r['ts']}</td><td>{r['phone']}</td><td>{(r['name'] or '')}</td></tr>"
        )
    return "".join(parts)

def _days_table_html(days_rows):
    if not days_rows:
        return '<tr><td colspan="3" class="text-muted">لا توجد بيانات بعد.</td></tr>'
    out = []
    for r in days_rows:
        link = url_for("admin_day", day=r["day"])
        jurl = url_for("admin_day_json", day=r["day"])
        out.append(
            f'<tr><td><a href="{link}">{r["day"]}</a></td>'
            f'<td><span class="badge bg-primary">{r["cnt"]}</span></td>'
            f'<td><a class="btn btn-sm btn-outline-secondary" href="{jurl}" target="_blank">JSON</a></td></tr>'
        )
    return "".join(out)

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
    days = list_days(limit_days=7)
    quick_links = "".join(
        f'<a class="btn btn-sm btn-outline-primary me-2 mb-2" href="{url_for("admin_day", day=d["day"])}">{d["day"]} <span class="badge bg-primary">{d["cnt"]}</span></a>'
        for d in days
    )
    rows_html = _rows_table_html(rows)

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
    body {{ background:#f4f6fb; color:#111827; }}
    .card {{ background:#ffffff; border:1px solid #e5e7eb; }}
    .table thead th {{ color:#374151; }}
    .muted {{ color:#6b7280; font-size:0.9rem; }}
    a, a:visited {{ color:#2563eb; }}
    .chip {{ background:#eef2ff; color:#3730a3; border:1px solid #c7d2fe; border-radius:999px; padding:.25rem .75rem; display:inline-block; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex flex-wrap align-items-center justify-content-between mb-4">
      <h3 class="m-0">لوحة التحكم (اليوم)</h3>
      <div class="muted">تاريخ اليوم: {today_str()}</div>
    </div>

    <div class="d-flex flex-wrap align-items-center mb-3">
      <a class="btn btn-dark me-2 mb-2" href="{url_for('admin_days')}">قائمة كل الأيام</a>
      {quick_links}
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
            <table class="table table-hover align-middle">
              <thead class="table-light">
                <tr>
                  <th style="width: 220px;">الوقت</th>
                  <th style="width: 220px;">الرقم</th>
                  <th>الاسم</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
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
      datasets: [{{ label: 'عدد الصور', data: counts }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color: '#111827' }} }},
        y: {{ ticks: {{ color: '#111827' }}, beginAtZero: true, precision: 0 }}
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
    data = [{"day": d, "count": c} for d, c in daily_counts(limit_days=365)]
    return jsonify(data)

# ======== أيام ========
@app.route("/admin/days")
@requires_auth
def admin_days():
    init_db()
    rows = list_days(limit_days=365)
    html_rows = _days_table_html(rows)

    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>قائمة الأيام - WhatsApp Images</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {{ background:#f4f6fb; color:#111827; }}
    .card {{ background:#ffffff; border:1px solid #e5e7eb; }}
    .muted {{ color:#6b7280; font-size:0.9rem; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex align-items-center justify-content-between mb-4">
      <h3 class="m-0">قائمة كل الأيام</h3>
      <div><a class="btn btn-dark" href="{url_for('admin_panel')}">عودة إلى اليوم</a></div>
    </div>

    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-hover align-middle">
          <thead class="table-light">
            <tr>
              <th style="width:220px;">اليوم</th>
              <th style="width:120px;">العدد</th>
              <th>روابط</th>
            </tr>
          </thead>
          <tbody>
            {html_rows}
          </tbody>
        </table>
      </div>
      <div class="muted">* اضغط على اليوم لعرض التفاصيل.</div>
    </div>
  </div>
</body>
</html>
    """
    return html

# ======== تفاصيل يوم ========
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

@app.route("/admin/day/<day>")
@requires_auth
def admin_day(day):
    init_db()
    if not DAY_RE.match(day):
        abort(400, description="صيغة اليوم غير صحيحة. استخدم YYYY-MM-DD")

    rows = rows_by_day(day)
    count_day = len(rows)
    rows_html = _rows_table_html(rows)

    all_days = [r["day"] for r in list_days(limit_days=365)]
    prev_link = next_link = ""
    if day in all_days:
        idx = all_days.index(day)
        if idx + 1 < len(all_days):
            prev_link = f'<a class="btn btn-outline-primary me-2" href="{url_for("admin_day", day=all_days[idx+1])}">اليوم السابق</a>'
        if idx - 1 >= 0:
            next_link = f'<a class="btn btn-outline-primary" href="{url_for("admin_day", day=all_days[idx-1])}">اليوم التالي</a>'

    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>تفاصيل اليوم {day} - WhatsApp Images</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {{ background:#f4f6fb; color:#111827; }}
    .card {{ background:#ffffff; border:1px solid #e5e7eb; }}
    .muted {{ color:#6b7280; font-size:0.9rem; }}
    .chip {{ background:#eef2ff; color:#3730a3; border:1px solid #c7d2fe; border-radius:999px; padding:.25rem .75rem; display:inline-block; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex flex-wrap align-items-center justify-content-between mb-4">
      <h3 class="m-0">تفاصيل اليوم: {day}</h3>
      <div class="d-flex align-items-center">
        <a class="btn btn-dark me-2" href="{url_for('admin_days')}">قائمة كل الأيام</a>
        <a class="btn btn-secondary" href="{url_for('admin_panel')}">اليوم الحالي</a>
      </div>
    </div>

    <div class="mb-3">
      {prev_link} {next_link}
      <span class="chip ms-2">المجموع: {count_day}</span>
      <a class="btn btn-outline-secondary ms-2" href="{url_for('admin_day_json', day=day)}" target="_blank">JSON</a>
    </div>

    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-hover align-middle">
          <thead class="table-light">
            <tr>
              <th style="width: 220px;">الوقت</th>
              <th style="width: 220px;">الرقم</th>
              <th>الاسم</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </div>
      <div class="muted">* الأحدث أولاً. يتم تسجيل العملية فقط عند نجاح الإرسال.</div>
    </div>
  </div>
</body>
</html>
    """
    return html

@app.route("/admin/day/<day>.json")
@requires_auth
def admin_day_json(day):
    init_db()
    if not DAY_RE.match(day):
        abort(400, description="صيغة اليوم غير صحيحة. استخدم YYYY-MM-DD")
    rows = rows_by_day(day)
    data = [{"ts": r["ts"], "phone": r["phone"], "name": r["name"]} for r in rows]
    return jsonify({"day": day, "count": len(rows), "rows": data})

# ========= Main =========
if __name__ == "__main__":
    with app.app_context():
        init_db()
    port = int(os.getenv("PORT", "8000"))
    # عند النشر على Render استخدم أمر تشغيل مثل:
    # gunicorn -b 0.0.0.0:$PORT WhatsBot:app
    app.run(host="0.0.0.0", port=port)
