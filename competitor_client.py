"""
Handles searching the "Competitor table" app's own Postgres database
(item_master + brand_sku tables) to find an existing product's SKU
(and full display name), or create a brand-new one.

Replaces airtable_client.py — same two functions (find_match, create_record),
same return shapes, so app.py only needs its import/calls swapped.
"""

import os
import psycopg2

# Connection string for the Competitor table app's OWN database — set this in
# Render's environment variables as COMPETITOR_DATABASE_URL (the External
# Database URL from that app's Postgres instance on Render).
DATABASE_URL = os.environ.get("COMPETITOR_DATABASE_URL")

NUMBER_PAD_LENGTH = 4
MAX_SKU_RETRIES = 5


def _get_conn():
    url = DATABASE_URL
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, connect_timeout=8)


def _normalize_size(size):
    """Strip spaces and lowercase, so '100 ml', '100ml', and '100 ML' all compare equal."""
    if not size:
        return ""
    return size.replace(" ", "").lower()


def _is_tester_row(sku, product_name):
    """No dedicated Condition column in item_master — but SKUs ending in 'T'
    (e.g. TMF1035T) are testers, and the word 'Tester' sometimes shows up in
    the product name too. Check both, same as the old Airtable logic did."""
    sku = (sku or "").strip().upper()
    name = (product_name or "").lower()
    return sku.endswith("T") or "tester" in name


def find_match(brand, name, size, condition=None):
    """
    Search item_master for a match on brand + perfume_name (case-insensitive,
    whitespace-trimmed), then compare size ourselves with spacing/case ignored.

    Returns {"sku": ..., "product_name": ...} — either may be None if no match
    was found.
    """
    no_match = {"sku": None, "product_name": None}

    if not DATABASE_URL:
        print("[competitor_db] COMPETITOR_DATABASE_URL not set — skipping lookup.")
        return no_match

    if not brand or not name or not size:
        print(f"[competitor_db] Missing brand/name/size (brand={brand!r}, name={name!r}, size={size!r}) — skipping lookup.")
        return no_match

    print(f"[competitor_db] Searching for: brand={brand!r} name={name!r} "
          f"(will match size={size!r}, condition={condition!r} separately)")

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT sku, size, product_name
            FROM item_master
            WHERE LOWER(TRIM(brand)) = LOWER(TRIM(%s))
              AND LOWER(TRIM(perfume_name)) = LOWER(TRIM(%s))
        """, (brand, name))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            print(f"[competitor_db] No match found for brand={brand!r} name={name!r} — SKU will be blank.")
            return no_match

        target_size = _normalize_size(size)
        available_sizes = []
        size_matches = []
        for sku, row_size, product_name in rows:
            available_sizes.append(row_size)
            if _normalize_size(row_size) == target_size:
                size_matches.append((sku, row_size, product_name))

        if not size_matches:
            print(f"[competitor_db] Brand/Name matched {len(rows)} row(s), but none had size={size!r}. "
                  f"Sizes available: {available_sizes} — SKU will be blank.")
            return no_match

        chosen = size_matches[0]
        if len(size_matches) > 1:
            # Multiple rows matched brand+name+size — almost certainly a regular vs
            # Tester duplicate. Use our extracted condition to pick the right one.
            wants_tester = (condition or "").strip().lower() == "tester"
            match_found = False
            for row in size_matches:
                if _is_tester_row(row[0], row[2]) == wants_tester:
                    chosen = row
                    match_found = True
                    break
            if not match_found:
                print(f"[competitor_db] {len(size_matches)} size-matching rows found, none clearly matched "
                      f"condition={condition!r} — defaulting to first.")

        sku, _, product_name = chosen
        print(f"[competitor_db] Match found — SKU={sku!r}, product_name={product_name!r}")
        return {"sku": sku, "product_name": product_name}

    except psycopg2.Error as e:
        # Network/DB errors shouldn't crash the whole save — just log and leave everything blank
        print(f"[competitor_db] Search failed: {e}")
        if conn:
            conn.close()
        return no_match


def _build_product_name(brand, name, size, concentration, gender, is_tester=False):
    """Builds the same style full display name item_master already uses,
    e.g. 'Emir Opulentia Flame By Paris Corner 100 ml EDP Unisex Perfume'."""
    parts = [brand, name, size, concentration, gender]
    parts = [p.strip() for p in parts if p and str(p).strip()]
    if not parts:
        return None
    suffix = "Tester" if is_tester else "Perfume"
    return " ".join(parts + [suffix])


def _generate_sku_via_brand(cur, brand_name):
    """
    Generates the next SKU for a brand using brand_sku's stored prefix +
    counter. Called with an open cursor inside the same transaction that
    will insert the new item_master row and bump the counter, so a failed
    insert rolls back the counter bump too (no burned SKU numbers).

    FOR UPDATE locks the brand_sku row so two requests creating the same
    brand's SKU at the same moment can't generate a duplicate number.

    Returns:
      {"status": "ok", "sku": "...", "next_number": N}
      {"status": "no_brand"}   — brand not found in brand_sku at all
      {"status": "no_prefix"}  — brand found, but has no SKU prefix set
      {"status": "failed"}     — ran out of retries against SKU collisions
    """
    cur.execute("""
        SELECT sku_prefix, next_sku_number FROM brand_sku
        WHERE LOWER(TRIM(brand)) = LOWER(TRIM(%s))
        FOR UPDATE
    """, (brand_name,))
    row = cur.fetchone()
    if not row:
        print(f"[competitor_db] No brand found in brand_sku matching {brand_name!r}.")
        return {"status": "no_brand"}

    prefix, next_number = row
    prefix = (prefix or "").strip()
    if not prefix:
        print(f"[competitor_db] Brand {brand_name!r} found but has no SKU prefix set.")
        return {"status": "no_prefix"}

    next_number = next_number or 1001

    for attempt in range(1, MAX_SKU_RETRIES + 1):
        candidate = f"{prefix}{str(next_number).zfill(NUMBER_PAD_LENGTH)}"
        cur.execute("SELECT 1 FROM item_master WHERE LOWER(TRIM(sku)) = LOWER(TRIM(%s))", (candidate,))
        if cur.fetchone():
            print(f"[competitor_db] SKU {candidate!r} already exists (attempt {attempt}/{MAX_SKU_RETRIES}) — trying next number.")
            next_number += 1
            continue
        print(f"[competitor_db] Generated SKU {candidate!r} for brand {brand_name!r} (prefix {prefix!r})")
        return {"status": "ok", "sku": candidate, "next_number": next_number + 1}

    print(f"[competitor_db] Exhausted {MAX_SKU_RETRIES} retries generating a SKU for {brand_name!r}.")
    return {"status": "failed"}


def create_record(brand, name, size, concentration, gender):
    """
    Creates a brand-new row in item_master for an item that had no existing
    match, auto-generating its SKU from brand_sku's prefix + counter.

    Returns {"status": "ok", "sku": "..."} | {"status": "no_brand"} |
    {"status": "no_prefix"} | {"status": "failed"}
    """
    if not DATABASE_URL:
        print("[competitor_db] COMPETITOR_DATABASE_URL not set — cannot create record.")
        return {"status": "failed"}

    if not brand or not name:
        print(f"[competitor_db] Missing brand/name (brand={brand!r}, name={name!r}) — cannot create record.")
        return {"status": "failed"}

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        sku_result = _generate_sku_via_brand(cur, brand)
        if sku_result["status"] != "ok":
            conn.rollback()
            conn.close()
            return sku_result

        sku = sku_result["sku"]
        product_name = _build_product_name(brand, name, size, concentration, gender)
        # Synthetic id — this row has no real Airtable record behind it.
        fi_record_id = f"PE-{sku}"

        print(f"[competitor_db] Creating new item_master row: sku={sku!r} brand={brand!r} name={name!r}")

        cur.execute("""
            INSERT INTO item_master
                (fi_record_id, sku, brand, perfume_name, size, type, category, product_name, source, loaded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        """, (fi_record_id, sku, brand, name, size or None, concentration or None, gender or None,
              product_name, "perfume_extractor"))

        cur.execute("""
            UPDATE brand_sku SET next_sku_number = %s, updated_at = now()
            WHERE LOWER(TRIM(brand)) = LOWER(TRIM(%s))
        """, (sku_result["next_number"], brand))

        conn.commit()
        conn.close()

        print(f"[competitor_db] Record created successfully — fi_record_id={fi_record_id!r}, SKU={sku!r}")
        return {"status": "ok", "sku": sku}

    except psycopg2.Error as e:
        print(f"[competitor_db] Failed to create record: {e}")
        if conn:
            conn.rollback()
            conn.close()
        return {"status": "failed"}

        