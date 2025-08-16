import os
import io
import mimetypes
from datetime import datetime

import cv2
import numpy as np
import requests
from flask import Flask, request, jsonify, send_file

# =======================
# Environment variables
# =======================
VERIFY_TOKEN     = os.environ["VERIFY_TOKEN"]
WHATSAPP_TOKEN   = os.environ["WHATSAPP_TOKEN"]
PHONE_NUMBER_ID  = os.environ["PHONE_NUMBER_ID"]
GRAPH_VERSION    = os.getenv("GRAPH_VERSION", "v21.0")
TEMPLATE_NAME    = os.getenv("TEMPLATE_NAME", "send_photo")  # approved template name
MODEL_DIR        = os.getenv("MODEL_DIR", "/opt/render/project/src/models")
MODEL_FILENAME   = os.getenv("MODEL_FILENAME", "GFPGANv1.4.pth")
MODEL_PATH       = os.path.join(MODEL_DIR, MODEL_FILENAME)

UPLOAD_FOLDER = "/tmp/whatsapp_images"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)

# =======================
# GFPGAN model (load once)
# =======================
# Lazy import to keep cold-start logs clean
from gfpgan import GFPGANer

gfpgan_restorer = None
gfpgan_error = None
try:
    if not os.path.isfile(MODEL_PATH):
        raise FileNotFoundError(f"Model not found at: {MODEL_PATH}")
    gfpgan_restorer = GFPGANer(
        model_path=MODEL_PATH,
        upscale=1,
        arch='clean',
        channel_multiplier=2,
        bg_upsampler=None
    )
    app.logger.info(f"GFPGAN model loaded from: {MODEL_PATH}")
except Exception as e:
    gfpgan_error = str(e)
    app.logger.error(f"Failed to load GFPGAN: {gfpgan_error}")

# =======================
# WhatsApp helpers
# =======================
def upload_media(file_path: str) -> str:
    """Uploads media to WhatsApp and returns media_id."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {
        'file': (os.path.basename(file_path), open(file_path, 'rb'), mime_type)
    }
    data = {"messaging_product": "whatsapp"}
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    app.logger.info(f"Upload response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()["id"]

def send_template_with_media_id(to_number: str, media_id: str, name_param: str) -> dict:
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
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    app.logger.info(f"Send response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()

# =======================
# Health & webhook
# =======================
@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        model_path=MODEL_PATH,
        gfpgan_loaded=(gfpgan_restorer is not None),
        gfpgan_error=gfpgan_error
    ), 200

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# =========================================
# NEW: /enhance  -> robot posts an image, we enhance, return JPEG
# =========================================
@app.route("/enhance", methods=["POST"])
def enhance_image():
    """
    Robot posts a multipart form with:
      - image: the image file to enhance
    Returns: image/jpeg (enhanced)
    """
    if gfpgan_restorer is None:
        return jsonify(error=f"GFPGAN not loaded: {gfpgan_error}"), 500

    if "image" not in request.files:
        return jsonify(error="Missing 'image'"), 400

    file_storage = request.files["image"]
    data = file_storage.read()
    if not data:
        return jsonify(error="Empty file"), 400

    npimg = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify(error="Failed to decode image"), 400

    try:
        # Enhance
        _, _, output = gfpgan_restorer.enhance(
            img, has_aligned=False, only_center_face=False, paste_back=True
        )

        # Optional: save a copy to tmp for debugging (disabled by default)
        # ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # debug_path = os.path.join(UPLOAD_FOLDER, f"enh_{ts}.jpg")
        # cv2.imwrite(debug_path, output)

        ok, buffer = cv2.imencode('.jpg', output, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            return jsonify(error="Failed to encode JPEG"), 500

        return send_file(
            io.BytesIO(buffer.tobytes()),
            mimetype="image/jpeg",
            as_attachment=False,
            download_name="enhanced.jpg"
        )
    except Exception as e:
        app.logger.exception("Enhancement failed")
        return jsonify(error=str(e)), 500

# ====================================================
# Existing: /send-image -> upload then send via template
# ====================================================
@app.route("/send-image", methods=["POST"])
def send_image():
    """
    Upload image and send it instantly via template.
    Required:
      - file: image file
      - to: recipient WhatsApp number (international format, no '+')
    Optional:
      - name: placeholder for template (defaults to "User")
    """
    if "file" not in request.files or "to" not in request.form:
        return jsonify(error="Missing file or 'to'"), 400

    phone_number = request.form["to"].strip()
    user_name = (request.form.get("name") or "User").strip()

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="Empty filename"), 400

    save_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(save_path)

    try:
        media_id = upload_media(save_path)
        result = send_template_with_media_id(phone_number, media_id, user_name or "User")
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        try:
            os.remove(save_path)
        except Exception:
            pass

# =======================
# Main
# =======================
if __name__ == "__main__":
    # For local testing only. On Render you’ll use Gunicorn.
    app.run(host="0.0.0.0", port=8000)
