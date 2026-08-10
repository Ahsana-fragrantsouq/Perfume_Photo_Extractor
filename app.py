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
import airtable_client
import photo_storage

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

    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    quality = 85
    while quality >= 40:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) * 4 / 3 < MAX_BASE64_BYTES:
            return data, "image/jpeg"
        quality -= 15

    return data, "image/jpeg"


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
        print(f"[extract] Claude found {len(items)} item(s) for shop '{shop_name}'.")

        # Save the RAW extraction to recorded_items right away — this happens
        # BEFORE any user correction, so it's a log of exactly what the AI saw.
        try:
            db.save_recorded_items(shop_name, items)
        except Exception as e:
            # Don't fail the whole request just because logging the raw data failed —
            # the user should still get their results to review.
            print(f"[extract] Warning: could not save raw items to recorded_items: {e}")

        # Upload the shelf photo so we have a permanent URL to attach to the final
        # saved items later (in /record). Shared by every item from this same photo.
        image_url = photo_storage.upload_image(compressed_bytes)

        return jsonify({
            "shop_name": shop_name,
            "item_count": len(items),
            "items": items,
            "image_url": image_url,
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


def build_fallback_product_name(item):
    """
    Builds a name in the same style as Airtable's own auto-generated "Product Name"
    field (e.g. "Anfasic Dokhoon Arqa Shay 75 ml EDP Unisex Perfume"), for items that
    didn't match anything in French Inventories. Used so the column is never blank —
    real Airtable text is always preferred when a match is found (see find_match()).
    """
    parts = [item.get("brand"), item.get("name"), item.get("size"), item.get("concentration"), item.get("gender")]
    parts = [p.strip() for p in parts if p and str(p).strip()]

    if not parts:
        return None

    suffix = "Tester" if (item.get("condition") or "").strip().lower() == "tester" else "Perfume"
    return " ".join(parts + [suffix])


@app.route("/record", methods=["POST"])
def record():
    """Receives the user's reviewed/corrected items and saves the FINAL version.
    Expects JSON body:
    {
      "shop_name": "...",
      "image_url": "...",           // from /extract's response — same photo for all items here
      "items": [
        {
          "original": {brand, name, size, ...},
          "corrected": {brand, name, size, ...},
          "selected": true
        },
        ...
      ]
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected JSON body."}), 400

    shop_name = (data.get("shop_name") or "").strip()
    image_url = data.get("image_url")  # may be None if Cloudinary wasn't configured — that's fine
    items = data.get("items", [])

    if not shop_name:
        return jsonify({"error": "shop_name is required."}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"error": "items must be a non-empty list."}), 400

    try:
        # Step 1: log every field the user changed, same as before — unrelated to selection
        corrections_logged = db.log_corrections(shop_name, items)

        # Step 2: only the items the user actually checked get saved to master_table
        selected_items = [item["corrected"] for item in items if item.get("selected")]
        print(f"[record] {len(selected_items)} of {len(items)} item(s) were selected to save.")

        # Step 3: for each selected item, search Airtable French Inventories for a
        # matching SKU + their own "Product Name" text (exact match on brand + name + size).
        # If nothing matches, we build a similarly-formatted name ourselves so the
        # column is never blank.
        for item in selected_items:
            match = airtable_client.find_match(item.get("brand"), item.get("name"), item.get("size"), item.get("condition"))
            item["sku"] = match["sku"]
            item["product_name"] = match["product_name"] or build_fallback_product_name(item)

        # Step 4: save the final corrected items (with their SKU + shared photo URL)
        saved_count = db.save_master_items(shop_name, selected_items, image_url) if selected_items else 0

        return jsonify({
            "shop_name": shop_name,
            "items_saved": saved_count,
            "corrections_logged": corrections_logged,
        }), 200

    except psycopg2.Error as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 502

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/data")
def admin_data():
    """Browser-viewable page to check what's been saved, and delete rows if needed.
    Protected by a key: /admin/data?key=YOUR_KEY
    Set ADMIN_KEY in Render's environment variables."""
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key:
        return "ADMIN_KEY is not set in the environment. Add one in Render to use this page.", 503
    if request.args.get("key") != admin_key:
        return "Forbidden: missing or incorrect ?key=", 403

    try:
        master_items = db.get_master_items(limit=500)
        recorded_items = db.get_recorded_items(limit=500)
        corrections = db.get_all_corrections(limit=500)
    except Exception as e:
        return f"Database error: {str(e)}", 502

    return render_template(
        "admin_data.html",
        master_items=master_items,
        recorded_items=recorded_items,
        corrections=corrections,
        admin_key=admin_key,
    )


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    """Deletes specific rows by id from one table. Called by the checkboxes on /admin/data.
    POST /admin/delete?key=YOUR_KEY   body: {"table": "master_table", "ids": [1, 2, 3]}"""
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key or request.args.get("key") != admin_key:
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    table = data.get("table")
    raw_ids = data.get("ids", [])

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    try:
        deleted = db.delete_items(table, ids)
        return jsonify({"deleted": deleted}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/admin/clear-all", methods=["POST"])
def admin_clear_all():
    """Wipes every table completely. Requires the exact confirmation phrase in the
    request body as a safeguard against accidental calls.
    POST /admin/clear-all?key=YOUR_KEY   body: {"confirm": "DELETE ALL"}"""
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key or request.args.get("key") != admin_key:
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "DELETE ALL":
        return jsonify({"error": "Confirmation phrase did not match. Nothing was deleted."}), 400

    try:
        db.clear_all()
        return jsonify({"status": "cleared"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/search")
def search_page():
    """Simple search page — a text box for name/SKU, results load via /api/search."""
    return render_template("search.html")


@app.route("/api/search")
def api_search():
    """JSON API used by the search page (and callable by other tools later).
    GET /api/search?q=sauvage"""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "Missing search query. Use ?q=..."}), 400

    try:
        results = db.search_master_items(query)
        return jsonify({"query": query, "count": len(results), "results": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)