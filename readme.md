# Perfume Photo Extractor (Flask + Claude Vision)

Step 1 of the full flow: upload photo + shop name → Claude Vision extracts items → returns JSON list.

## Files
- `app.py` — Flask app with `/extract` API and `/` test page
- `templates/index.html` — simple upload UI (photo + shop name)
- `requirements.txt` — dependencies
- `Procfile` — tells Render how to run it

## Local setup
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```
Visit `http://localhost:5000`

## Deploy to Render
1. Push this folder to a GitHub repo (or a new folder in an existing repo).
2. On Render: New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app` (already in Procfile, Render usually auto-detects it)
5. Add environment variable: `ANTHROPIC_API_KEY` = your key
6. Deploy. Render gives you a URL like `https://perfume-extractor.onrender.com`

## API usage (for calling from other systems later)

**POST** `/extract`
Content-Type: `multipart/form-data`

| field       | type | required |
|-------------|------|----------|
| photo       | file | yes      |
| shop_name   | text | yes      |

Example with curl:
```bash
curl -X POST https://your-app.onrender.com/extract \
  -F "photo=@shelf.jpg" \
  -F "shop_name=Al Haramain Dubai Mall"
```

Response:
```json
{
  "shop_name": "Al Haramain Dubai Mall",
  "item_count": 2,
  "items": [
    {
      "brand": "Dior",
      "name": "Sauvage",
      "size": "100ml",
      "concentration": "EDP",
      "gender": "Men",
      "condition": "Sealed",
      "estimated_price": null,
      "confidence": "high"
    }
  ]
}
```

## Notes / things to tune as you test
- The prompt in `app.py` (`EXTRACTION_PROMPT`) controls exactly what gets extracted — edit it directly if Claude misses fields or picks up things you don't want (e.g. background clutter).
- `MODEL = "claude-sonnet-5"` — if cost matters more than accuracy at high volume, you can test `claude-haiku-4-5-20251001`, but Sonnet will read tiny label text more reliably on cluttered shelf photos.
- Max upload size isn't capped yet — for shop photos this is usually fine, but add `MAX_CONTENT_LENGTH` to `app.py` if you want a hard limit.
- Next steps (from your flow diagram) — "user corrects errors" + "LM learns the correction" + "select items to record" — are separate build phases once this extraction step is solid.