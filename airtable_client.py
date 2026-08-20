"""
Handles searching Airtable's inventory table to find an existing product's SKU
(and full display name), so we can link our own master_table records back to Airtable.
"""

import os
import re
import requests

# These IDs point at whichever base/table you're currently using — override in
# Render's environment variables to switch between test and production bases
# without touching code:
#   AIRTABLE_BASE_ID
#   AIRTABLE_FRENCH_INVENTORIES_TABLE_ID
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app5gOqDt9aZrW5bV")
FRENCH_INVENTORIES_TABLE_ID = os.environ.get("AIRTABLE_FRENCH_INVENTORIES_TABLE_ID", "tblL03CEHdYy1kUdQ")

# Exact field names as they appear in the Airtable table. Overridable via env vars
# if you switch to a table with different capitalization, but these defaults match
# your current table (test base uses lowercase "brand"/"size").
FIELD_BRAND = os.environ.get("AIRTABLE_FIELD_BRAND", "Brand")
FIELD_NAME = os.environ.get("AIRTABLE_FIELD_NAME", "Perfume Name")
FIELD_SIZE = os.environ.get("AIRTABLE_FIELD_SIZE", "size")
FIELD_SKU = os.environ.get("AIRTABLE_FIELD_SKU", "SKU")
FIELD_PRODUCT_NAME = os.environ.get("AIRTABLE_FIELD_PRODUCT_NAME", "Product Name")  # Airtable's own auto-generated full display name
FIELD_CONCENTRATION = os.environ.get("AIRTABLE_FIELD_CONCENTRATION", "Type")  # e.g. EDP, EDT, Parfum — single-select field
FIELD_GENDER = os.environ.get("AIRTABLE_FIELD_GENDER", "Category")  # e.g. Men, Women, Unisex — single-select field


def _escape_formula_value(value):
    """Airtable formulas use double quotes for string literals — escape any
    double quotes inside the value itself so the formula doesn't break."""
    return value.replace('"', '\\"')


def _normalize_size(size):
    """Strip spaces and lowercase, so '100 ml', '100ml', and '100 ML' all compare equal.
    Airtable and Claude's extraction don't always agree on spacing/capitalization,
    even for the exact same product."""
    if not size:
        return ""
    return size.replace(" ", "").lower()


def _is_tester_record(record):
    """No dedicated Condition field in Airtable — but SKUs ending in 'T' (e.g. TMF1035T)
    are testers, and their 'Product Name (Obsolete)' field literally contains the word
    'Tester' too. Check both, in case one is more reliable than the other for a given row."""
    sku = record["fields"].get(FIELD_SKU, "") or ""
    obsolete_name = record["fields"].get("Product Name (Obsolete)", "") or ""
    return sku.strip().upper().endswith("T") or "tester" in obsolete_name.lower()


def find_match(brand, name, size, condition=None):
    """
    Search the Airtable inventory table for a match on Brand + Perfume Name
    (case-insensitive, whitespace-trimmed), then compare Size ourselves with
    spacing/case ignored too — e.g. "100ml" matches "100 ml", and "tom ford "
    matches "Tom Ford".

    Returns a dict {"sku": ..., "product_name": ...} — either value may be None
    if no match was found. `product_name` is Airtable's own auto-generated full
    display name (e.g. "Anfasic Dokhoon Arqa Shay 75 ml EDP Unisex Perfume"),
    pulled directly from their "Product Name" field rather than rebuilt ourselves,
    so it matches their formatting exactly. If that field doesn't exist on the
    current table, this is simply None and the caller falls back to building one.

    Some products have TWO Airtable rows for the same brand/name/size — a regular
    one and a Tester (SKU ending in "T", e.g. TMF1035 vs TMF1035T). When that happens,
    `condition` (our own extracted "Sealed"/"Tester"/etc.) is used to pick the right one.
    """
    no_match = {"sku": None, "product_name": None}

    if not AIRTABLE_API_KEY:
        print("[airtable] AIRTABLE_API_KEY not set — skipping lookup.")
        return no_match

    if not brand or not name or not size:
        print(f"[airtable] Missing brand/name/size (brand={brand!r}, name={name!r}, size={size!r}) — skipping lookup.")
        return no_match

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"

    # Match on Brand + Perfume Name, ignoring case and leading/trailing whitespace —
    # thousands of hand-entered Airtable records means occasional stray spaces or
    # capitalization differences are expected, not exceptions.
    # A perfume can also have several rows for different sizes/testers, so we fetch
    # all brand+name matches and pick the right size/condition ourselves below.
    formula = 'AND(LOWER(TRIM({%s}))="%s", LOWER(TRIM({%s}))="%s")' % (
        FIELD_BRAND, _escape_formula_value(brand.strip().lower()),
        FIELD_NAME, _escape_formula_value(name.strip().lower()),
    )
    params = {"filterByFormula": formula, "maxRecords": 50}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    print(f"[airtable] Searching for: brand={brand!r} name={name!r} "
          f"(will match size={size!r}, condition={condition!r} separately)")

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        records = response.json().get("records", [])

        if not records:
            print(f"[airtable] No match found for brand={brand!r} name={name!r} — SKU will be blank.")
            return no_match

        target_size = _normalize_size(size)
        available_sizes = []

        # Collect every record whose size matches — there may be more than one
        # (a regular + a Tester version), which is why we don't just return the first hit.
        size_matches = []
        for record in records:
            record_size = record["fields"].get(FIELD_SIZE, "")
            available_sizes.append(record_size)
            if _normalize_size(record_size) == target_size:
                size_matches.append(record)

        if not size_matches:
            print(f"[airtable] Brand/Name matched {len(records)} row(s), but none had size={size!r}. "
                  f"Sizes available in Airtable: {available_sizes} — SKU will be blank.")
            return no_match

        chosen = size_matches[0]
        if len(size_matches) > 1:
            # Multiple rows matched brand+name+size — almost certainly a regular vs
            # Tester duplicate. Use our extracted condition to pick the right one.
            wants_tester = (condition or "").strip().lower() == "tester"
            match_found = False
            for record in size_matches:
                if _is_tester_record(record) == wants_tester:
                    chosen = record
                    match_found = True
                    break
            if not match_found:
                print(f"[airtable] {len(size_matches)} size-matching rows found, none clearly matched "
                      f"condition={condition!r} — defaulting to first.")

        sku = chosen["fields"].get(FIELD_SKU)
        product_name = chosen["fields"].get(FIELD_PRODUCT_NAME)
        print(f"[airtable] Match found — SKU={sku!r}, product_name={product_name!r}")
        return {"sku": sku, "product_name": product_name}

    except requests.RequestException as e:
        # Network/API errors shouldn't crash the whole save — just log and leave everything blank
        print(f"[airtable] Search failed: {e}")
        return no_match


BRANDS_TABLE_ID = os.environ.get("AIRTABLE_BRANDS_TABLE_ID", "tblBBEMijM0gb5yXX")
FIELD_BRAND_NAME = os.environ.get("AIRTABLE_FIELD_BRAND_NAME", "Brand Name")
FIELD_SKU_PREFIX = os.environ.get("AIRTABLE_FIELD_SKU_PREFIX", "SKU Prefix")
FIELD_NEXT_SKU_NUMBER = os.environ.get("AIRTABLE_FIELD_NEXT_SKU_NUMBER", "Next sku number")

NUMBER_PAD_LENGTH = 4
MAX_SKU_RETRIES = 5


def find_brand_record(brand_name):
    """Looks up a brand by name in the Brands table (case-insensitive, trimmed).
    Returns the full record dict (with 'id' and 'fields') if found, else None."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{BRANDS_TABLE_ID}"
    formula = 'LOWER(TRIM({%s}))="%s"' % (FIELD_BRAND_NAME, _escape_formula_value(brand_name.strip().lower()))
    params = {"filterByFormula": formula, "maxRecords": 1}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    records = response.json().get("records", [])
    return records[0] if records else None


def _get_brands_with_prefix(prefix):
    """All Brands-table records sharing this exact SKU Prefix (case-insensitive,
    trimmed) — several brands can legitimately share one prefix (seen in your
    own data, e.g. 'Paris Melle Elsytys' and 'Reyane Tradition' both use 'RTM'),
    so their counters must be kept in sync to avoid generating duplicate SKUs."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{BRANDS_TABLE_ID}"
    formula = 'LOWER(TRIM({%s}))="%s"' % (FIELD_SKU_PREFIX, _escape_formula_value(prefix.strip().lower()))
    params = {"filterByFormula": formula, "maxRecords": 100}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("records", [])


def _bump_brands_next_number(brand_ids, new_value):
    """Sets Next sku number to new_value on every given Brands-table record,
    batched in groups of 10 (Airtable's REST API limit per write request)."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{BRANDS_TABLE_ID}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

    for i in range(0, len(brand_ids), 10):
        batch = brand_ids[i:i + 10]
        body = {"records": [{"id": bid, "fields": {FIELD_NEXT_SKU_NUMBER: new_value}} for bid in batch]}
        response = requests.patch(url, headers=headers, json=body, timeout=15)
        response.raise_for_status()


def _sku_exists(sku):
    """Checks whether this exact SKU is already used in French Inventories."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    formula = 'LOWER(TRIM({%s}))="%s"' % (FIELD_SKU, _escape_formula_value(sku.strip().lower()))
    params = {"filterByFormula": formula, "maxRecords": 1}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return len(response.json().get("records", [])) > 0


def generate_sku_via_brand(brand_name):
    """
    Generates the next SKU for a brand using the Brands table's stored prefix +
    counter — same approach as the existing Airtable automation script, ported
    to run from our backend instead of inside Airtable.

    Returns a dict:
      {"status": "ok", "sku": "...", "brand_record_id": "...", "same_prefix_brand_ids": [...], "next_number": N}
      {"status": "no_brand"}       — brand not found in the Brands table at all
      {"status": "no_prefix"}      — brand found, but has no SKU Prefix set
      {"status": "failed"}         — ran out of retries against SKU collisions

    On "ok", the counter has NOT been bumped yet — the caller bumps it only
    after successfully creating the French Inventories record, so a failed
    creation doesn't burn a SKU number.
    """
    brand_record = find_brand_record(brand_name)
    if not brand_record:
        print(f"[airtable] No brand found in Brands table matching {brand_name!r}.")
        return {"status": "no_brand"}

    prefix = (brand_record["fields"].get(FIELD_SKU_PREFIX) or "").strip()
    if not prefix:
        print(f"[airtable] Brand {brand_name!r} found but has no SKU Prefix set.")
        return {"status": "no_prefix"}

    for attempt in range(1, MAX_SKU_RETRIES + 1):
        same_prefix = _get_brands_with_prefix(prefix)
        if not same_prefix:
            return {"status": "no_prefix"}

        max_next = 0
        for b in same_prefix:
            n = b["fields"].get(FIELD_NEXT_SKU_NUMBER)
            if isinstance(n, (int, float)) and n > max_next:
                max_next = int(n)
        if max_next == 0:
            max_next = 1

        candidate = f"{prefix}{str(max_next).zfill(NUMBER_PAD_LENGTH)}"

        if _sku_exists(candidate):
            print(f"[airtable] SKU {candidate!r} already exists (attempt {attempt}/{MAX_SKU_RETRIES}) — "
                  f"bumping counters and retrying.")
            _bump_brands_next_number([b["id"] for b in same_prefix], max_next + 1)
            continue

        print(f"[airtable] Generated SKU {candidate!r} for brand {brand_name!r} (prefix {prefix!r})")
        return {
            "status": "ok",
            "sku": candidate,
            "brand_record_id": brand_record["id"],
            "same_prefix_brand_ids": [b["id"] for b in same_prefix],
            "next_number": max_next + 1,
        }

    print(f"[airtable] Exhausted {MAX_SKU_RETRIES} retries generating a SKU for {brand_name!r}.")
    return {"status": "failed"}


def create_record(brand, name, size, concentration, gender):
    """
    Creates a brand-new record in French Inventories for an item that had no
    existing match. The Brand field is a LINKED RECORD, so this looks up the
    brand in the Brands table first — if it's not there, no record is created
    (the caller should show something like "No brands available" rather than
    treating this as a generic failure).

    Uses Airtable's `typecast` option so that Concentration/Gender values that
    don't exactly match an existing single-select option (case, spelling, etc.)
    get added as new options automatically, rather than the write failing outright.

    Returns a dict: {"status": "ok", "sku": "..."} | {"status": "no_brand"} |
    {"status": "no_prefix"} | {"status": "failed"}
    """
    if not AIRTABLE_API_KEY:
        print("[airtable] AIRTABLE_API_KEY not set — cannot create record.")
        return {"status": "failed"}

    if not brand or not name:
        print(f"[airtable] Missing brand/name (brand={brand!r}, name={name!r}) — cannot create record.")
        return {"status": "failed"}

    sku_result = generate_sku_via_brand(brand)
    if sku_result["status"] != "ok":
        return sku_result

    sku = sku_result["sku"]
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

    fields = {
        FIELD_BRAND: [{"id": sku_result["brand_record_id"]}],  # linked record field — needs id, not plain text
        FIELD_NAME: name,
        FIELD_SKU: sku,
    }
    if size:
        fields[FIELD_SIZE] = size
    if concentration:
        fields[FIELD_CONCENTRATION] = concentration
    if gender:
        fields[FIELD_GENDER] = gender

    body = {"records": [{"fields": fields}], "typecast": True}

    print(f"[airtable] Creating new record: {fields}")

    try:
        response = requests.post(url, headers=headers, json=body, timeout=15)
        response.raise_for_status()
        created = response.json()["records"][0]
        print(f"[airtable] Record created successfully — id={created['id']}, SKU={sku!r}")

        # Only bump the shared counter now that the record actually exists —
        # a failed write above means this SKU number is still free to try again.
        _bump_brands_next_number(sku_result["same_prefix_brand_ids"], sku_result["next_number"])

        return {"status": "ok", "sku": sku}

    except requests.RequestException as e:
        detail = ""
        if getattr(e, "response", None) is not None:
            detail = f" — {e.response.text}"
        print(f"[airtable] Failed to create record: {e}{detail}")
        return {"status": "failed"}