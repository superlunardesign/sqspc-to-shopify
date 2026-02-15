"""
Browser-based Squarespace product extractor.

Uses Playwright to launch a headless Chromium browser, navigate to
the Squarespace storefront, and inject a JavaScript extraction script
that runs with same-origin access to the site's JSON API.

For reviews, Playwright interacts with the page like a real user:
scrolling to the review section, clicking "Show All" / "Load More"
buttons, and then reading each review directly from the rendered DOM.

Falls back to screenshot + Claude vision only if DOM extraction fails.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# The injectable JS lives alongside this module
_JS_PATH = Path(__file__).parent / "extract_inject.js"

# ---------------------------------------------------------------------------
# Common selectors for review widgets across Squarespace themes and
# third-party review apps (Yotpo, Judge.me, Stamped, Loox, etc.)
# ---------------------------------------------------------------------------

# Buttons that expand / load all reviews
SHOW_ALL_SELECTORS = [
    # Squarespace native
    'button:has-text("Show All")',
    'button:has-text("show all")',
    'a:has-text("Show All")',
    'button:has-text("See All Reviews")',
    'button:has-text("See all reviews")',
    'a:has-text("See All Reviews")',
    'button:has-text("View All Reviews")',
    'button:has-text("View all reviews")',
    'button:has-text("View All")',
    'a:has-text("View All")',
    'button:has-text("Read All Reviews")',
    'button:has-text("Load More")',
    'button:has-text("load more")',
    'a:has-text("Load More")',
    'button:has-text("Show more")',
    'button:has-text("show more")',
    'a:has-text("Show more")',
    'button:has-text("More Reviews")',
    'a:has-text("More Reviews")',
    # Class-based
    '.load-more-btn',
    '.show-all-btn',
    '.load-more',
    '.show-more',
    # Yotpo
    '.yotpo-load-more',
    '.yotpo .load-more',
    '.yotpo-nav-btn.yotpo-next',
    # Judge.me
    '.jdgm-load-more',
    '.jdgm-paginate__more',
    # Stamped
    '.stamped-load-more',
    # Generic pagination
    '.review-pagination .next',
    '.reviews-pagination .next',
    '[data-testid="load-more-reviews"]',
    '[data-testid="show-all-reviews"]',
]

# Containers that hold individual review items
REVIEW_CONTAINER_SELECTORS = [
    # Squarespace native
    '.review-item',
    '[data-testid="review-item"]',
    '.sqs-review',
    '.product-reviews .review',
    # Yotpo
    '.yotpo-review',
    '.yotpo .review',
    # Judge.me
    '.jdgm-rev',
    '.jdgm-review',
    # Stamped
    '.stamped-review',
    # Loox
    '.loox-review',
    # Generic
    '.review',
    '[class*="review-item"]',
    '[class*="ReviewItem"]',
    '[data-review-id]',
]

# Sub-selectors within a review item
REVIEW_AUTHOR_SELECTORS = [
    '[data-testid="reviewer-name"]',
    '.review-author',
    '.reviewer-name',
    '.review-author-name',
    '.yotpo-user-name',
    '.jdgm-rev__author',
    '.stamped-review-author',
    '[class*="author"]',
    '[class*="reviewer"]',
]

REVIEW_BODY_SELECTORS = [
    '[data-testid="review-desc"]',
    '.review-body',
    '.review-content',
    '.review-text',
    '.yotpo-review-content',
    '.jdgm-rev__body',
    '.stamped-review-body',
    '[class*="review-body"]',
    '[class*="review-content"]',
    '[class*="review-text"]',
]

REVIEW_DATE_SELECTORS = [
    '[data-testid="review-date"]',
    '.review-date',
    '.yotpo-review-date',
    '.jdgm-rev__timestamp',
    '.stamped-review-date',
    '[class*="review-date"]',
    'time',
]

REVIEW_RATING_SELECTORS = [
    '[data-testid="review-stars"]',
    '.review-stars',
    '.star-rating',
    '.yotpo-review-stars',
    '.jdgm-rev__rating',
    '.stamped-review-rating',
    '[class*="star-rating"]',
    '[class*="review-rating"]',
    '[aria-label*="star"]',
    '[aria-label*="rating"]',
]

REVIEW_TITLE_SELECTORS = [
    '.review-title',
    '.yotpo-review-title',
    '.jdgm-rev__title',
    '.stamped-review-title',
    '[class*="review-title"]',
]


def _click_show_all(page, log_lines: list) -> int:
    """Find and click 'Show All' / 'Load More' buttons until none remain.

    Returns the total number of clicks performed.
    """
    total_clicks = 0
    max_rounds = 20  # safety limit

    for round_num in range(max_rounds):
        clicked = False
        for selector in SHOW_ALL_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=500):
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                    btn.click()
                    total_clicks += 1
                    clicked = True
                    log_lines.append(
                        f"[sqspc] Clicked '{selector}' (round {round_num + 1})"
                    )
                    logger.info("Clicked review button: %s (round %d)", selector, round_num + 1)
                    # Wait for content to load
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if not clicked:
            break

    return total_clicks


def _extract_rating(el) -> str:
    """Extract star rating from a review element."""
    for selector in REVIEW_RATING_SELECTORS:
        try:
            stars_el = el.locator(selector).first
            if not stars_el.is_visible(timeout=200):
                continue

            # Method 1: Count filled/active star icons
            for star_selector in [
                ".filled", ".star--filled", "[data-active]",
                ".star.active", "[class*='filled']", "svg.filled",
            ]:
                filled = stars_el.locator(star_selector)
                count = filled.count()
                if count > 0:
                    return str(count)

            # Method 2: aria-label like "5 out of 5 stars"
            aria = stars_el.get_attribute("aria-label") or ""
            import re
            m = re.match(r"(\d+(?:\.\d+)?)", aria)
            if m:
                return m.group(1)

            # Method 3: data attribute
            for attr in ["data-rating", "data-score", "data-value"]:
                val = stars_el.get_attribute(attr)
                if val:
                    return val

            # Method 4: style width percentage (e.g. width: 80% = 4 stars)
            style = stars_el.get_attribute("style") or ""
            width_match = re.search(r"width:\s*([\d.]+)%", style)
            if width_match:
                pct = float(width_match.group(1))
                return str(round(pct / 20))

        except Exception:
            continue
    return ""


def _first_text(el, selectors: list) -> str:
    """Return text content from the first matching visible sub-element."""
    for selector in selectors:
        try:
            sub = el.locator(selector).first
            if sub.is_visible(timeout=200):
                text = sub.text_content(timeout=1000)
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


def _extract_reviews_from_page(page, product_title: str, product_handle: str, log_lines: list) -> list[dict]:
    """Extract reviews from the current page DOM using Playwright selectors."""
    reviews = []

    # Find review items using various selectors
    for container_sel in REVIEW_CONTAINER_SELECTORS:
        try:
            items = page.locator(container_sel)
            count = items.count()
            if count == 0:
                continue

            logger.info("Found %d review items with selector '%s'", count, container_sel)
            log_lines.append(f"[sqspc] Found {count} review elements with '{container_sel}'")

            for i in range(count):
                item = items.nth(i)
                try:
                    if not item.is_visible(timeout=300):
                        continue

                    author = _first_text(item, REVIEW_AUTHOR_SELECTORS)
                    body = _first_text(item, REVIEW_BODY_SELECTORS)
                    date = _first_text(item, REVIEW_DATE_SELECTORS)
                    title = _first_text(item, REVIEW_TITLE_SELECTORS)
                    rating = _extract_rating(item)

                    # Skip empty reviews
                    if not author and not body:
                        continue

                    reviews.append({
                        "product_title": product_title,
                        "product_handle": product_handle,
                        "rating": rating,
                        "author": author,
                        "title": title,
                        "body": body,
                        "created_at": date,
                        "email": "",
                    })
                except Exception as exc:
                    logger.debug("Error extracting review %d: %s", i, exc)
                    continue

            # If we found reviews with this selector, don't try others
            # (avoids double-counting from overlapping selectors)
            if reviews:
                break

        except Exception:
            continue

    return reviews


def _extract_reviews_for_product(page, product: dict, log_lines: list) -> list[dict]:
    """Navigate to a product page, expand all reviews, and extract them."""
    url = product.get("url", "")
    title = product.get("title", "")
    handle = product.get("handle", "")

    if not url:
        return []

    try:
        log_lines.append(f"[sqspc] Reviews: visiting {title}...")
        logger.info("Review extraction: visiting %s", url)
        page.goto(url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3000)

        # Scroll down to trigger lazy-loading of review sections
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.75)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Click "Show All" / "Load More" buttons to expand all reviews
        clicks = _click_show_all(page, log_lines)
        if clicks > 0:
            log_lines.append(f"[sqspc] Clicked {clicks} expand button(s) on {title}")

        # Now extract all visible reviews from the DOM
        reviews = _extract_reviews_from_page(page, title, handle, log_lines)

        if reviews:
            log_lines.append(f"[sqspc] {title}: {len(reviews)} review(s) extracted")
            logger.info("Extracted %d reviews from %s", len(reviews), title)
        else:
            log_lines.append(f"[sqspc] {title}: no reviews found in DOM")
            logger.info("No reviews found in DOM for %s", title)

        return reviews

    except Exception as exc:
        logger.warning("Error extracting reviews for %s: %s", title, exc)
        log_lines.append(f"[sqspc] Error on {title}: {exc}")
        return []


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

            # -----------------------------------------------------------------
            # Phase 2: Playwright-driven review extraction
            #
            # Visit each product page, scroll down, click "Show All" / "Load
            # More" buttons, then read reviews straight from the DOM.  This is
            # the primary review strategy — it works like a human browsing the
            # site.
            # -----------------------------------------------------------------
            if result["products"] and not result["reviews"]:
                log_lines.append("[sqspc] === Phase 2: Playwright review extraction ===")
                logger.info("Browser extractor: starting Playwright review extraction")

                all_reviews = []
                for product in result["products"]:
                    page_reviews = _extract_reviews_for_product(page, product, log_lines)
                    all_reviews.extend(page_reviews)
                    page.wait_for_timeout(800)  # rate limit

                result["reviews"] = all_reviews
                logger.info(
                    "Browser extractor: Playwright review extraction — %d reviews total",
                    len(all_reviews),
                )

            # -----------------------------------------------------------------
            # Phase 3 (fallback): Screenshot + Claude vision
            #
            # Only used if Playwright DOM extraction also found nothing, and an
            # ANTHROPIC_API_KEY is available.
            # -----------------------------------------------------------------
            has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
            if result["products"] and not result["reviews"] and has_api_key:
                logger.info("Browser extractor: DOM extraction found no reviews, trying vision fallback")
                log_lines.append("[sqspc] === Phase 3: Screenshot + AI vision fallback ===")

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
                    "[sqspc] No reviews found via DOM. Set ANTHROPIC_API_KEY to enable "
                    "screenshot + AI vision as a last resort."
                )

            browser.close()

    except Exception as exc:
        logger.exception("Browser extractor failed: %s", exc)
        result["log"].append(f"ERROR: {exc}")

    return result
