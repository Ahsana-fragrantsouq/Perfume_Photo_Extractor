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

# Exact field names as they appear in the Airtable table. Also overridable via env
# vars, since different bases (e.g. a test base vs. the real French Inventories)
# can use different capitalization for the same fields — e.g. "brand" vs "Brand".
FIELD_BRAND = os.environ.get("AIRTABLE_FIELD_BRAND", "Brand")
FIELD_NAME = os.environ.get("AIRTABLE_FIELD_NAME", "Perfume Name")
FIELD_SIZE = os.environ.get("AIRTABLE_FIELD_SIZE", "Size")
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


def _generate_brand_prefix(brand):
    """Builds a 3-letter prefix from a brand name for brand-new SKUs, e.g.
    'Tom Ford' -> 'TF' -> padded to 'TFO'. Best-effort — existing brands reuse
    their real prefix instead (see _find_existing_sku_pattern); this only
    kicks in for a brand with zero prior Airtable records."""
    words = re.findall(r"[A-Za-z]+", brand)
    letters = "".join(w[0].upper() for w in words if w)
    if len(letters) >= 3:
        return letters[:3]
    if words:
        # Not enough separate words — pad using extra letters from the last word
        last_word = words[-1]
        return (letters + last_word[1:].upper())[:3]
    return "GEN"


def _find_existing_sku_pattern(brand):
    """Looks at this brand's existing SKUs in Airtable to find its established
    prefix + the highest number used so far. Returns (prefix, next_number, pad_width)
    or None if this brand has no existing records."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    formula = 'LOWER(TRIM({%s}))="%s"' % (FIELD_BRAND, _escape_formula_value(brand.strip().lower()))
    params = {"filterByFormula": formula, "maxRecords": 100, "fields[]": FIELD_SKU}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    records = response.json().get("records", [])

    parsed = []
    for record in records:
        sku = (record["fields"].get(FIELD_SKU) or "").strip()
        m = re.match(r"^([A-Za-z]+)(\d+)", sku)
        if m:
            parsed.append((m.group(1).upper(), m.group(2)))

    if not parsed:
        return None

    # Use whichever prefix is most common among this brand's existing SKUs
    prefixes = [p for p, _ in parsed]
    prefix = max(set(prefixes), key=prefixes.count)
    numbers = [int(n) for p, n in parsed if p == prefix]
    pad_width = max(len(n) for p, n in parsed if p == prefix)
    return prefix, max(numbers) + 1, pad_width


def _find_next_number_for_prefix(prefix):
    """When a brand has no existing SKUs, we still need to avoid colliding with
    some OTHER brand that happens to use the same generated prefix. Checks all
    SKUs starting with this prefix (any brand) and returns the next free number."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    formula = 'LEFT(UPPER({%s}), %d)="%s"' % (FIELD_SKU, len(prefix), prefix)
    params = {"filterByFormula": formula, "maxRecords": 100, "fields[]": FIELD_SKU}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    records = response.json().get("records", [])

    numbers = []
    for record in records:
        sku = (record["fields"].get(FIELD_SKU) or "").strip()
        m = re.match(r"^" + re.escape(prefix) + r"(\d+)", sku, re.IGNORECASE)
        if m:
            numbers.append(int(m.group(1)))

    return (max(numbers) + 1) if numbers else 1001


def generate_sku(brand):
    """
    Auto-generates a new SKU for a brand, matching the style of existing SKUs
    (e.g. 'TMF1035', 'GRA1001'): a short brand prefix + an incrementing number.

    - If this brand already has SKUs in Airtable, reuses that brand's real
      prefix and continues the numbering from its highest existing SKU.
    - If not, builds a new prefix from the brand name and starts at ...1001,
      checking for collisions with any other brand already using that prefix.
    """
    existing = _find_existing_sku_pattern(brand)
    if existing:
        prefix, next_number, pad_width = existing
        sku = f"{prefix}{str(next_number).zfill(pad_width)}"
        print(f"[airtable] Generated SKU {sku!r} continuing {brand!r}'s existing prefix {prefix!r}")
        return sku

    prefix = _generate_brand_prefix(brand)
    next_number = _find_next_number_for_prefix(prefix)
    sku = f"{prefix}{str(next_number).zfill(4)}"
    print(f"[airtable] Generated SKU {sku!r} with new prefix {prefix!r} for brand {brand!r} (no prior records)")
    return sku


def create_record(brand, name, size, concentration, gender):
    """
    Creates a brand-new record in the Airtable inventory table for an item that
    had no existing match, with an auto-generated SKU.

    Uses Airtable's `typecast` option so that Concentration/Gender values that
    don't exactly match an existing single-select option (case, spelling, etc.)
    get added as new options automatically, rather than the write failing outright —
    the existing option lists for these fields aren't perfectly consistent already.

    Returns the new SKU string on success, or None if the write failed (logged either way).
    """
    if not AIRTABLE_API_KEY:
        print("[airtable] AIRTABLE_API_KEY not set — cannot create record.")
        return None

    if not brand or not name:
        print(f"[airtable] Missing brand/name (brand={brand!r}, name={name!r}) — cannot create record.")
        return None

    sku = generate_sku(brand)

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

    fields = {
        FIELD_BRAND: brand,
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
        response = requests.post(url, headers=headers, json=body, timeout=10)
        response.raise_for_status()
        created = response.json()["records"][0]
        print(f"[airtable] Record created successfully — id={created['id']}, SKU={sku!r}")
        return sku
    except requests.RequestException as e:
        detail = ""
        if getattr(e, "response", None) is not None:
            detail = f" — {e.response.text}"
        print(f"[airtable] Failed to create record: {e}{detail}")
        return None