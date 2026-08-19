"""
Handles searching Airtable's inventory table to find an existing product's SKU
(and full display name), so we can link our own master_table records back to Airtable.
"""

import os
import requests

# These IDs point at whichever base/table you're currently using — override in
# Render's environment variables to switch between test and production bases
# without touching code:
#   AIRTABLE_BASE_ID
#   AIRTABLE_FRENCH_INVENTORIES_TABLE_ID
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app5gOqDt9aZrW5bV")
FRENCH_INVENTORIES_TABLE_ID = os.environ.get("AIRTABLE_FRENCH_INVENTORIES_TABLE_ID", "tblL03CEHdYy1kUdQ")

# Exact field names as they appear in the Airtable table. Also overridable via env
# vars, since different bases (e.g. a test base vs. the real French Inventories)
# can use different capitalization for the same fields — e.g. "brand" vs "Brand".
FIELD_BRAND = os.environ.get("AIRTABLE_FIELD_BRAND", "Brand")
FIELD_NAME = os.environ.get("AIRTABLE_FIELD_NAME", "Perfume Name")
FIELD_SIZE = os.environ.get("AIRTABLE_FIELD_SIZE", "Size")
FIELD_SKU = os.environ.get("AIRTABLE_FIELD_SKU", "SKU")
FIELD_PRODUCT_NAME = os.environ.get("AIRTABLE_FIELD_PRODUCT_NAME", "Product Name")  # Airtable's own auto-generated full display name


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
        response = requests.get(url, headers=headers, params=params, timeout=10)
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