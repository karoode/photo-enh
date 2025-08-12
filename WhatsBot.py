import os
import requests
import mimetypes
from flask import Flask, request, jsonify, send_file
from io import BytesIO
import cv2
import numpy as np
from basicsr.archs.rrdbnet_arch import RRDBNet
from gfpgan import GFPGANer

# ===== Environment Variables =====
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0")
TEMPLATE_NAME = os.getenv("TEMPLATE_NAME", "send_photo")

# ===== Model Path =====
# Stored on Render persistent disk
MODEL_PATH = os.getenv("MODEL_PATH", "/opt/render/project/src/models/GFPGANv1.4.pth")

UPLOAD_FOLDER = "/tmp/whatsapp_images"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ===== Flask App =====
app = Flask(__name__)

# ===== Initialize GFPGAN at Startup =====
print(f"Loading GFPGAN model from {MODEL_PATH}...")
gfpganer = GFPGANer(
    model_path=MODEL_PATH,
    upscale=1,
    arch='clean',
    channel_multiplier=2,
    bg_upsampler=None
)
print("GFPGAN model loaded successfully.")

# ===== WhatsApp API Helpers =====
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

# ===== Image Enhancement =====
def enhance_image_bytes(image_bytes):
    """Enhance image using GFPGAN and return enhanced image bytes."""
    img_array = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Invalid image file.")

    _, _, restored_img = gfpganer.enhance(
        img, has_aligned=False, only_center_face=False, paste_back=True
    )
    _, buffer = cv2.imencode('.jpg', restored_img)
    return buffer

# ===== API: Enhance Only =====
@app.route("/enhance", methods=["POST"])
def enhance_endpoint():
    if "image" not in request.files:
        return jsonify(error="Missing 'image' file"), 400
    file = request.files["image"]
    try:
        enhanced_bytes = enhance_image_bytes(file.read())
        return send_file(BytesIO(enhanced_bytes), mimetype="image/jpeg")
    except Exception as e:
        return jsonify(error=str(e)), 500

# ===== API: Enhance + Send to WhatsApp =====
@app.route("/send-image", methods=["POST"])
def send_image():
    """
    Enhances image and sends it instantly via WhatsApp template.
    Required form fields:
      file: image file
      to: recipient phone number (international format, no +)
      name: name placeholder for template
    """
    if "file" not in request.files or "to" not in request.form or "name" not in request.form:
        return jsonify(error="Missing file, to, or name"), 400

    phone_number = request.form["to"].strip()
    user_name = request.form["name"].strip()
    file = request.files["file"]

    try:
        # Enhance image
        enhanced_bytes = enhance_image_bytes(file.read())

        # Save enhanced image temporarily
        save_path = os.path.join(UPLOAD_FOLDER, f"enhanced_{phone_number}.jpg")
        with open(save_path, "wb") as f:
            f.write(enhanced_bytes)

        # Upload and send via WhatsApp
        media_id = upload_media(save_path)
        result = send_template_with_media_id(phone_number, media_id, user_name)

        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if 'save_path' in locals() and os.path.exists(save_path):
            os.remove(save_path)

# ===== Webhook Verification =====
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ===== Main =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
