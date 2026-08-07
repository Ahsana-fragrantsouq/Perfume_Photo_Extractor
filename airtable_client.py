"""
Handles searching the Airtable "French Inventories" table to find an existing
product's SKU, so we can link our own master_table records back to Airtable.
"""

import os
import requests

# These IDs are specific to Fragrant Souq's existing Airtable base — not secret,
# so it's fine to have them as defaults, but can be overridden via env vars if needed.
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app5gOqDt9aZrW5bV")
FRENCH_INVENTORIES_TABLE_ID = os.environ.get("AIRTABLE_FRENCH_INVENTORIES_TABLE_ID", "tblL03CEHdYy1kUdQ")

# Exact field names as they appear in the French Inventories Airtable table
FIELD_BRAND = "Brand"
FIELD_NAME = "Perfume Name"
FIELD_SIZE = "Size"
FIELD_SKU = "SKU"


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
    """Airtable has no dedicated Condition field — but SKUs ending in 'T' (e.g. TMF1035T)
    are testers, and their 'Product Name (Obsolete)' field literally contains the word
    'Tester' too. Check both, in case one is more reliable than the other for a given row."""
    sku = record["fields"].get(FIELD_SKU, "") or ""
    obsolete_name = record["fields"].get("Product Name (Obsolete)", "") or ""
    return sku.strip().upper().endswith("T") or "tester" in obsolete_name.lower()


def find_sku(brand, name, size, condition=None):
    """
    Search French Inventories for a match on Brand + Perfume Name (exact), then
    compare Size ourselves with spacing/case ignored — e.g. "100ml" matches "100 ml".
    Returns the SKU string if a match is found, otherwise None.

    Brand + Name matching is exact (case-sensitive). Size matching ignores spacing
    and capitalization, since real Airtable data isn't always consistent there.

    Some products have TWO Airtable rows for the same brand/name/size — a regular
    one and a Tester (SKU ending in "T", e.g. TMF1035 vs TMF1035T). When that happens,
    `condition` (our own extracted "Sealed"/"Tester"/etc.) is used to pick the right one.
    """
    if not AIRTABLE_API_KEY:
        print("[airtable] AIRTABLE_API_KEY not set — skipping SKU lookup, leaving SKU blank.")
        return None

    if not brand or not name or not size:
        print(f"[airtable] Missing brand/name/size (brand={brand!r}, name={name!r}, size={size!r}) — skipping lookup.")
        return None

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"

    # Match on Brand + Perfume Name only — a perfume can have several rows for
    # different sizes (and testers), so we fetch all of them and pick ourselves.
    formula = 'AND({%s}="%s", {%s}="%s")' % (
        FIELD_BRAND, _escape_formula_value(brand),
        FIELD_NAME, _escape_formula_value(name),
    )
    params = {"filterByFormula": formula, "maxRecords": 50}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    print(f"[airtable] Searching French Inventories: brand={brand!r} name={name!r} "
          f"(will match size={size!r}, condition={condition!r} separately)")

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        records = response.json().get("records", [])

        if not records:
            print(f"[airtable] No match found for brand={brand!r} name={name!r} — SKU will be blank.")
            return None

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
            return None

        if len(size_matches) == 1:
            sku = size_matches[0]["fields"].get(FIELD_SKU)
            print(f"[airtable] Match found — SKU={sku!r}")
            return sku

        # Multiple rows matched brand+name+size — almost certainly a regular vs
        # Tester duplicate. Use our extracted condition to pick the right one.
        wants_tester = (condition or "").strip().lower() == "tester"
        for record in size_matches:
            if _is_tester_record(record) == wants_tester:
                sku = record["fields"].get(FIELD_SKU)
                print(f"[airtable] {len(size_matches)} size-matching rows found — "
                      f"picked SKU={sku!r} based on condition={condition!r} (wants_tester={wants_tester})")
                return sku

        # Condition didn't clearly point to one — fall back to the first match rather
        # than returning nothing, but flag it so it's easy to notice in the logs.
        sku = size_matches[0]["fields"].get(FIELD_SKU)
        print(f"[airtable] {len(size_matches)} size-matching rows found, none clearly matched "
              f"condition={condition!r} — defaulting to first: SKU={sku!r}")
        return sku

    except requests.RequestException as e:
        # Network/API errors shouldn't crash the whole save — just log and leave SKU blank
        print(f"[airtable] Search failed: {e}")
        return None