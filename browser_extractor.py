"""
Browser-based Squarespace product extractor.

Uses Playwright to launch a headless Chromium browser, navigate to
the Squarespace storefront, and inject a JavaScript extraction script
that runs with same-origin access to the site's JSON API. This
bypasses all CORS/proxy issues and sees the page exactly as a user
would.

If JS-extracted reviews come back empty, falls back to taking
full-page screenshots and using Claude's vision to read reviews
directly from the rendered page.

Returns structured product and review data to the Python caller.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# The injectable JS lives alongside this module
_JS_PATH = Path(__file__).parent / "extract_inject.js"


def extract_via_browser(
    site_url: str,
    shop_path: str = "/shop",
    max_products: int = 0,
    timeout_ms: int = 120_000,
) -> dict:
    """Launch a headless browser, navigate to the site, and extract products.

    Parameters
    ----------
    site_url : str
        Root URL of the Squarespace store (e.g. ``https://example.com``).
    shop_path : str
        Path to the shop/product listing page (e.g. ``/shop-skincare``).
    max_products : int
        Limit the number of products to fetch (0 = unlimited).
    timeout_ms : int
        Total timeout for the extraction in milliseconds.

    Returns
    -------
    dict
        ``{"products": [...], "reviews": [...], "log": [...]}``
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed")
        return {
            "products": [],
            "reviews": [],
            "log": ["ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium"],
        }

    js_code = _JS_PATH.read_text(encoding="utf-8")

    site_url = site_url.rstrip("/")
    if not site_url.startswith("http"):
        site_url = f"https://{site_url}"
    if not shop_path.startswith("/"):
        shop_path = f"/{shop_path}"

    start_url = f"{site_url}{shop_path}"
    logger.info("Browser extractor: navigating to %s", start_url)

    result = {
        "products": [],
        "reviews": [],
        "log": [],
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()

            # Capture console.log messages from the injected script
            log_lines = []
            page.on("console", lambda msg: log_lines.append(msg.text))

            # Navigate to the shop page
            logger.info("Browser extractor: loading %s", start_url)
            page.goto(start_url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2000)

            logger.info("Browser extractor: page loaded, injecting extraction script")

            # Set configuration variables before running the script
            page.evaluate(f"""() => {{
                window.__SQSPC_SHOP_PATH = "{shop_path}";
                window.__SQSPC_MAX_PRODUCTS = {max_products};
            }}""")

            # Inject and run the async extraction script.
            # The js_code is an async IIFE: (async function() { ... })();
            # We must await it so the Promise resolves before we read the result.
            extraction_js = f"""
            async () => {{
                await {js_code}
                return window.__SQSPC_RESULT;
            }}
            """

            raw = page.evaluate(extraction_js, timeout=timeout_ms)

            if raw and isinstance(raw, dict):
                result["products"] = raw.get("products", [])
                result["reviews"] = raw.get("reviews", [])

            result["log"] = log_lines
            logger.info(
                "Browser extractor: JS extraction done — %d products, %d reviews",
                len(result["products"]),
                len(result["reviews"]),
            )

            # ----- Vision fallback for reviews ---------------------------------
            # If JS extraction found products but no reviews, try screenshot +
            # Claude vision on each product page.
            has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
            if result["products"] and not result["reviews"] and has_api_key:
                logger.info("Browser extractor: no JS reviews found, trying vision extraction")
                log_lines.append("[sqspc] No reviews found via JS — trying screenshot + AI vision...")

                from vision_reviews import extract_reviews_via_screenshot

                vision_reviews = []
                for product in result["products"]:
                    url = product.get("url", "")
                    if not url:
                        continue
                    title = product.get("title", "")
                    handle = product.get("handle", "")

                    log_lines.append(f"[sqspc] Vision: screenshotting {title}...")
                    page_reviews = extract_reviews_via_screenshot(
                        page, url, title, handle,
                    )
                    if page_reviews:
                        vision_reviews.extend(page_reviews)
                        log_lines.append(
                            f"[sqspc] Vision: found {len(page_reviews)} review(s) on {title}"
                        )
                    else:
                        log_lines.append(f"[sqspc] Vision: no reviews visible on {title}")

                result["reviews"] = vision_reviews
                logger.info(
                    "Browser extractor: vision extraction found %d reviews total",
                    len(vision_reviews),
                )
            elif result["products"] and not result["reviews"] and not has_api_key:
                log_lines.append(
                    "[sqspc] No reviews found via JS. Set ANTHROPIC_API_KEY to enable "
                    "screenshot + AI vision review extraction."
                )

            browser.close()

    except Exception as exc:
        logger.exception("Browser extractor failed: %s", exc)
        result["log"].append(f"ERROR: {exc}")

    return result
