import os
import base64
import json
import re
import io
from functools import wraps

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from werkzeug.security import check_password_hash
from PIL import Image
import anthropic
import psycopg2

import db
import airtable_client
import photo_storage

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key-change-in-render")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

try:
    db.init_db()
except Exception as e:
    print(f"[startup] Skipping DB init: {e}")


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def api_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "not_logged_in", "message": "Please log in again."}), 401
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")

        user = None
        try:
            user = db.get_user_by_username(username)
        except Exception as e:
            error = f"Could not check login right now: {e}"

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        elif not error:
            error = "Incorrect username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


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
    try:
        examples_text = db.build_correction_examples_text(shop_name)
    except Exception as e:
        print(f"[extract] Could not load past corrections: {e}")
        examples_text = ""
    return EXTRACTION_PROMPT_BASE + examples_text


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


MAX_DIMENSION = 1568
MAX_BASE64_BYTES = 10 * 1024 * 1024


def compress_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

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
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


@app.route("/api/field-options")
@api_login_required
def api_field_options():
    """Powers the autocomplete dropdowns for Concentration/Gender/Condition on the
    extraction results table — every value ever used for each field."""
    try:
        return jsonify({
            "concentration": db.get_distinct_field_values("concentration"),
            "gender": db.get_distinct_field_values("gender"),
            "condition": db.get_distinct_field_values("condition"),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/shop-names")
@api_login_required
def api_shop_names():
    """Powers the autocomplete dropdown on the upload page's Shop name field —
    every shop name ever used, most recently used first."""
    try:
        names = db.get_distinct_shop_names()
        return jsonify({"shop_names": names}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/extract", methods=["POST"])
@api_login_required
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

        try:
            db.save_recorded_items(shop_name, items)
        except Exception as e:
            print(f"[extract] Warning: could not save raw items to recorded_items: {e}")

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
    parts = [item.get("brand"), item.get("name"), item.get("size"), item.get("concentration"), item.get("gender")]
    parts = [p.strip() for p in parts if p and str(p).strip()]

    if not parts:
        return None

    suffix = "Tester" if (item.get("condition") or "").strip().lower() == "tester" else "Perfume"
    return " ".join(parts + [suffix])


@app.route("/record", methods=["POST"])
@api_login_required
def record():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected JSON body."}), 400

    shop_name = (data.get("shop_name") or "").strip()
    image_url = data.get("image_url")
    items = data.get("items", [])

    if not shop_name:
        return jsonify({"error": "shop_name is required."}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"error": "items must be a non-empty list."}), 400

    try:
        corrections_logged = db.log_corrections(shop_name, items)

        selected_items = [item["corrected"] for item in items if item.get("selected")]
        print(f"[record] {len(selected_items)} of {len(items)} item(s) were selected to save.")

        for item in selected_items:
            match = airtable_client.find_match(item.get("brand"), item.get("name"), item.get("size"), item.get("condition"))
            item["sku"] = match["sku"]
            item["product_name"] = match["product_name"] or build_fallback_product_name(item)

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
@login_required
def admin_data():
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
    )


@app.route("/admin/create-in-airtable", methods=["POST"])
@api_login_required
def admin_create_in_airtable():
    data = request.get_json(silent=True) or {}

    try:
        row_id = int(data["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Missing or invalid 'id'"}), 400

    brand = (data.get("brand") or "").strip()
    name = (data.get("name") or "").strip()
    size = (data.get("size") or "").strip()
    concentration = (data.get("concentration") or "").strip()
    gender = (data.get("gender") or "").strip()

    if not brand or not name:
        return jsonify({"error": "Brand and Name are required to create an Airtable record."}), 400

    try:
        result = airtable_client.create_record(brand, name, size, concentration, gender)

        if result["status"] == "no_brand":
            return jsonify({"error": "no_brand", "message": f"No brand found in Brands table matching '{brand}'."}), 404

        if result["status"] == "no_prefix":
            return jsonify({"error": "no_prefix", "message": f"Brand '{brand}' has no SKU Prefix set in the Brands table."}), 404

        if result["status"] != "ok":
            return jsonify({"error": "Failed to create the Airtable record. Check the server logs for details."}), 502

        sku = result["sku"]
        db.update_skus([{"id": row_id, "sku": sku}])
        return jsonify({"sku": sku}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/admin/update-sku", methods=["POST"])
@api_login_required
def admin_update_sku():
    data = request.get_json(silent=True) or {}
    updates = data.get("updates", [])

    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "updates must be a non-empty list"}), 400

    try:
        for u in updates:
            int(u["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Each update needs an integer 'id'"}), 400

    try:
        updated = db.update_skus(updates)
        return jsonify({"updated": updated}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/admin/delete", methods=["POST"])
@api_login_required
def admin_delete():
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
@api_login_required
def admin_clear_all():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "DELETE ALL":
        return jsonify({"error": "Confirmation phrase did not match. Nothing was deleted."}), 400

    try:
        db.clear_all()
        return jsonify({"status": "cleared"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/search")
@login_required
def search_page():
    return render_template("search.html")


@app.route("/api/search")
@api_login_required
def api_search():
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