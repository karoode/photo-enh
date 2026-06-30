import os
import io
import re
import base64
import mimetypes
import sqlite3
import tempfile
from datetime import datetime
from functools import wraps

import requests
from PIL import Image
from flask import Flask, request, jsonify, Response, g, send_file
from openai import OpenAI

# =========================================================
# ENV
# =========================================================
def _env(key: str, default=None, required=False):
    value = os.getenv(key, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {key}")
    return value

OPENAI_API_KEY   = _env("OPENAI_API_KEY", required=True)
OPENAI_MODEL     = _env("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_QUALITY   = _env("OPENAI_IMAGE_QUALITY", "high")

VERIFY_TOKEN     = _env("VERIFY_TOKEN", "")
WHATSAPP_TOKEN   = _env("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID  = _env("PHONE_NUMBER_ID", "")

GRAPH_VERSION    = _env("GRAPH_VERSION", "v21.0")
TEMPLATE_NAME    = _env("TEMPLATE_NAME", "send_photo")

ADMIN_USERNAME   = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD   = _env("ADMIN_PASSWORD", "admin")

DISK_MOUNT_PATH  = _env("DISK_MOUNT_PATH", "/var/data")
DB_FILE_NAME     = _env("DB_FILE_NAME", "whatsapp_stats.db")

UPLOAD_FOLDER    = "/tmp/whatsapp_images"
TMP_FOLDER       = "/tmp/openai_enhance"
ORIGINALS_DIR    = os.path.join(DISK_MOUNT_PATH, "originals")
ENHANCED_DIR     = os.path.join(DISK_MOUNT_PATH, "enhanced")
DB_PATH          = os.path.join(DISK_MOUNT_PATH, DB_FILE_NAME)

for p in [UPLOAD_FOLDER, TMP_FOLDER, ORIGINALS_DIR, ENHANCED_DIR, os.path.dirname(DB_PATH)]:
    os.makedirs(p, exist_ok=True)

# =========================================================
# APP
# =========================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

DEFAULT_ENHANCE_PROMPT = """Enhance this image realistically without changing the person's identity, face shape, expression, hairstyle, beard, clothing, pose, background composition, logo, QR code, text, colors, or layout.

Improve only the technical quality: increase sharpness, restore facial details, reduce blur and noise, improve skin clarity naturally, balance lighting, recover details in the eyes, eyebrows, hair, and beard, and make the image look clean and professional.

Keep the result photorealistic and natural. Do not beautify, do not stylize, do not make it look like AI-generated, do not change facial features, do not change the person’s age, do not alter the purple footer banner, QR code, ALJAZARI logo, website, phone number, or Arabic/English text.

Output the enhanced image in the same aspect ratio and preserve the original design exactly.
"""

RESAMPLE = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

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
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO sent_images (ts, day, phone, name) VALUES (?, ?, ?, ?)",
        (t, d, phone, name or None),
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

def enhance_with_openai(
    input_path: str,
    prompt: str,
    preserve_footer: bool = False,
    footer_ratio: float = 0.22,
):
    with open(input_path, "rb") as f:
        result = client.images.edit(
            model=OPENAI_MODEL,
            image=f,
            prompt=prompt,
            quality=OPENAI_QUALITY,
        )

    if not getattr(result, "data", None):
        raise RuntimeError("OpenAI returned no image data.")

    item = result.data[0]

    if getattr(item, "b64_json", None):
        image_bytes = base64.b64decode(item.b64_json)
    elif getattr(item, "url", None):
        resp = requests.get(item.url, timeout=180)
        resp.raise_for_status()
        image_bytes = resp.content
    else:
        raise RuntimeError("OpenAI response did not contain image bytes.")

    with Image.open(input_path) as orig_img:
        orig_img = orig_img.convert("RGB")
        orig_w, orig_h = orig_img.size

        with Image.open(io.BytesIO(image_bytes)) as enh_img:
            enh_img = enh_img.convert("RGB")
            enh_img = enh_img.resize((orig_w, orig_h), RESAMPLE)

            if preserve_footer:
                footer_h = int(orig_h * footer_ratio)
                if footer_h > 0:
                    footer_region = orig_img.crop((0, orig_h - footer_h, orig_w, orig_h))
                    enh_img.paste(footer_region, (0, orig_h - footer_h))

            fd, out_path = tempfile.mkstemp(prefix="enhanced_", suffix=".png", dir=TMP_FOLDER)
            os.close(fd)
            enh_img.save(out_path, format="PNG")

    return out_path

# =========================================================
# WHATSAPP HELPERS
# =========================================================
def ensure_whatsapp_config():
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN and PHONE_NUMBER_ID are required for /send-image")

def upload_media(file_path):
    ensure_whatsapp_config()
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, mime_type)}
        data = {"messaging_product": "whatsapp"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=180)
    print("Upload response:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()["id"]

def send_template_with_media_id(to_number, media_id, name_param):
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

    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    print("Send response:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# =========================================================
# AUTH (optional admin if you want later)
# =========================================================
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Admin Panel"'})

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
        "whatsapp_enabled": bool(WHATSAPP_TOKEN and PHONE_NUMBER_ID)
    })

@app.route("/enhance", methods=["POST"])
def enhance_endpoint():
    if "image" not in request.files:
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
        tmp_input = save_filestorage_temp(image_file)
        persistent_original = copy_to_persistent(tmp_input, ORIGINALS_DIR, prefix=f"{app_id}_original")
        print(f"[ENHANCE] app_id={app_id} job_id={job_id} original={persistent_original}", flush=True)

        tmp_output = enhance_with_openai(
            input_path=tmp_input,
            prompt=prompt,
            preserve_footer=preserve_footer,
            footer_ratio=footer_ratio,
        )

        persistent_enhanced = copy_to_persistent(tmp_output, ENHANCED_DIR, prefix=f"{app_id}_enhanced")
        print(f"[ENHANCE] app_id={app_id} job_id={job_id} enhanced={persistent_enhanced}", flush=True)

        with open(tmp_output, "rb") as f:
            data = f.read()

        response = send_file(
            io.BytesIO(data),
            mimetype="image/png",
            as_attachment=False,
            download_name="enhanced.png"
        )
        response.headers["X-App-Id"] = app_id
        response.headers["X-Job-Id"] = job_id
        response.headers["X-Enhanced-Path"] = persistent_enhanced
        return response

    except Exception as e:
        print("[ENHANCE ERROR]", str(e), flush=True)
        return jsonify(error=str(e)), 500

    finally:
        for p in [tmp_input, tmp_output]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

@app.route("/send-image", methods=["POST"])
def send_image():
    if "file" not in request.files or "to" not in request.form:
        return jsonify(error="Missing file or to"), 400

    phone_number = request.form["to"].strip()
    user_name = (request.form.get("name") or "").strip() or "User"

    enhance_before_send = bool_from_form(request.form.get("enhance"), default=False)
    prompt = (request.form.get("prompt") or "").strip() or DEFAULT_ENHANCE_PROMPT
    preserve_footer = bool_from_form(request.form.get("preserve_footer"), default=False)
    footer_ratio = float_from_form(request.form.get("footer_ratio"), default=0.22)
    app_id = (request.form.get("app_id") or "unknown_app").strip()
    job_id = (request.form.get("job_id") or "").strip()

    uploaded_file = request.files["file"]

    tmp_input = None
    tmp_output = None

    try:
        tmp_input = save_filestorage_temp(uploaded_file, folder=UPLOAD_FOLDER)
        send_path = tmp_input

        if enhance_before_send:
            print(f"[SEND] enhancing before send app_id={app_id} job_id={job_id}", flush=True)
            tmp_output = enhance_with_openai(
                input_path=tmp_input,
                prompt=prompt,
                preserve_footer=preserve_footer,
                footer_ratio=footer_ratio,
            )
            send_path = tmp_output
            copy_to_persistent(tmp_output, ENHANCED_DIR, prefix=f"{app_id}_send_enhanced")
        else:
            print(f"[SEND] sending existing file without enhancement app_id={app_id} job_id={job_id}", flush=True)

        media_id = upload_media(send_path)
        result = send_template_with_media_id(phone_number, media_id, user_name)

        try:
            record_send(phone_number, user_name)
        except Exception as db_err:
            print("[DB LOG ERROR]", db_err, flush=True)

        return jsonify({
            "ok": True,
            "enhanced_before_send": enhance_before_send,
            "result": result
        })

    except Exception as e:
        print("[SEND ERROR]", str(e), flush=True)
        return jsonify(error=str(e)), 500

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
    print("[WEBHOOK]", payload, flush=True)
    return "OK", 200

@app.route("/stats/today", methods=["GET"])
@requires_auth
def stats_today():
    db = get_db()
    day = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT ts, phone, name FROM sent_images WHERE day = ? ORDER BY ts DESC",
        (day,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
