import os
import base64
import json
import re

from flask import Flask, request, jsonify, render_template
import anthropic

app = Flask(__name__)

# Anthropic client — reads ANTHROPIC_API_KEY from environment (set this in Render)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

EXTRACTION_PROMPT = """You are looking at a photo of perfumes/fragrances taken inside a shop.

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


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_media_type(filename):
    ext = filename.rsplit(".", 1)[1].lower()
    if ext == "jpg":
        ext = "jpeg"
    return f"image/{ext}"


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
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        media_type = get_media_type(photo.filename)

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
                            "text": EXTRACTION_PROMPT,
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


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)