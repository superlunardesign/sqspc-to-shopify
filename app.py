"""
Squarespace → Shopify Migrator – Flask web app.

Provides a simple web UI and REST API for scraping Squarespace
storefronts and downloading Shopify-compatible CSV exports.
"""

import os
import uuid
import logging
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file

from scraper import scrape_products, scrape_reviews
from exporter import export_products_csv, export_reviews_csv
from merger import parse_shopify_csv, compare_products, merge_products

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/tmp/sqspc_exports"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/scrape", methods=["POST"])
def scrape():
    """Run the scraper and return download links for the generated CSVs."""
    data = request.get_json(force=True)
    site_url = (data.get("site_url") or "").strip().rstrip("/")
    shop_path = (data.get("shop_path") or "/shop").strip()
    do_reviews = data.get("scrape_reviews", True)
    do_products = data.get("scrape_products", True)

    if not site_url:
        return jsonify({"error": "site_url is required"}), 400

    # Ensure the URL has a scheme
    if not site_url.startswith("http"):
        site_url = f"https://{site_url}"

    # Ensure shop_path starts with /
    if not shop_path.startswith("/"):
        shop_path = f"/{shop_path}"

    run_id = uuid.uuid4().hex[:12]
    result = {"product_count": 0, "review_count": 0, "sold_out_count": 0}

    try:
        # ----- Products ----------------------------------------------------
        products = []
        if do_products:
            logger.info("Starting product scrape for %s%s", site_url, shop_path)
            products = scrape_products(site_url, shop_path)
            result["product_count"] = len(products)
            result["sold_out_count"] = sum(
                1 for p in products if p.get("sold_out")
            )

            if products:
                fname = f"products_{run_id}.csv"
                fpath = OUTPUT_DIR / fname
                with open(fpath, "w", newline="", encoding="utf-8") as f:
                    export_products_csv(products, f)
                result["products_csv"] = fname
                logger.info("Wrote %s (%d products)", fpath, len(products))

                # Include product summaries for the table UI
                result["products"] = [
                    {
                        "title": p.get("title", ""),
                        "handle": p.get("handle", ""),
                        "vendor": p.get("vendor", ""),
                        "price": (p.get("variants") or [{}])[0].get("price", ""),
                        "compare_at_price": (p.get("variants") or [{}])[0].get("compare_at_price", ""),
                        "sku": (p.get("variants") or [{}])[0].get("sku", ""),
                        "images": p.get("images", [])[:5],
                        "tags": p.get("tags", []),
                        "product_type": p.get("product_type", ""),
                        "sold_out": p.get("sold_out", False),
                        "variant_count": len(p.get("variants", [])),
                        "description": (p.get("description", "") or "")[:200],
                    }
                    for p in products
                ]

        # ----- Reviews -----------------------------------------------------
        if do_reviews and products:
            logger.info("Starting review scrape for %d products", len(products))
            reviews = scrape_reviews(site_url, products)
            result["review_count"] = len(reviews)

            if reviews:
                fname = f"reviews_{run_id}.csv"
                fpath = OUTPUT_DIR / fname
                with open(fpath, "w", newline="", encoding="utf-8") as f:
                    export_reviews_csv(reviews, f)
                result["reviews_csv"] = fname
                logger.info("Wrote %s (%d reviews)", fpath, len(reviews))

    except Exception as exc:
        logger.exception("Scrape failed")
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@app.route("/download/<filename>")
def download(filename):
    """Serve a previously generated CSV file."""
    # Sanitise – only allow simple filenames
    safe = Path(filename).name
    fpath = OUTPUT_DIR / safe
    if not fpath.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(fpath, as_attachment=True, mimetype="text/csv")


# ---------------------------------------------------------------------------
# Merge routes
# ---------------------------------------------------------------------------
@app.route("/compare", methods=["POST"])
def compare():
    """Upload a Shopify CSV and a Squarespace CSV, return a field-level diff."""
    shopify_file = request.files.get("shopify_csv")
    sqspc_file = request.files.get("squarespace_csv")

    if not shopify_file or not sqspc_file:
        return jsonify({"error": "Both shopify_csv and squarespace_csv files are required"}), 400

    try:
        shopify_content = shopify_file.read().decode("utf-8-sig")
        sqspc_content = sqspc_file.read().decode("utf-8-sig")

        shopify_data = parse_shopify_csv(shopify_content)
        sqspc_data = parse_shopify_csv(sqspc_content)

        if not shopify_data:
            return jsonify({"error": "No products found in Shopify CSV. Check the file has a 'Handle' column."}), 400
        if not sqspc_data:
            return jsonify({"error": "No products found in Squarespace CSV. Check the file has a 'Handle' column."}), 400

        # Store parsed data for the merge step
        run_id = uuid.uuid4().hex[:12]
        sh_path = OUTPUT_DIR / f"_shopify_{run_id}.csv"
        sq_path = OUTPUT_DIR / f"_sqspc_{run_id}.csv"
        sh_path.write_text(shopify_content, encoding="utf-8")
        sq_path.write_text(sqspc_content, encoding="utf-8")

        diff = compare_products(shopify_data, sqspc_data)
        diff["run_id"] = run_id

        logger.info(
            "Compare: %d matched, %d shopify-only, %d squarespace-only, %d with diffs",
            diff["summary"]["matched"],
            diff["summary"]["shopify_only"],
            diff["summary"]["squarespace_only"],
            diff["summary"]["with_diffs"],
        )

        return jsonify(diff)

    except Exception as exc:
        logger.exception("Compare failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/merge", methods=["POST"])
def merge():
    """Apply merge decisions and produce a downloadable merged CSV."""
    data = request.get_json(force=True)
    run_id = data.get("run_id")
    decisions = data.get("decisions", {})

    if not run_id:
        return jsonify({"error": "run_id is required"}), 400

    sh_path = OUTPUT_DIR / f"_shopify_{run_id}.csv"
    sq_path = OUTPUT_DIR / f"_sqspc_{run_id}.csv"

    if not sh_path.is_file() or not sq_path.is_file():
        return jsonify({"error": "Comparison data expired. Please re-upload the CSVs."}), 404

    try:
        shopify_data = parse_shopify_csv(sh_path.read_text(encoding="utf-8"))
        sqspc_data = parse_shopify_csv(sq_path.read_text(encoding="utf-8"))

        merged_csv = merge_products(shopify_data, sqspc_data, decisions)

        fname = f"merged_{run_id}.csv"
        fpath = OUTPUT_DIR / fname
        fpath.write_text(merged_csv, encoding="utf-8")

        logger.info("Wrote merged CSV: %s", fpath)

        return jsonify({"merged_csv": fname})

    except Exception as exc:
        logger.exception("Merge failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
