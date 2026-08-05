import os
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

FIELDS = ["brand", "name", "size", "concentration", "gender", "condition", "estimated_price"]


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
    conn = get_conn()
    try:
        with conn.cursor() as cur:
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
                    estimated_price TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()
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
                # short human-readable label for the item, so corrections are readable later
                item_context = f"{corrected.get('brand') or original.get('brand') or ''} {corrected.get('name') or original.get('name') or ''}".strip()

                for field in FIELDS:
                    orig_val = original.get(field)
                    corr_val = corrected.get(field)
                    if orig_val != corr_val:
                        cur.execute("""
                            INSERT INTO corrections
                                (shop_name, field_name, original_value, corrected_value, item_context, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """, (shop_name, field, orig_val, corr_val, item_context, now))
                        rows_written += 1
        conn.commit()
    finally:
        conn.close()
    return rows_written


def save_recorded_items(shop_name, items):
    """items: list of corrected item dicts to persist as final records."""
    conn = get_conn()
    now = datetime.now(timezone.utc)
    saved = 0
    try:
        with conn.cursor() as cur:
            for item in items:
                cur.execute("""
                    INSERT INTO recorded_items
                        (shop_name, brand, name, size, concentration, gender, condition, estimated_price, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    shop_name,
                    item.get("brand"),
                    item.get("name"),
                    item.get("size"),
                    item.get("concentration"),
                    item.get("gender"),
                    item.get("condition"),
                    item.get("estimated_price"),
                    now,
                ))
                saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved


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