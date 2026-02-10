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
    result = {"product_count": 0, "review_count": 0}

    try:
        # ----- Products ----------------------------------------------------
        products = []
        if do_products:
            logger.info("Starting product scrape for %s%s", site_url, shop_path)
            products = scrape_products(site_url, shop_path)
            result["product_count"] = len(products)

            if products:
                fname = f"products_{run_id}.csv"
                fpath = OUTPUT_DIR / fname
                with open(fpath, "w", newline="", encoding="utf-8") as f:
                    export_products_csv(products, f)
                result["products_csv"] = fname
                logger.info("Wrote %s (%d products)", fpath, len(products))

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
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
