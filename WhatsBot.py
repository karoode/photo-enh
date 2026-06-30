import os
import io
import re
import json
import time
import uuid
import base64
import mimetypes
import sqlite3
import tempfile
from datetime import datetime
from functools import wraps

import requests
from PIL import Image
from flask import Flask, request, jsonify, Response, g, send_file, redirect, url_for, abort
from openai import OpenAI

# =========================================================
# ENV
# =========================================================
def _env(key: str, default=None, required: bool = False):
    value = os.getenv(key, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {key}")
    return value

# OpenAI image enhancement
OPENAI_API_KEY       = _env("OPENAI_API_KEY", required=True)
OPENAI_MODEL         = _env("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_QUALITY       = _env("OPENAI_IMAGE_QUALITY", "high")
OPENAI_SIZE          = _env("OPENAI_IMAGE_SIZE", "auto")
OPENAI_TIMEOUT       = float(_env("OPENAI_TIMEOUT", "120"))
ENHANCE_TARGET_SEC   = float(_env("ENHANCE_TARGET_SEC", "10"))

# WhatsApp Cloud API
VERIFY_TOKEN         = _env("VERIFY_TOKEN", "")
WHATSAPP_TOKEN       = _env("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID      = _env("PHONE_NUMBER_ID", "")
GRAPH_VERSION        = _env("GRAPH_VERSION", "v21.0")
TEMPLATE_NAME        = _env("TEMPLATE_NAME", "send_photo")

# Admin
ADMIN_USERNAME       = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD       = _env("ADMIN_PASSWORD", "admin")

# Storage
DISK_MOUNT_PATH      = _env("DISK_MOUNT_PATH", "/var/data")
DB_FILE_NAME         = _env("DB_FILE_NAME", "whatsapp_stats.db")

UPLOAD_FOLDER        = "/tmp/whatsapp_images"
TMP_FOLDER           = "/tmp/openai_enhance"
ORIGINALS_DIR        = os.path.join(DISK_MOUNT_PATH, "originals")
ENHANCED_DIR         = os.path.join(DISK_MOUNT_PATH, "enhanced")
SENT_DIR             = os.path.join(DISK_MOUNT_PATH, "sent")
DB_PATH              = os.path.join(DISK_MOUNT_PATH, DB_FILE_NAME)

for folder in [UPLOAD_FOLDER, TMP_FOLDER, ORIGINALS_DIR, ENHANCED_DIR, SENT_DIR, os.path.dirname(DB_PATH)]:
    os.makedirs(folder, exist_ok=True)

# =========================================================
# APP
# =========================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)

DEFAULT_ENHANCE_PROMPT = """Enhance this image realistically without changing the person's identity, face shape, expression, hairstyle, beard, clothing, pose, background composition, logo, QR code, text, colors, or layout.

Improve only the technical quality: increase sharpness, restore facial details, reduce blur and noise, improve skin clarity naturally, balance lighting, recover details in the eyes, eyebrows, hair, and beard, and make the image look clean and professional.

Keep the result photorealistic and natural. Do not beautify, do not stylize, do not make it look like AI-generated, do not change facial features, do not change the person’s age, do not alter the purple footer banner, QR code, ALJAZARI logo, website, phone number, or Arabic/English text.

Output the enhanced image in the same aspect ratio and preserve the original design exactly.
"""

RESAMPLE = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

# =========================================================
# LOGGING
# =========================================================
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def log_event(event: str, **kwargs):
    payload = {"ts": now_iso(), "event": event, **kwargs}
    print(json.dumps(payload, ensure_ascii=False), flush=True)

# =========================================================
# DB
# =========================================================
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
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            phone TEXT NOT NULL,
            name TEXT,
            image_kind TEXT,
            app_id TEXT,
            job_id TEXT
        )
        """
    )
    db.commit()

def record_send(phone, name, image_kind, app_id, job_id):
    db = get_db()
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO sent_images (ts, day, phone, name, image_kind, app_id, job_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (t, d, phone, name or None, image_kind or None, app_id or None, job_id or None),
    )
    db.commit()

with app.app_context():
    init_db()

# =========================================================
# HELPERS
# =========================================================
def safe_name(name: str) -> str:
    return re.sub(r"[^\w\.\-]+", "_", name or "file")

def bool_from_form(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")

def float_from_form(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def save_filestorage_temp(file_storage, folder=TMP_FOLDER):
    filename = safe_name(file_storage.filename or "upload.png")
    fd, path = tempfile.mkstemp(prefix="upload_", suffix="_" + filename, dir=folder)
    os.close(fd)
    file_storage.save(path)
    return path

def copy_to_persistent(src_path, target_dir, prefix="file"):
    ext = os.path.splitext(src_path)[1] or ".png"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = os.path.join(target_dir, f"{prefix}_{ts}{ext}")
    with open(src_path, "rb") as rf, open(dst, "wb") as wf:
        wf.write(rf.read())
    return dst

def openai_image_edit(input_path: str, prompt: str):
    kwargs = {
        "model": OPENAI_MODEL,
        "image": open(input_path, "rb"),
        "prompt": prompt,
        "quality": OPENAI_QUALITY,
    }
    if OPENAI_SIZE:
        kwargs["size"] = OPENAI_SIZE

    try:
        return client.images.edit(**kwargs)
    finally:
        try:
            kwargs["image"].close()
        except Exception:
            pass

def enhance_with_openai(input_path: str, prompt: str, preserve_footer: bool = False, footer_ratio: float = 0.22):
    t0 = time.perf_counter()
    result = openai_image_edit(input_path, prompt)
    openai_duration = time.perf_counter() - t0

    if not getattr(result, "data", None):
        raise RuntimeError("OpenAI returned no image data.")

    item = result.data[0]
    if getattr(item, "b64_json", None):
        image_bytes = base64.b64decode(item.b64_json)
    elif getattr(item, "url", None):
        r = requests.get(item.url, timeout=OPENAI_TIMEOUT)
        r.raise_for_status()
        image_bytes = r.content
    else:
        raise RuntimeError("OpenAI response did not contain image bytes.")

    with Image.open(input_path) as orig_img:
        orig_img = orig_img.convert("RGB")
        orig_w, orig_h = orig_img.size

        with Image.open(io.BytesIO(image_bytes)) as enh_img:
            enh_img = enh_img.convert("RGB")
            enh_img = enh_img.resize((orig_w, orig_h), RESAMPLE)

            # Preserve bottom footer/banner/QR/text exactly from original if requested.
            if preserve_footer:
                footer_h = int(orig_h * footer_ratio)
                if footer_h > 0:
                    footer_region = orig_img.crop((0, orig_h - footer_h, orig_w, orig_h))
                    enh_img.paste(footer_region, (0, orig_h - footer_h))

            fd, out_path = tempfile.mkstemp(prefix="enhanced_", suffix=".png", dir=TMP_FOLDER)
            os.close(fd)
            enh_img.save(out_path, format="PNG")

    return out_path, openai_duration

# =========================================================
# WHATSAPP API
# =========================================================
def ensure_whatsapp_config():
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN and PHONE_NUMBER_ID are required for /send-image")

def upload_media(file_path, request_id):
    ensure_whatsapp_config()
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    log_event("WHATSAPP_MEDIA_UPLOAD_START", request_id=request_id, file_size=file_size(file_path), mime_type=mime_type)
    t0 = time.perf_counter()
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, mime_type)}
        data = {"messaging_product": "whatsapp"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=180)
    duration = time.perf_counter() - t0
    log_event("WHATSAPP_MEDIA_UPLOAD_END", request_id=request_id, status_code=resp.status_code, duration_sec=round(duration, 3), response=resp.text[:500])
    resp.raise_for_status()
    return resp.json()["id"]

def send_template_with_media_id(to_number, media_id, name_param, request_id):
    ensure_whatsapp_config()
    if not name_param:
        name_param = "User"

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": "en"},
            "components": [
                {"type": "header", "parameters": [{"type": "image", "image": {"id": media_id}}]},
                {"type": "body", "parameters": [{"type": "text", "text": name_param}]},
            ],
        },
    }

    log_event("WHATSAPP_SEND_START", request_id=request_id, to=to_number, template=TEMPLATE_NAME)
    t0 = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    duration = time.perf_counter() - t0
    log_event("WHATSAPP_SEND_END", request_id=request_id, status_code=resp.status_code, duration_sec=round(duration, 3), response=resp.text[:500])
    resp.raise_for_status()
    return resp.json()

# =========================================================
# AUTH
# =========================================================
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Admin Panel"'})

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return wrapper

# =========================================================
# ROUTES
# =========================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "openai_model": OPENAI_MODEL,
        "openai_quality": OPENAI_QUALITY,
        "openai_size": OPENAI_SIZE,
        "enhance_target_sec": ENHANCE_TARGET_SEC,
        "whatsapp_enabled": bool(WHATSAPP_TOKEN and PHONE_NUMBER_ID),
    })

@app.route("/enhance", methods=["POST"])
def enhance_endpoint():
    request_id = str(uuid.uuid4())[:8]
    total_t0 = time.perf_counter()

    if "image" not in request.files:
        log_event("ENHANCE_BAD_REQUEST", request_id=request_id, reason="Missing image")
        return jsonify(error="Missing image"), 400

    image_file = request.files["image"]
    prompt = (request.form.get("prompt") or "").strip() or DEFAULT_ENHANCE_PROMPT
    job_id = (request.form.get("job_id") or "").strip()
    app_id = (request.form.get("app_id") or "unknown_app").strip()
    preserve_footer = bool_from_form(request.form.get("preserve_footer"), default=False)
    footer_ratio = float_from_form(request.form.get("footer_ratio"), default=0.22)

    tmp_input = None
    tmp_output = None

    try:
        log_event("ENHANCE_REQUEST_START", request_id=request_id, app_id=app_id, job_id=job_id, filename=image_file.filename, preserve_footer=preserve_footer, footer_ratio=footer_ratio)

        tmp_input = save_filestorage_temp(image_file)
        original_saved = copy_to_persistent(tmp_input, ORIGINALS_DIR, prefix=f"{app_id}_original")
        log_event("ENHANCE_ORIGINAL_SAVED", request_id=request_id, path=original_saved, file_size=file_size(tmp_input))

        tmp_output, openai_duration = enhance_with_openai(
            input_path=tmp_input,
            prompt=prompt,
            preserve_footer=preserve_footer,
            footer_ratio=footer_ratio,
        )

        enhanced_saved = copy_to_persistent(tmp_output, ENHANCED_DIR, prefix=f"{app_id}_enhanced")
        total_duration = time.perf_counter() - total_t0
        log_event(
            "ENHANCE_REQUEST_END",
            request_id=request_id,
            app_id=app_id,
            job_id=job_id,
            openai_duration_sec=round(openai_duration, 3),
            total_duration_sec=round(total_duration, 3),
            target_sec=ENHANCE_TARGET_SEC,
            over_target=total_duration > ENHANCE_TARGET_SEC,
            enhanced_path=enhanced_saved,
            output_size=file_size(tmp_output),
        )

        with open(tmp_output, "rb") as f:
            data = f.read()

        response = send_file(io.BytesIO(data), mimetype="image/png", as_attachment=False, download_name="enhanced.png")
        response.headers["X-Request-Id"] = request_id
        response.headers["X-App-Id"] = app_id
        response.headers["X-Job-Id"] = job_id
        response.headers["X-OpenAI-Duration-Sec"] = str(round(openai_duration, 3))
        response.headers["X-Total-Duration-Sec"] = str(round(total_duration, 3))
        return response

    except Exception as e:
        total_duration = time.perf_counter() - total_t0
        log_event("ENHANCE_REQUEST_ERROR", request_id=request_id, app_id=app_id, job_id=job_id, duration_sec=round(total_duration, 3), error=str(e))
        return jsonify(error=str(e), request_id=request_id), 500

    finally:
        for p in [tmp_input, tmp_output]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

@app.route("/send-image", methods=["POST"])
def send_image():
    request_id = str(uuid.uuid4())[:8]
    total_t0 = time.perf_counter()

    if "file" not in request.files or "to" not in request.form:
        log_event("SEND_BAD_REQUEST", request_id=request_id, reason="Missing file or to")
        return jsonify(error="Missing file or to"), 400

    phone_number = request.form["to"].strip()
    user_name = (request.form.get("name") or "").strip() or "User"
    app_id = (request.form.get("app_id") or "unknown_app").strip()
    job_id = (request.form.get("job_id") or "").strip()
    image_kind = (request.form.get("image_kind") or "unknown").strip()

    # Supported for other apps, but Promobot camera sends enhance=0.
    enhance_before_send = bool_from_form(request.form.get("enhance"), default=False)
    prompt = (request.form.get("prompt") or "").strip() or DEFAULT_ENHANCE_PROMPT
    preserve_footer = bool_from_form(request.form.get("preserve_footer"), default=False)
    footer_ratio = float_from_form(request.form.get("footer_ratio"), default=0.22)

    uploaded_file = request.files["file"]
    tmp_input = None
    tmp_output = None

    try:
        log_event("SEND_REQUEST_START", request_id=request_id, app_id=app_id, job_id=job_id, to=phone_number, name=user_name, image_kind=image_kind, enhance_requested=enhance_before_send, filename=uploaded_file.filename)

        tmp_input = save_filestorage_temp(uploaded_file, folder=UPLOAD_FOLDER)
        send_path = tmp_input
        log_event("SEND_FILE_RECEIVED", request_id=request_id, file_size=file_size(tmp_input))

        if enhance_before_send:
            log_event("SEND_INLINE_ENHANCE_START", request_id=request_id, note="Promobot camera should normally send enhance=0")
            tmp_output, openai_duration = enhance_with_openai(tmp_input, prompt, preserve_footer, footer_ratio)
            send_path = tmp_output
            image_kind = "enhanced_inline"
            enhanced_saved = copy_to_persistent(tmp_output, ENHANCED_DIR, prefix=f"{app_id}_send_enhanced")
            log_event("SEND_INLINE_ENHANCE_END", request_id=request_id, openai_duration_sec=round(openai_duration, 3), enhanced_path=enhanced_saved, output_size=file_size(tmp_output))
        else:
            saved_sent_copy = copy_to_persistent(tmp_input, SENT_DIR, prefix=f"{app_id}_{image_kind}_send")
            log_event("SEND_ENHANCE_SKIPPED", request_id=request_id, reason="background_enhancement_flow", saved_copy=saved_sent_copy)

        media_id = upload_media(send_path, request_id)
        result = send_template_with_media_id(phone_number, media_id, user_name, request_id)

        try:
            record_send(phone_number, user_name, image_kind, app_id, job_id)
        except Exception as db_err:
            log_event("SEND_DB_LOG_ERROR", request_id=request_id, error=str(db_err))

        total_duration = time.perf_counter() - total_t0
        log_event("SEND_REQUEST_END", request_id=request_id, app_id=app_id, job_id=job_id, image_kind=image_kind, total_duration_sec=round(total_duration, 3))

        return jsonify({
            "ok": True,
            "request_id": request_id,
            "image_kind": image_kind,
            "enhanced_before_send": enhance_before_send,
            "result": result,
        })

    except Exception as e:
        total_duration = time.perf_counter() - total_t0
        log_event("SEND_REQUEST_ERROR", request_id=request_id, app_id=app_id, job_id=job_id, image_kind=image_kind, duration_sec=round(total_duration, 3), error=str(e))
        return jsonify(error=str(e), request_id=request_id), 500

    finally:
        for p in [tmp_input, tmp_output]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    payload = request.get_json(force=True, silent=True) or {}
    log_event("WEBHOOK_RECEIVED", payload=payload)
    return "OK", 200

@app.route("/")
def root():
    return redirect(url_for("admin_panel"))

@app.route("/admin")
@requires_auth
def admin_panel():
    init_db()
    db = get_db()
    rows = db.execute("SELECT ts, phone, name, image_kind, app_id, job_id FROM sent_images ORDER BY id DESC LIMIT 100").fetchall()
    total = db.execute("SELECT COUNT(*) AS c FROM sent_images").fetchone()["c"]
    trs = "".join(
        f"<tr><td>{r['ts']}</td><td>{r['phone']}</td><td>{r['name'] or ''}</td><td>{r['image_kind'] or ''}</td><td>{r['app_id'] or ''}</td><td>{r['job_id'] or ''}</td></tr>"
        for r in rows
    ) or '<tr><td colspan="6">No data yet.</td></tr>'
    return f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp Image Server</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-4 bg-light">
<div class="container-fluid">
<h3>WhatsApp Image Server</h3>
<div class="alert alert-info">Total sent: <b>{total}</b> | OpenAI model: <b>{OPENAI_MODEL}</b> | quality: <b>{OPENAI_QUALITY}</b> | target: <b>{ENHANCE_TARGET_SEC}s</b></div>
<table class="table table-bordered table-striped bg-white">
<thead><tr><th>Time</th><th>Phone</th><th>Name</th><th>Image Kind</th><th>App</th><th>Job</th></tr></thead>
<tbody>{trs}</tbody>
</table>
</div>
</body>
</html>
"""

@app.route("/stats/today", methods=["GET"])
@requires_auth
def stats_today():
    init_db()
    db = get_db()
    day = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT ts, phone, name, image_kind, app_id, job_id FROM sent_images WHERE day = ? ORDER BY ts DESC",
        (day,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
