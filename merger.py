"""
CSV Merge Engine – compare and merge Squarespace-scraped and Shopify product CSVs.

Provides field-level diff detection and user-directed merge strategies
so you can keep Shopify edits (tags, collections, titles, etc.) while
filling in data from the Squarespace scrape.
"""

import csv
import io
from collections import OrderedDict

from exporter import PRODUCT_CSV_HEADERS

# Fields that appear only on the *first* variant row for a product and
# should not be compared across additional variant/image rows.
PRODUCT_LEVEL_FIELDS = {
    "Title",
    "Body (HTML)",
    "Vendor",
    "Product Category",
    "Type",
    "Tags",
    "Published",
    "SEO Title",
    "SEO Description",
    "Status",
}

# Fields shown in the merge UI for the user to decide on.
# Order matters — it controls UI column order.
MERGEABLE_FIELDS = [
    "Title",
    "Body (HTML)",
    "Vendor",
    "Product Category",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option1 Value",
    "Option2 Name",
    "Option2 Value",
    "Option3 Name",
    "Option3 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Qty",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Compare At Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Variant Barcode",
    "Image Src",
    "Image Position",
    "Image Alt Text",
    "Gift Card",
    "SEO Title",
    "SEO Description",
    "Variant Image",
    "Variant Weight Unit",
    "Variant Tax Code",
    "Cost per item",
    "Status",
]

# Compact labels for the merge UI (shorter than the full header names).
FIELD_LABELS = {
    "Title": "Title",
    "Body (HTML)": "Body HTML",
    "Vendor": "Vendor",
    "Product Category": "Category",
    "Type": "Type",
    "Tags": "Tags",
    "Published": "Published",
    "Variant SKU": "SKU",
    "Variant Price": "Price",
    "Variant Compare At Price": "Compare Price",
    "Image Src": "Image",
    "SEO Title": "SEO Title",
    "SEO Description": "SEO Desc",
    "Status": "Status",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _normalise(val: str) -> str:
    """Strip and normalise whitespace for comparison."""
    return " ".join((val or "").split())


def parse_shopify_csv(file_content: str) -> dict:
    """Parse a Shopify product CSV into a dict keyed by Handle.

    Each value is a list of row-dicts (one per CSV row sharing that handle),
    preserving variant and image rows.

    Returns
    -------
    dict[str, list[dict]]
        {handle: [row_dict, ...]}
    """
    reader = csv.DictReader(io.StringIO(file_content))
    products: dict[str, list[dict]] = OrderedDict()

    for row in reader:
        handle = (row.get("Handle") or "").strip()
        if not handle:
            continue
        products.setdefault(handle, []).append(row)

    return products


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_products(shopify: dict, squarespace: dict) -> dict:
    """Compare two product CSVs and return a diff structure.

    Parameters
    ----------
    shopify : dict
        Parsed Shopify CSV (from parse_shopify_csv).
    squarespace : dict
        Parsed Squarespace CSV (from parse_shopify_csv applied to the
        exported scrape CSV).

    Returns
    -------
    dict with keys:
        matched    – list of products present in both, with per-field diffs
        shopify_only – handles only in Shopify
        squarespace_only – handles only in Squarespace
        fields     – ordered list of field names shown in the UI
        field_labels – shorter display labels
    """
    all_shopify = set(shopify.keys())
    all_sqspc = set(squarespace.keys())

    matched_handles = sorted(all_shopify & all_sqspc)
    shopify_only = sorted(all_shopify - all_sqspc)
    squarespace_only = sorted(all_sqspc - all_shopify)

    matched = []
    for handle in matched_handles:
        sh_rows = shopify[handle]
        sq_rows = squarespace[handle]
        # Compare the first (primary) row of each product
        sh_primary = sh_rows[0] if sh_rows else {}
        sq_primary = sq_rows[0] if sq_rows else {}

        diffs = {}
        has_diff = False
        for field in MERGEABLE_FIELDS:
            sh_val = (sh_primary.get(field) or "").strip()
            sq_val = (sq_primary.get(field) or "").strip()
            changed = _normalise(sh_val) != _normalise(sq_val)
            if changed:
                has_diff = True
            diffs[field] = {
                "shopify": sh_val,
                "squarespace": sq_val,
                "changed": changed,
            }

        matched.append({
            "handle": handle,
            "title": sh_primary.get("Title") or sq_primary.get("Title") or handle,
            "has_diff": has_diff,
            "fields": diffs,
            "shopify_rows": len(sh_rows),
            "squarespace_rows": len(sq_rows),
        })

    return {
        "matched": matched,
        "shopify_only": shopify_only,
        "squarespace_only": squarespace_only,
        "fields": MERGEABLE_FIELDS,
        "field_labels": FIELD_LABELS,
        "summary": {
            "total_shopify": len(all_shopify),
            "total_squarespace": len(all_sqspc),
            "matched": len(matched_handles),
            "shopify_only": len(shopify_only),
            "squarespace_only": len(squarespace_only),
            "with_diffs": sum(1 for m in matched if m["has_diff"]),
        },
    }


# ---------------------------------------------------------------------------
# Merge engine
# ---------------------------------------------------------------------------

def merge_products(
    shopify: dict,
    squarespace: dict,
    decisions: dict,
    include_shopify_only: bool = True,
    include_squarespace_only: bool = True,
) -> str:
    """Apply merge decisions and produce a final Shopify-compatible CSV.

    Parameters
    ----------
    shopify : dict
        Parsed Shopify CSV.
    squarespace : dict
        Parsed Squarespace-scraped CSV.
    decisions : dict
        Merge decisions from the UI.  Structure:
        {
            "defaults": {                   # column-level defaults
                "<field>": "shopify" | "squarespace" | "fill_empty",
                ...
            },
            "products": {                   # per-product overrides
                "<handle>": {
                    "<field>": "shopify" | "squarespace" | "fill_empty",
                    ...
                },
                ...
            },
            "include_shopify_only": bool,
            "include_squarespace_only": bool,
        }
    include_shopify_only : bool
        Whether to include products only found in Shopify.
    include_squarespace_only : bool
        Whether to include products only found in Squarespace.

    Returns
    -------
    str
        The merged CSV content as a string.
    """
    defaults = decisions.get("defaults", {})
    product_overrides = decisions.get("products", {})
    inc_sh_only = decisions.get("include_shopify_only", include_shopify_only)
    inc_sq_only = decisions.get("include_squarespace_only", include_squarespace_only)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PRODUCT_CSV_HEADERS, extrasaction="ignore")
    writer.writeheader()

    all_handles = list(OrderedDict.fromkeys(
        list(shopify.keys()) + list(squarespace.keys())
    ))

    for handle in all_handles:
        sh_rows = shopify.get(handle, [])
        sq_rows = squarespace.get(handle, [])

        in_both = bool(sh_rows) and bool(sq_rows)
        sh_only = bool(sh_rows) and not bool(sq_rows)
        sq_only = not bool(sh_rows) and bool(sq_rows)

        if sh_only and not inc_sh_only:
            continue
        if sq_only and not inc_sq_only:
            continue

        # For products only in one source, just write them through
        if sh_only:
            for row in sh_rows:
                _write_row(writer, row)
            continue

        if sq_only:
            for row in sq_rows:
                _write_row(writer, row)
            continue

        # ---- Matched product: apply merge decisions ----
        overrides = product_overrides.get(handle, {})

        # Merge the primary row (first row of each)
        sh_primary = sh_rows[0] if sh_rows else {}
        sq_primary = sq_rows[0] if sq_rows else {}

        merged_primary = {}
        for field in PRODUCT_CSV_HEADERS:
            sh_val = (sh_primary.get(field) or "").strip()
            sq_val = (sq_primary.get(field) or "").strip()

            # Determine which source to use for this field
            strategy = overrides.get(field) or defaults.get(field, "shopify")

            if strategy == "squarespace":
                merged_primary[field] = sq_val
            elif strategy == "fill_empty":
                # Use Shopify value if present, otherwise fill from Squarespace
                merged_primary[field] = sh_val if sh_val else sq_val
            else:  # "shopify" (default)
                merged_primary[field] = sh_val

        # Handle is always preserved
        merged_primary["Handle"] = handle
        _write_row(writer, merged_primary)

        # Write additional variant rows from whichever source has more variants
        # Use Shopify's additional rows as the base, supplementing with Squarespace
        sh_extra = sh_rows[1:]
        sq_extra = sq_rows[1:]

        # Determine which extra rows to keep based on their type
        # (variant rows vs image-only rows)
        sh_variant_rows = [r for r in sh_extra if _is_variant_row(r)]
        sh_image_rows = [r for r in sh_extra if _is_image_row(r)]
        sq_variant_rows = [r for r in sq_extra if _is_variant_row(r)]
        sq_image_rows = [r for r in sq_extra if _is_image_row(r)]

        # Use Shopify variant rows, but fall back to Squarespace if Shopify has none
        variant_rows = sh_variant_rows if sh_variant_rows else sq_variant_rows
        for row in variant_rows:
            row["Handle"] = handle
            _write_row(writer, row)

        # For images: prefer Shopify image rows, fall back to Squarespace
        image_source = "shopify"
        img_strategy = overrides.get("Image Src") or defaults.get("Image Src", "shopify")
        if img_strategy == "squarespace":
            image_source = "squarespace"
        elif img_strategy == "fill_empty" and not sh_image_rows:
            image_source = "squarespace"

        image_rows = sh_image_rows if image_source == "shopify" else sq_image_rows
        for row in image_rows:
            row["Handle"] = handle
            _write_row(writer, row)

    return buf.getvalue()


def _write_row(writer, row: dict):
    """Write a single row, ensuring all expected fields exist."""
    clean = {h: (row.get(h) or "") for h in PRODUCT_CSV_HEADERS}
    writer.writerow(clean)


def _is_variant_row(row: dict) -> bool:
    """Check if a row is a variant row (has variant-specific data)."""
    return bool(
        (row.get("Variant SKU") or "").strip()
        or (row.get("Variant Price") or "").strip()
        or (row.get("Option1 Value") or "").strip()
    )


def _is_image_row(row: dict) -> bool:
    """Check if a row is an image-only row."""
    return (
        bool((row.get("Image Src") or "").strip())
        and not _is_variant_row(row)
        and not (row.get("Title") or "").strip()
    )
