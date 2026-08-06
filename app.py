import os
import base64
import json
import re
import io

from flask import Flask, request, jsonify, render_template
from PIL import Image
import anthropic
import psycopg2

import db

app = Flask(__name__)

# Anthropic client — reads ANTHROPIC_API_KEY from environment (set this in Render)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

# Create tables on startup if they don't exist yet. If DATABASE_URL isn't set
# (e.g. running locally without Postgres), the app still works for /extract —
# only /record will fail until a database is connected.
try:
    db.init_db()
except Exception as e:
    print(f"[startup] Skipping DB init: {e}")

EXTRACTION_PROMPT_BASE = """You are looking at a photo of perfumes/fragrances taken inside a shop.

Identify every distinct perfume/fragrance bottle or box you can see in the image.
For each item, extract as much of the following as you can confidently determine from the image:

- brand (e.g. "Dior", "Chanel")
- name (e.g. "Sauvage", "Bleu de Chanel")
- size (e.g. "100ml", "50ml") — include unit
- concentration (e.g. "EDP", "EDT", "Parfum", "Cologne") — null if not visible/unclear
- gender ("Men", "Women", "Unisex") — null if unclear
- condition ("Sealed", "Tester", "Used", "Unknown") — best guess from packaging/visual cues
- estimated_price — only if a price tag/label is visibly readable in the photo, else null
- confidence — your confidence in this item's identification: "high", "medium", or "low"

Respond with ONLY valid JSON, no markdown code fences, no explanation text, in this exact structure:

{
  "items": [
    {
      "brand": "...",
      "name": "...",
      "size": "...",
      "concentration": "...",
      "gender": "...",
      "condition": "...",
      "estimated_price": "...",
      "confidence": "..."
    }
  ]
}

If you cannot identify any items at all, return {"items": []}.
Do not guess wildly — if a field is not determinable from the image, use null for that field rather than inventing a value.
"""


def build_extraction_prompt(shop_name):
    """Base prompt + any past corrections for this shop, so Claude improves over time per-shop."""
    try:
        examples_text = db.build_correction_examples_text(shop_name)
    except Exception as e:
        print(f"[extract] Could not load past corrections: {e}")
        examples_text = ""
    return EXTRACTION_PROMPT_BASE + examples_text


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


MAX_DIMENSION = 1568  # Claude's recommended max edge length; larger images just get downsampled anyway
MAX_BASE64_BYTES = 10 * 1024 * 1024  # Anthropic's hard limit for base64-encoded images


def compress_image(image_bytes):
    """Resize and compress an image so its base64 size stays under Claude's 10MB limit.
    Returns (jpeg_bytes, media_type)."""
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")  # normalize (handles PNG transparency, HEIC-via-Pillow, etc.)

    # Resize so the longest edge is at most MAX_DIMENSION
    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    quality = 85
    while quality >= 40:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        # base64 inflates size by ~33% — check against that, not the raw byte count
        if len(data) * 4 / 3 < MAX_BASE64_BYTES:
            return data, "image/jpeg"
        quality -= 15

    return data, "image/jpeg"  # best effort at lowest quality tried


def extract_json(text):
    """Claude sometimes wraps JSON in code fences despite instructions — strip them defensively."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def extract():
    if "photo" not in request.files:
        return jsonify({"error": "No photo file provided. Use form field name 'photo'."}), 400

    photo = request.files["photo"]
    shop_name = request.form.get("shop_name", "").strip()

    if photo.filename == "":
        return jsonify({"error": "No photo selected."}), 400

    if not shop_name:
        return jsonify({"error": "shop_name is required."}), 400

    if not allowed_file(photo.filename):
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    try:
        image_bytes = photo.read()
        compressed_bytes, media_type = compress_image(image_bytes)
        image_b64 = base64.standard_b64encode(compressed_bytes).decode("utf-8")

        message = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": build_extraction_prompt(shop_name),
                        },
                    ],
                }
            ],
        )

        raw_text = message.content[0].text

        if message.stop_reason == "max_tokens":
            return jsonify({
                "error": "Response was cut off because too many items were detected in one photo. "
                         "Try taking a closer photo covering fewer items at once.",
                "raw_response": raw_text,
            }), 502

        parsed = extract_json(raw_text)
        items = parsed.get("items", [])

        return jsonify({
            "shop_name": shop_name,
            "item_count": len(items),
            "items": items,
        }), 200

    except json.JSONDecodeError:
        return jsonify({
            "error": "Model did not return valid JSON.",
            "raw_response": raw_text if "raw_text" in locals() else None,
        }), 502

    except anthropic.APIError as e:
        return jsonify({"error": f"Anthropic API error: {str(e)}"}), 502

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/record", methods=["POST"])
def record():
    """Receives the user's reviewed/corrected items.
    Expects JSON body:
    {
      "shop_name": "...",
      "items": [
        {
          "original": {brand, name, size, ...},   // what Claude extracted
          "corrected": {brand, name, size, ...},   // what the user confirmed/fixed
          "selected": true                          // whether the user wants this one recorded
        },
        ...
      ]
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected JSON body."}), 400

    shop_name = (data.get("shop_name") or "").strip()
    items = data.get("items", [])

    if not shop_name:
        return jsonify({"error": "shop_name is required."}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"error": "items must be a non-empty list."}), 400

    try:
        # Log every field the user changed, regardless of whether the item was selected —
        # even discarding an item doesn't mean its corrections aren't useful to learn from.
        corrections_logged = db.log_corrections(shop_name, items)

        selected_items = [item["corrected"] for item in items if item.get("selected")]
        saved_count = db.save_recorded_items(shop_name, selected_items) if selected_items else 0

        return jsonify({
            "shop_name": shop_name,
            "items_saved": saved_count,
            "corrections_logged": corrections_logged,
        }), 200

    except psycopg2.Error as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 502

    except RuntimeError as e:
        # Raised by db.get_conn() when DATABASE_URL isn't configured
        return jsonify({"error": str(e)}), 503

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/data")
def admin_data():
    """Simple browser-viewable page to check what's been saved — no external tools needed.
    Protected by a key so random visitors can't see your data: /admin/data?key=YOUR_KEY
    Set ADMIN_KEY in Render's environment variables to whatever you want that key to be."""
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key:
        return "ADMIN_KEY is not set in the environment. Add one in Render to use this page.", 503
    if request.args.get("key") != admin_key:
        return "Forbidden: missing or incorrect ?key=", 403

    try:
        items = db.get_recorded_items(limit=200)
        corrections = db.get_all_corrections(limit=200)
    except Exception as e:
        return f"Database error: {str(e)}", 502

    def render_table(rows, columns):
        if not rows:
            return "<p>No rows yet.</p>"
        html = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in columns) + "</tr></thead><tbody>"
        for row in rows:
            html += "<tr>" + "".join(f"<td>{row.get(c, '') if row.get(c) is not None else ''}</td>" for c in columns) + "</tr>"
        html += "</tbody></table>"
        return html

    items_html = render_table(items, ["id", "shop_name", "brand", "name", "size", "concentration", "gender", "condition", "estimated_price", "created_at"])
    corrections_html = render_table(corrections, ["id", "shop_name", "item_context", "field_name", "original_value", "corrected_value", "created_at"])

    return f"""
    <html>
    <head>
    <title>Saved Data</title>
    <style>
      body {{ font-family: -apple-system, Arial, sans-serif; margin: 30px; color: #222; }}
      h2 {{ margin-top: 40px; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 10px; }}
      th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; }}
      th {{ background: #f5f5f5; }}
    </style>
    </head>
    <body>
      <h1>Saved Data</h1>
      <h2>Recorded Items ({len(items)})</h2>
      {items_html}
      <h2>Corrections Log ({len(corrections)})</h2>
      {corrections_html}
    </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)