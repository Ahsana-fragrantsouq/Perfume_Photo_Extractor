import os
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

FIELDS = ["brand", "name", "size", "concentration", "gender", "condition"]


def get_conn():
    """Get a Postgres connection using Render's DATABASE_URL env var.
    Render sometimes provides 'postgres://' which psycopg2 needs as 'postgresql://'."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set. Add a Postgres database in Render and link it to this service.")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def init_db():
    """Create tables if they don't exist yet. Safe to call on every app startup."""
    print("[db] Checking/creating database tables...")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Raw log of every correction a user makes (one row per field changed)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS corrections (
                    id SERIAL PRIMARY KEY,
                    shop_name TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    original_value TEXT,
                    corrected_value TEXT,
                    item_context TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_corrections_shop_name
                ON corrections (shop_name);
            """)

            # Raw dump of exactly what Claude extracted, saved immediately at /extract time,
            # BEFORE any user correction. This is the "as the AI saw it" record.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recorded_items (
                    id SERIAL PRIMARY KEY,
                    shop_name TEXT NOT NULL,
                    brand TEXT,
                    name TEXT,
                    size TEXT,
                    concentration TEXT,
                    gender TEXT,
                    condition TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)

            # The final, corrected catalog — one row per item the user reviewed,
            # fixed, and chose to save. Includes the matched Airtable SKU (if found)
            # and a clickable link back to the original shelf photo.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS master_table (
                    id SERIAL PRIMARY KEY,
                    shop_name TEXT NOT NULL,
                    brand TEXT,
                    name TEXT,
                    size TEXT,
                    concentration TEXT,
                    gender TEXT,
                    condition TEXT,
                    sku TEXT,
                    image_url TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()
        print("[db] Tables ready.")
    finally:
        conn.close()


def get_recent_corrections(shop_name, limit=15):
    """Fetch the most recent corrections for a shop, used to build few-shot examples for the prompt."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT field_name, original_value, corrected_value, item_context
                FROM corrections
                WHERE shop_name = %s AND original_value IS DISTINCT FROM corrected_value
                ORDER BY created_at DESC
                LIMIT %s;
            """, (shop_name, limit))
            return cur.fetchall()
    finally:
        conn.close()


def log_corrections(shop_name, items):
    """items: list of {original: {...}, corrected: {...}}.
    Logs one row per field that actually changed between what Claude extracted and what the user fixed."""
    conn = get_conn()
    now = datetime.now(timezone.utc)
    rows_written = 0
    try:
        with conn.cursor() as cur:
            for item in items:
                original = item.get("original", {}) or {}
                corrected = item.get("corrected", {}) or {}
                item_context = f"{corrected.get('brand') or original.get('brand') or ''} {corrected.get('name') or original.get('name') or ''}".strip()

                for field in FIELDS:
                    orig_val = original.get(field)
                    corr_val = corrected.get(field)
                    if orig_val != corr_val:
                        print(f"[db] Correction logged for '{item_context}': {field} changed '{orig_val}' -> '{corr_val}'")
                        cur.execute("""
                            INSERT INTO corrections
                                (shop_name, field_name, original_value, corrected_value, item_context, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """, (shop_name, field, orig_val, corr_val, item_context, now))
                        rows_written += 1
        conn.commit()
    finally:
        conn.close()
    print(f"[db] Total corrections logged: {rows_written}")
    return rows_written


def save_recorded_items(shop_name, items):
    """Saves the RAW items exactly as Claude extracted them — called from /extract,
    BEFORE the user has corrected anything. items: list of item dicts from Claude."""
    print(f"[db] Saving {len(items)} raw extracted item(s) to recorded_items for shop '{shop_name}'...")
    conn = get_conn()
    now = datetime.now(timezone.utc)
    saved = 0
    try:
        with conn.cursor() as cur:
            for item in items:
                cur.execute("""
                    INSERT INTO recorded_items
                        (shop_name, brand, name, size, concentration, gender, condition, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    shop_name,
                    item.get("brand"),
                    item.get("name"),
                    item.get("size"),
                    item.get("concentration"),
                    item.get("gender"),
                    item.get("condition"),
                    now,
                ))
                saved += 1
        conn.commit()
        print(f"[db] Saved {saved} raw item(s) to recorded_items.")
    finally:
        conn.close()
    return saved


def save_master_items(shop_name, items, image_url):
    """
    Saves the FINAL, corrected items to master_table — called from /record, after the
    user has reviewed/fixed everything and selected which items to keep.

    items: list of corrected item dicts, each already carrying a 'sku' key
           (set by looking it up in Airtable — may be None if no match was found).
    image_url: the Cloudinary URL of the original shelf photo, shared by every item
               from this same extraction (may be None if photo upload wasn't configured).
    """
    print(f"[db] Saving {len(items)} item(s) to master_table for shop '{shop_name}'...")
    conn = get_conn()
    now = datetime.now(timezone.utc)
    saved = 0
    try:
        with conn.cursor() as cur:
            for item in items:
                sku = item.get("sku")
                print(f"[db]   -> {item.get('brand')} {item.get('name')} | SKU={sku!r} | image_url={image_url!r}")
                cur.execute("""
                    INSERT INTO master_table
                        (shop_name, brand, name, size, concentration, gender, condition, sku, image_url, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    shop_name,
                    item.get("brand"),
                    item.get("name"),
                    item.get("size"),
                    item.get("concentration"),
                    item.get("gender"),
                    item.get("condition"),
                    sku,
                    image_url,
                    now,
                ))
                saved += 1
        conn.commit()
        print(f"[db] Saved {saved} item(s) to master_table.")
    finally:
        conn.close()
    return saved


def search_master_items(query, limit=100):
    """
    Search master_table for items where the search text appears anywhere in
    the Brand, Name, or SKU (partial match, case-insensitive) — e.g. "initio"
    matches brand "Initio", "sauvage" matches name "Dior Sauvage", and "gra100"
    matches SKU "GRA1003".
    """
    print(f"[db] Searching master_table for: {query!r}")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            like_pattern = f"%{query}%"
            cur.execute("""
                SELECT id, shop_name, brand, name, size, concentration, gender, condition, sku, image_url, created_at
                FROM master_table
                WHERE brand ILIKE %s OR name ILIKE %s OR sku ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s;
            """, (like_pattern, like_pattern, like_pattern, limit))
            results = cur.fetchall()
            print(f"[db] Search found {len(results)} result(s).")
            return results
    finally:
        conn.close()


def get_master_items(limit=200):
    """Fetch recent master_table rows for the /admin/data viewer page."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, shop_name, brand, name, size, concentration, gender, condition, sku, image_url, created_at
                FROM master_table
                ORDER BY created_at DESC
                LIMIT %s;
            """, (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def get_recorded_items(limit=100):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, shop_name, brand, name, size, concentration, gender, condition, created_at
                FROM recorded_items
                ORDER BY created_at DESC
                LIMIT %s;
            """, (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def get_all_corrections(limit=200):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, shop_name, item_context, field_name, original_value, corrected_value, created_at
                FROM corrections
                ORDER BY created_at DESC
                LIMIT %s;
            """, (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def build_correction_examples_text(shop_name, limit=15):
    """Format recent corrections as a short block of examples to inject into the extraction prompt."""
    corrections = get_recent_corrections(shop_name, limit=limit)
    if not corrections:
        return ""

    lines = []
    for c in corrections:
        lines.append(
            f'- For item "{c["item_context"]}", field "{c["field_name"]}": '
            f'previously misread as "{c["original_value"]}", correct value is "{c["corrected_value"]}".'
        )

    return (
        "\n\nHere are corrections from past photos at this shop — use these as guidance for "
        "similar items, wording, or naming conventions this shop uses:\n" + "\n".join(lines)
    )