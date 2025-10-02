import os
import requests
import mimetypes
import sqlite3
import json
import re
from datetime import datetime
from functools import wraps

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
    if TZ:
        return datetime.now(TZ)
    return datetime.now()

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
    """إنشاء الجداول الأساسية."""
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,         -- وقت الإرسال المحلي
            day TEXT NOT NULL,        -- YYYY-MM-DD
            phone TEXT NOT NULL,
            name TEXT,
            message_id TEXT,          -- wamid من واتساب
            status TEXT,              -- آخر حالة معروفة (sent/delivered/read/failed)
            status_ts TEXT            -- توقيت آخر حالة (من واتساب، إن وُجد)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sent_images_day ON sent_images(day)
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sent_images_msgid ON sent_images(message_id)
        """
    )
    # جدول أحداث الويبهوك الخام + بعض الحقول المفيدة
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,         -- وقت الاستلام المحلي
            day TEXT NOT NULL,        -- YYYY-MM-DD
            object TEXT,              -- من Meta (عادة whatsapp_business_account)
            entry_id TEXT,
            change_field TEXT,        -- غالباً messages
            event_type TEXT,          -- status/message/unknown
            message_id TEXT,          -- wamid إذا وُجد
            status TEXT,              -- delivered/read/failed... (للstatus)
            from_wa TEXT,             -- مرسل (للرسائل الواردة)
            to_wa TEXT,               -- مستقبل (إن وُجد)
            raw_json TEXT NOT NULL    -- الحدث بالكامل
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_webhook_events_day ON webhook_events(day)
        """
    )
    db.commit()

def migrate_db():
    """محاولة خفيفة لإضافة أعمدة لو قاعدة قديمة."""
    db = get_db()
    # نحاول إضافة أعمدة جديدة لو ناقصة (بدون كسر)
    for col_def in [
        ("sent_images", "message_id TEXT"),
        ("sent_images", "status TEXT"),
        ("sent_images", "status_ts TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]}")
            db.commit()
        except Exception:
            pass  # موجودة مسبقاً

def record_send(phone, name, message_id=None):
    db = get_db()
    t = now_local().isoformat(timespec="seconds")
    d = today_str()
    db.execute(
        "INSERT INTO sent_images (ts, day, phone, name, message_id) VALUES (?, ?, ?, ?, ?)",
        (t, d, phone, name or None, message_id),
    )
    db.commit()

def update_status_by_message_id(message_id, status, status_ts=None, to_wa=None):
    if not message_id:
        return
    db = get_db()
    db.execute(
        "UPDATE sent_images SET status = ?, status_ts = ? WHERE message_id = ?",
        (status, status_ts, message_id)
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
        "SELECT ts, phone, name, message_id, status, status_ts FROM sent_images WHERE day = ? ORDER BY ts DESC",
        (day,)
    )
    return cur.fetchall()

def today_rows():
    return rows_by_day(today_str())

def insert_webhook_event(obj, entry_id, change_field, event_type, message_id, status, from_wa, to_wa, raw_json):
    db = get_db()
    t = now_local().isoformat(timespec="seconds")
    d = today_str()
    db.execute(
        """
        INSERT INTO webhook_events (ts, day, object, entry_id, change_field, event_type, message_id, status, from_wa, to_wa, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (t, d, obj, entry_id, change_field, event_type, message_id, status, from_wa, to_wa, raw_json)
    )
    db.commit()

def webhook_events_by_day(day, limit=500):
    db = get_db()
    cur = db.execute(
        "SELECT ts, object, entry_id, change_field, event_type, message_id, status, from_wa, to_wa FROM webhook_events WHERE day = ? ORDER BY ts DESC LIMIT ?",
        (day, limit)
    )
    return cur.fetchall()

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
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    print("Send response:", resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json()
    # نرجّع wamid إن وجد
    msg_id = None
    try:
        msg_id = data["messages"][0]["id"]
    except Exception:
        pass
    return data, msg_id

# ========= API endpoint =========
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

    # Save file temporarily (اسم آمن)
    safe_name = re.sub(r"[^\w\.\-]+", "_", file.filename or "upload.jpg")
    save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(save_path)

    try:
        media_id = upload_media(save_path)
        result, msg_id = send_template_with_media_id(phone_number, media_id, user_name or "User")
        # نسجّل فقط عند النجاح + نخزن wamid
        try:
            init_db(); migrate_db()
            record_send(phone_number, user_name or "User", msg_id)
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

# ========= Webhook verification (GET) + receiver (POST) =========
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    # POST: استلام أحداث واتساب
    try:
        init_db(); migrate_db()
        data = request.get_json(force=True, silent=True)
        if not data:
            # ندوّن حدث خام بدون JSON واضح
            insert_webhook_event(None, None, None, "unknown", None, None, None, None, request.data.decode("utf-8", errors="ignore") or "")
            return "OK", 200

        obj = data.get("object")
        entries = data.get("entry", [])
        for entry in entries:
            entry_id = entry.get("id")
            changes = entry.get("changes", [])
            for ch in changes:
                field = ch.get("field")
                value = ch.get("value", {})
                # statuses: حالات رسائل أرسلتها أنت
                for st in value.get("statuses", []) if isinstance(value.get("statuses", []), list) else []:
                    message_id = st.get("id")
                    status = st.get("status")
                    ts_utc = st.get("timestamp")  # ثواني يونكس كسلسلة
                    to_wa = st.get("recipient_id")
                    # حدّث جدول sent_images
                    status_ts = None
                    try:
                        if ts_utc:
                            status_ts = datetime.utcfromtimestamp(int(ts_utc)).isoformat(timespec="seconds") + "Z"
                    except Exception:
                        pass

                    update_status_by_message_id(message_id, status, status_ts, to_wa)
                    insert_webhook_event(obj, entry_id, field, "status", message_id, status, None, to_wa, json.dumps(ch, ensure_ascii=False))

                # messages: رسائل واردة من المستخدمين
                for msg in value.get("messages", []) if isinstance(value.get("messages", []), list) else []:
                    message_id = msg.get("id")
                    from_wa = msg.get("from")
                    to_wa = None
                    # قد تظهر metadata للـ phone_number_id
                    meta = value.get("metadata", {})
                    if isinstance(meta, dict):
                        to_wa = meta.get("phone_number_id")
                    insert_webhook_event(obj, entry_id, field, "message", message_id, None, from_wa, to_wa, json.dumps(ch, ensure_ascii=False))

        return "OK", 200
    except Exception as e:
        # نسجل الحدث الخام على الأقل
        try:
            insert_webhook_event(None, None, None, "error", None, None, None, None, request.data.decode("utf-8", errors="ignore") or str(e))
        except Exception:
            pass
        return "OK", 200  # نرجع 200 حتى ما يعاود الإرسال

# ========= Basic Auth =========
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
        if not auth or not check_auth(auth.username, password=auth.password):
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
    init_db(); migrate_db()
    rows = today_rows()
    count_today = len(rows)

    days = list_days(limit_days=7)
    quick_links = "".join(
        f'<a class="btn btn-sm btn-outline-primary me-2 mb-2" href="{url_for("admin_day", day=d["day"])}">{d["day"]} <span class="badge bg-primary">{d["cnt"]}</span></a>'
        for d in days
    )

    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>لوحة التحكم (اليوم) - WhatsApp Images</title>
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
    code {{ background:#f3f4f6; padding:2px 4px; border-radius:4px; }}
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
      <a class="btn btn-secondary me-2 mb-2" href="{url_for('admin_webhook_day', day=today_str())}">أحداث الويبهوك لليوم</a>
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
          <div class="mt-2">
            <span class="badge bg-secondary">يربط الحالة من الويبهوك تلقائياً</span>
          </div>
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
                  <th style="width: 180px;">الوقت</th>
                  <th style="width: 180px;">الرقم</th>
                  <th>الاسم</th>
                  <th style="width: 320px;">الحالة (آخر تحديث)</th>
                  <th>wamid</th>
                </tr>
              </thead>
              <tbody>
                {''.join(f"<tr><td>{r['ts']}</td><td>{r['phone']}</td><td>{(r['name'] or '')}</td><td>{(r['status'] or '-')}<br><small class='text-muted'>{(r['status_ts'] or '')}</small></td><td><code>{(r['message_id'] or '')}</code></td></tr>" for r in rows) or '<tr><td colspan=\"5\" class=\"text-muted\">لا توجد بيانات بعد اليوم.</td></tr>'}
              </tbody>
            </table>
          </div>
          <div class="muted">* يتم تسجيل العملية فقط عند نجاح الإرسال. الحالة تُحدّث لاحقاً من إشعارات واتساب (ويبهوك).</div>
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
    init_db(); migrate_db()
    data = [{"day": d, "count": c} for d, c in daily_counts(limit_days=365)]
    return jsonify(data)

# ======== قائمة الأيام ========
@app.route("/admin/days")
@requires_auth
def admin_days():
    init_db(); migrate_db()
    rows = list_days(limit_days=365)
    html_rows = "".join(
        f"""
        <tr>
          <td><a href="{url_for('admin_day', day=r['day'])}">{r['day']}</a></td>
          <td><span class="badge bg-primary">{r['cnt']}</span></td>
          <td>
            <a class="btn btn-sm btn-outline-secondary me-1" href="{url_for('admin_day_json', day=r['day'])}" target="_blank">JSON</a>
            <a class="btn btn-sm btn-outline-dark" href="{url_for('admin_webhook_day', day=r['day'])}">أحداث الويبهوك</a>
          </td>
        </tr>
        """
        for r in rows
    ) or '<tr><td colspan="3" class="text-muted">لا توجد بيانات بعد.</td></tr>'

    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>قائمة كل الأيام - WhatsApp Images</title>
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
      <div>
        <a class="btn btn-dark" href="{url_for('admin_panel')}">عودة إلى اليوم</a>
      </div>
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
      <div class="muted">* اضغط على اليوم لعرض التفاصيل (الأرقام والأسماء والحالات) لذلك اليوم، ويمكنك استعراض أحداث الويبهوك لنفس اليوم.</div>
    </div>
  </div>
</body>
</html>
    """
    return html

# ======== تفاصيل يوم واحد ========
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

@app.route("/admin/day/<day>")
@requires_auth
def admin_day(day):
    init_db(); migrate_db()
    if not DAY_RE.match(day):
        abort(400, description="صيغة اليوم غير صحيحة. استخدم YYYY-MM-DD")

    rows = rows_by_day(day)
    count_day = len(rows)

    all_days = [r["day"] for r in list_days(limit_days=365)]
    prev_link = next_link = ''
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
    code {{ background:#f3f4f6; padding:2px 4px; border-radius:4px; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex flex-wrap align-items-center justify-content-between mb-4">
      <h3 class="m-0">تفاصيل اليوم: {day}</h3>
      <div class="d-flex align-items-center">
        <a class="btn btn-dark me-2" href="{url_for('admin_days')}">قائمة كل الأيام</a>
        <a class="btn btn-secondary me-2" href="{url_for('admin_webhook_day', day=day)}">أحداث الويبهوك لليوم</a>
        <a class="btn btn-outline-dark" href="{url_for('admin_panel')}">اليوم الحالي</a>
      </div>
    </div>

    <div class="mb-3">
      {prev_link} {next_link}
      <span class="chip ms-2">المجموع: {count_day}</span>
    </div>

    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-hover align-middle">
          <thead class="table-light">
            <tr>
              <th style="width: 180px;">الوقت</th>
              <th style="width: 180px;">الرقم</th>
              <th>الاسم</th>
              <th style="width: 320px;">الحالة (آخر تحديث)</th>
              <th>wamid</th>
            </tr>
          </thead>
          <tbody>
            {''.join(f"<tr><td>{r['ts']}</td><td>{r['phone']}</td><td>{(r['name'] or '')}</td><td>{(r['status'] or '-')}<br><small class='text-muted'>{(r['status_ts'] or '')}</small></td><td><code>{(r['message_id'] or '')}</code></td></tr>" for r in rows) or '<tr><td colspan=\"5\" class=\"text-muted\">لا توجد بيانات لهذا اليوم.</td></tr>'}
          </tbody>
        </table>
      </div>
      <div class="muted">* الأحدث أولاً. الحالة تُحدّث من إشعارات واتساب.</div>
    </div>
  </div>
</body>
</html>
    """
    return html

@app.route("/admin/day/<day>.json")
@requires_auth
def admin_day_json(day):
    init_db(); migrate_db()
    if not DAY_RE.match(day):
        abort(400, description="صيغة اليوم غير صحيحة. استخدم YYYY-MM-DD")
    rows = rows_by_day(day)
    data = [
        {"ts": r["ts"], "phone": r["phone"], "name": r["name"], "message_id": r["message_id"], "status": r["status"], "status_ts": r["status_ts"]}
        for r in rows
    ]
    return jsonify({"day": day, "count": len(rows), "rows": data})

# ======== صفحة أحداث الويبهوك لليوم ========
@app.route("/admin/webhook/day/<day>")
@requires_auth
def admin_webhook_day(day):
    init_db(); migrate_db()
    if not DAY_RE.match(day):
        abort(400, description="صيغة اليوم غير صحيحة. استخدم YYYY-MM-DD")

    rows = webhook_events_by_day(day)
    count_day = len(rows)

    html_rows = "".join(
        f"<tr><td>{r['ts']}</td><td>{r['event_type'] or '-'}</td><td><code>{r['message_id'] or ''}</code></td><td>{r['status'] or '-'}</td><td>{r['from_wa'] or ''}</td><td>{r['to_wa'] or ''}</td><td>{r['object'] or ''}</td><td>{r['entry_id'] or ''}</td><td>{r['change_field'] or ''}</td></tr>"
        for r in rows
    ) or '<tr><td colspan="9" class="text-muted">لا توجد أحداث لهذا اليوم.</td></tr>'

    html = f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>أحداث الويبهوك {day}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {{ background:#f4f6fb; color:#111827; }}
    .card {{ background:#ffffff; border:1px solid #e5e7eb; }}
    .muted {{ color:#6b7280; font-size:0.9rem; }}
    code {{ background:#f3f4f6; padding:2px 4px; border-radius:4px; }}
  </style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex align-items-center justify-content-between mb-4">
      <h3 class="m-0">أحداث الويبهوك لليوم: {day}</h3>
      <div class="d-flex">
        <a class="btn btn-dark me-2" href="{url_for('admin_days')}">قائمة كل الأيام</a>
        <a class="btn btn-secondary" href="{url_for('admin_day', day=day)}">تفاصيل الإرسال لليوم</a>
      </div>
    </div>

    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-hover align-middle">
          <thead class="table-light">
            <tr>
              <th>الوقت</th>
              <th>النوع</th>
              <th>wamid</th>
              <th>الحالة</th>
              <th>from</th>
              <th>to</th>
              <th>object</th>
              <th>entry_id</th>
              <th>field</th>
            </tr>
          </thead>
          <tbody>
            {html_rows}
          </tbody>
        </table>
      </div>
      <div class="muted">* هذه الصفحة تُظهر ملخص الحقول المفيدة من أحداث الويبهوك. الحدث الخام محفوظ في قاعدة البيانات (عمود raw_json) إن احتجته لاحقاً.</div>
    </div>
  </div>
</body>
</html>
    """
    return html

# ========= Main =========
if __name__ == "__main__":
    with app.app_context():
        init_db()
        migrate_db()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
