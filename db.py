import os
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

FIELDS = ["brand", "name", "size", "concentration", "gender", "condition"]

# Individual connection settings for cPanel-hosted Postgres. Set these in Render's
# environment variables:
#   DB_HOST      — your cPanel server's IP address (or hostname)
#   DB_PORT      — usually 5432, confirm in cPanel > PostgreSQL Databases
#   DB_NAME      — the database name you created (often prefixed, e.g. cpaneluser_perfumedb)
#   DB_USER      — the database user (often prefixed too, e.g. cpaneluser_dbuser)
#   DB_PASSWORD  — that user's password
#   DB_SSLMODE   — optional override; defaults to "prefer" (tries SSL, falls back if
#                  the server doesn't support it) so this works whether or not your
#                  host requires SSL, without needing to know in advance.
REQUIRED_DB_VARS = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


def get_conn():
    """Get a Postgres connection using individual host/port/dbname/user/password
    env vars (for cPanel-hosted Postgres), rather than a single DATABASE_URL."""
    missing = [v for v in REQUIRED_DB_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing database env var(s): {', '.join(missing)}. "
            f"Set DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD in Render's environment variables."
        )

    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
        # Kept short on purpose: this connection sits on the critical path of every
        # /extract call (build_extraction_prompt looks up past corrections before
        # calling Claude). A long timeout here, combined with Claude's own response
        # time, can exceed gunicorn's worker timeout and crash the whole request.
        # Failing fast means the person still gets their extraction results even if
        # the database is temporarily unreachable.
        connect_timeout=3,
    )


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
            # fixed, and chose to save. Includes the matched Airtable SKU (if found),
            # a full display name (from Airtable when matched, or built ourselves),
            # and a clickable link back to the original shelf photo.
            # Uses "updated_at" (not "created_at") since this table may later support
            # editing existing rows — updated_at is the date that will actually matter then.
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
                    product_name TEXT,
                    image_url TEXT,
                    updated_at TIMESTAMPTZ NOT NULL
                );
            """)

            # Migration: add product_name to a table that was created before this column existed.
            cur.execute("""
                ALTER TABLE master_table ADD COLUMN IF NOT EXISTS product_name TEXT;
            """)

            # Migration: earlier deploys created this table with "created_at" instead.
            # Rename it in place so existing data (and its dates) aren't lost.
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'master_table' AND column_name = 'created_at';
            """)
            if cur.fetchone():
                print("[db] Migrating master_table: renaming created_at -> updated_at")
                cur.execute("ALTER TABLE master_table RENAME COLUMN created_at TO updated_at;")
        conn.commit()
        print("[db] Tables ready.")
    finally:
        conn.close()


ALLOWED_TABLES = {"master_table", "recorded_items", "corrections"}


# TODO-REMOVE-BEFORE-LIVE: delete_items() + clear_all() below back the "Delete selected"
# and "Clear All Data" buttons on /admin/data. These are dev/testing-only tools for
# wiping bad test data while building this app. Remove both functions (and their
# routes in app.py, and their buttons/JS in templates/admin_data.html) before this
# app is used with real, permanent shop data.
def delete_items(table, ids):
    """Delete specific rows by id from one of the three known tables.
    `table` is checked against an allowlist — never build this from raw user input
    without that check, since table names can't be parameterized like values can."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table!r}. Must be one of {ALLOWED_TABLES}.")
    if not ids:
        return 0

    print(f"[db] Deleting {len(ids)} row(s) from {table}: ids={ids}")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # table name is validated against ALLOWED_TABLES above, so this is safe
            cur.execute(f"DELETE FROM {table} WHERE id = ANY(%s);", (ids,))
            deleted = cur.rowcount
        conn.commit()
        print(f"[db] Deleted {deleted} row(s) from {table}.")
        return deleted
    finally:
        conn.close()


def update_skus(updates):
    """Manually update the SKU on one or more master_table rows.
    updates: list of {"id": int, "sku": str} dicts. Also bumps updated_at,
    since a manual SKU fix is a real edit to the row."""
    if not updates:
        return 0

    print(f"[db] Updating SKU on {len(updates)} master_table row(s)...")
    conn = get_conn()
    now = datetime.now(timezone.utc)
    updated = 0
    try:
        with conn.cursor() as cur:
            for u in updates:
                row_id = int(u["id"])
                sku = (u.get("sku") or "").strip() or None
                cur.execute(
                    "UPDATE master_table SET sku = %s, updated_at = %s WHERE id = %s;",
                    (sku, now, row_id),
                )
                if cur.rowcount:
                    updated += cur.rowcount
                    print(f"[db]   -> row {row_id}: sku set to {sku!r}")
        conn.commit()
        print(f"[db] Updated {updated} row(s).")
    finally:
        conn.close()
    return updated


def clear_all():
    # TODO-REMOVE-BEFORE-LIVE: backs the "Clear All Data" danger-zone button.
    # Wipes every row in every table with no way to undo it. Remove this function
    # (and its route in app.py, and the danger-zone UI in admin_data.html) before go-live.
    """Wipes every row from all three tables and resets id counters back to 1.
    Irreversible — the app layer must confirm intent before calling this."""
    print("[db] CLEARING ALL DATA from master_table, recorded_items, corrections...")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE master_table, recorded_items, corrections RESTART IDENTITY;")
        conn.commit()
        print("[db] All data cleared.")
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

    items: list of corrected item dicts, each already carrying 'sku' and 'product_name'
           keys (set by looking up/building from Airtable — 'sku' may be None if no
           match was found, but 'product_name' should always have a value, real or
           fallback-built).
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
                product_name = item.get("product_name")
                print(f"[db]   -> {item.get('brand')} {item.get('name')} | SKU={sku!r} | "
                      f"product_name={product_name!r} | image_url={image_url!r}")
                cur.execute("""
                    INSERT INTO master_table
                        (shop_name, brand, name, size, concentration, gender, condition, sku, product_name, image_url, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    shop_name,
                    item.get("brand"),
                    item.get("name"),
                    item.get("size"),
                    item.get("concentration"),
                    item.get("gender"),
                    item.get("condition"),
                    sku,
                    product_name,
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
    the Brand, Name, Product Name, or SKU (partial match, case-insensitive) —
    e.g. "initio" matches brand "Initio", "sauvage" matches name "Dior Sauvage",
    and "gra100" matches SKU "GRA1003".
    """
    print(f"[db] Searching master_table for: {query!r}")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            like_pattern = f"%{query}%"
            cur.execute("""
                SELECT id, shop_name, brand, name, size, concentration, gender, condition, sku, product_name, image_url, updated_at
                FROM master_table
                WHERE brand ILIKE %s OR name ILIKE %s OR sku ILIKE %s OR product_name ILIKE %s
                ORDER BY updated_at DESC
                LIMIT %s;
            """, (like_pattern, like_pattern, like_pattern, like_pattern, limit))
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
                SELECT id, shop_name, brand, name, size, concentration, gender, condition, sku, product_name, image_url, updated_at
                FROM master_table
                ORDER BY updated_at DESC
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