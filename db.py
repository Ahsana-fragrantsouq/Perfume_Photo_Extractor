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
    return psycopg2.connect(
        db_url,
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

            cur.execute("""
                ALTER TABLE master_table ADD COLUMN IF NOT EXISTS product_name TEXT;
            """)

            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'master_table' AND column_name = 'created_at';
            """)
            if cur.fetchone():
                print("[db] Migrating master_table: renaming created_at -> updated_at")
                cur.execute("ALTER TABLE master_table RENAME COLUMN created_at TO updated_at;")

            # User accounts for logging into the whole app. Passwords are stored as
            # hashes (see werkzeug.security in app.py), never plain text.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()
        print("[db] Tables ready.")
    finally:
        conn.close()

    _seed_admin_user_if_needed()


def _seed_admin_user_if_needed():
    """If there are no users at all yet, create one from ADMIN_USERNAME/ADMIN_PASSWORD
    env vars — otherwise nobody could ever log in for the first time. Once at least
    one user exists, this does nothing (existing accounts are never touched)."""
    from werkzeug.security import generate_password_hash

    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users;")
            (count,) = cur.fetchone()
            if count > 0:
                return

            if not username or not password:
                print("[db] No users exist yet, and ADMIN_USERNAME/ADMIN_PASSWORD aren't set — "
                      "nobody will be able to log in until you set those env vars and redeploy.")
                return

            cur.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s);",
                (username, generate_password_hash(password), datetime.now(timezone.utc)),
            )
        conn.commit()
        print(f"[db] Seeded initial admin user {username!r} from ADMIN_USERNAME/ADMIN_PASSWORD.")
    finally:
        conn.close()


def get_user_by_username(username):
    """Looks up a user by username (case-sensitive). Returns {'id', 'username', 'password_hash'} or None."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s;", (username,))
            return cur.fetchone()
    finally:
        conn.close()


ALLOWED_TABLES = {"master_table", "recorded_items", "corrections"}


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
    the Brand, Name, Product Name, or SKU (partial match, case-insensitive).
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


def get_distinct_shop_names(limit=200):
    """
    All shop names ever used, across both recorded_items (every extraction attempt)
    and master_table (final saved items) — so the autocomplete list on the upload
    page covers a shop even if nothing from it was ever actually saved yet.
    Returns a plain list of strings, most recently used first.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shop_name, MAX(seen_at) AS last_seen FROM (
                    SELECT shop_name, created_at AS seen_at FROM recorded_items
                    UNION ALL
                    SELECT shop_name, updated_at AS seen_at FROM master_table
                ) AS combined
                GROUP BY shop_name
                ORDER BY last_seen DESC
                LIMIT %s;
            """, (limit,))
            return [row[0] for row in cur.fetchall()]
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