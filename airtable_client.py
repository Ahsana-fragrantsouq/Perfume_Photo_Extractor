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


def find_sku(brand, name, size):
    """
    Search French Inventories for an exact match on Brand + Perfume Name + Size.
    Returns the SKU string if a match is found, otherwise None.

    NOTE: matching is exact for now (all three fields must match exactly, case-sensitive).
    This can be loosened to fuzzy/partial matching later if needed.
    """
    if not AIRTABLE_API_KEY:
        print("[airtable] AIRTABLE_API_KEY not set — skipping SKU lookup, leaving SKU blank.")
        return None

    if not brand or not name or not size:
        print(f"[airtable] Missing brand/name/size (brand={brand!r}, name={name!r}, size={size!r}) — skipping lookup.")
        return None

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"

    # Build an exact-match formula: AND({Brand}="X", {Perfume Name}="Y", {Size}="Z")
    formula = 'AND({%s}="%s", {%s}="%s", {%s}="%s")' % (
        FIELD_BRAND, _escape_formula_value(brand),
        FIELD_NAME, _escape_formula_value(name),
        FIELD_SIZE, _escape_formula_value(size),
    )
    params = {"filterByFormula": formula, "maxRecords": 1}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    print(f"[airtable] Searching French Inventories: brand={brand!r} name={name!r} size={size!r}")

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        records = response.json().get("records", [])

        if not records:
            print(f"[airtable] No match found for brand={brand!r} name={name!r} size={size!r} — SKU will be blank.")
            return None

        sku = records[0]["fields"].get(FIELD_SKU)
        print(f"[airtable] Match found — SKU={sku!r}")
        return sku

    except requests.RequestException as e:
        # Network/API errors shouldn't crash the whole save — just log and leave SKU blank
        print(f"[airtable] Search failed: {e}")
        return None