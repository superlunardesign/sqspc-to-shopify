"""
Shopify-compatible CSV exporter.

Generates two CSV files:
1. **products.csv** – Shopify product import format
2. **reviews.csv** – Compatible with popular Shopify review apps
   (Judge.me, Loox, Stamped, Yotpo, etc.)

The product CSV also embeds ingredients, how-to-use, and benefits
into the product body HTML so they transfer to Shopify cleanly.
"""

import csv
import io
import html as html_module
from typing import IO


# ---------------------------------------------------------------------------
# Shopify product CSV columns
# ---------------------------------------------------------------------------
PRODUCT_CSV_HEADERS = [
    "Handle",
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

# ---------------------------------------------------------------------------
# Review CSV columns (Judge.me / generic format)
# ---------------------------------------------------------------------------
REVIEW_CSV_HEADERS = [
    "product_handle",
    "state",
    "rating",
    "title",
    "author",
    "email",
    "body",
    "created_at",
]


def _build_body_html(product: dict) -> str:
    """Compose the Shopify Body (HTML) field.

    Starts with the original product body, then appends structured
    sections (ingredients, how to use, benefits, what it is, who it's
    for) as formatted HTML blocks if they exist.
    """
    parts = []

    body = product.get("body_html", "").strip()
    if body:
        parts.append(body)

    for label, key in [
        ("What It Is", "what_it_is"),
        ("Benefits", "benefits"),
        ("Who It's For", "who_its_for"),
        ("How to Use", "how_to_use"),
        ("Ingredients", "ingredients"),
    ]:
        value = product.get(key, "").strip()
        if value:
            safe = html_module.escape(value).replace("\n", "<br>")
            parts.append(f"<h3>{label}</h3>\n<p>{safe}</p>")

    return "\n".join(parts)


def export_products_csv(products: list[dict], dest: IO[str] | None = None) -> str:
    """Write the Shopify products CSV.

    Parameters
    ----------
    products : list[dict]
        Product dicts as returned by :func:`scraper.scrape_products`.
    dest : file-like, optional
        If provided the CSV is written there; otherwise an in-memory
        string is returned.

    Returns
    -------
    str
        The CSV content (also written to *dest* when supplied).
    """
    buf = dest or io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PRODUCT_CSV_HEADERS, extrasaction="ignore")
    writer.writeheader()

    for product in products:
        handle = product.get("handle", "")
        title = product.get("title", "")
        body_html = _build_body_html(product)
        tags = ", ".join(product.get("tags", []) + product.get("categories", []))
        images = product.get("images", [])
        variants = product.get("variants", [{}])
        description = product.get("description", "")

        # Determine option names from the first variant
        option_names = []
        if variants and variants[0].get("option_values"):
            option_names = [ov[0] for ov in variants[0]["option_values"]]

        vendor = product.get("vendor", "")
        product_type = product.get("product_type", "")
        sold_out = product.get("sold_out", False)
        status = "draft" if sold_out else "active"

        for v_idx, variant in enumerate(variants):
            row = {
                "Handle": handle,
                "Title": title if v_idx == 0 else "",
                "Body (HTML)": body_html if v_idx == 0 else "",
                "Vendor": vendor if v_idx == 0 else "",
                "Product Category": "",
                "Type": product_type if v_idx == 0 else "",
                "Tags": tags if v_idx == 0 else "",
                "Published": "FALSE" if sold_out else "TRUE",
                "Variant SKU": variant.get("sku", ""),
                "Variant Grams": "",
                "Variant Inventory Tracker": "shopify",
                "Variant Inventory Qty": variant.get("inventory_qty", ""),
                "Variant Inventory Policy": "deny",
                "Variant Fulfillment Service": "manual",
                "Variant Price": variant.get("price", ""),
                "Variant Compare At Price": variant.get("compare_at_price", ""),
                "Variant Requires Shipping": "TRUE",
                "Variant Taxable": "TRUE",
                "Variant Barcode": "",
                "Gift Card": "FALSE",
                "SEO Title": title if v_idx == 0 else "",
                "SEO Description": description[:320] if v_idx == 0 else "",
                "Variant Image": variant.get("image", ""),
                "Variant Weight Unit": variant.get("weight_unit", "lb").lower()
                .replace("pounds", "lb")
                .replace("ounces", "oz")
                .replace("kilograms", "kg")
                .replace("grams", "g"),
                "Variant Tax Code": "",
                "Cost per item": "",
                "Status": status,
            }

            # Options
            opt_values = variant.get("option_values", [])
            for i in range(3):
                name_key = f"Option{i+1} Name"
                val_key = f"Option{i+1} Value"
                if i < len(opt_values):
                    row[name_key] = opt_values[i][0] if v_idx == 0 or i < len(option_names) else ""
                    row[val_key] = opt_values[i][1]
                else:
                    row[name_key] = ""
                    row[val_key] = ""

            # If first variant row, set the first image
            if v_idx == 0 and images:
                row["Image Src"] = images[0]
                row["Image Position"] = "1"
                row["Image Alt Text"] = title
            else:
                row["Image Src"] = ""
                row["Image Position"] = ""
                row["Image Alt Text"] = ""

            writer.writerow(row)

        # Additional image rows (image 2+)
        for img_idx, img_url in enumerate(images[1:], start=2):
            img_row = {h: "" for h in PRODUCT_CSV_HEADERS}
            img_row["Handle"] = handle
            img_row["Image Src"] = img_url
            img_row["Image Position"] = str(img_idx)
            img_row["Image Alt Text"] = title
            writer.writerow(img_row)

    if dest is None:
        return buf.getvalue()
    return ""


def export_reviews_csv(reviews: list[dict], dest: IO[str] | None = None) -> str:
    """Write the reviews CSV.

    The format is compatible with Judge.me and most Shopify review
    apps that support CSV import.

    Parameters
    ----------
    reviews : list[dict]
        Review dicts as returned by :func:`scraper.scrape_reviews`.
    dest : file-like, optional
        Write target; in-memory string returned when *None*.

    Returns
    -------
    str
        The CSV content.
    """
    buf = dest or io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REVIEW_CSV_HEADERS, extrasaction="ignore")
    writer.writeheader()

    for review in reviews:
        # Normalise rating to integer 1-5
        raw_rating = review.get("rating", "")
        try:
            rating = str(round(float(raw_rating))) if raw_rating else ""
        except (ValueError, TypeError):
            rating = str(raw_rating) if raw_rating else ""

        # Skip reviews with no body (Judge.me requires body text)
        body = (review.get("body") or "").strip()
        if not body:
            continue

        writer.writerow(
            {
                "product_handle": review.get("product_handle", ""),
                "state": "published",
                "rating": rating,
                "title": review.get("title", ""),
                "author": review.get("author", "") or "Anonymous",
                "email": review.get("email", ""),
                "body": body,
                "created_at": review.get("created_at", ""),
            }
        )

    if dest is None:
        return buf.getvalue()
    return ""
