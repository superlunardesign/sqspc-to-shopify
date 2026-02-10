"""
Squarespace product scraper.

Scrapes products, images, descriptions, structured sections
(ingredients, how to use, benefits), and reviews from a
Squarespace storefront using its public JSON API with
HTML fallback for listing and detail pages.
"""

import json
import re
import time
import logging
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json",
}


class _RateLimiter:
    """Adaptive rate limiter that backs off on 429 responses."""

    def __init__(self, base_delay: float = 1.2):
        self.base_delay = base_delay
        self.delay = base_delay
        self.last_429_time = 0.0
        self.consecutive_429s = 0

    def wait(self):
        """Wait the current delay."""
        time.sleep(self.delay)

    def on_success(self):
        """Slowly reduce delay after successful requests."""
        self.consecutive_429s = 0
        # If we were recently rate-limited, decay very slowly
        since_429 = time.time() - self.last_429_time
        if since_429 < 60:
            # Within 60s of a 429: barely decay (0.97x per success)
            self.delay = max(self.base_delay, self.delay * 0.97)
        else:
            self.delay = max(self.base_delay, self.delay * 0.90)

    def on_rate_limit(self, retry_after: float = 0):
        """Increase delay after a 429 response."""
        self.consecutive_429s += 1
        self.last_429_time = time.time()
        if retry_after > 0:
            self.delay = max(self.delay, retry_after)
        else:
            # Exponential backoff: double the delay, cap at 30s
            self.delay = min(30, self.delay * 2)
        logger.info("Rate limited – backing off to %.1fs delay", self.delay)

    def cooldown(self, seconds: float = 0):
        """Pause after a burst of requests, e.g. between scrape phases."""
        pause = seconds or max(5, self.delay * 3)
        if self.consecutive_429s > 0:
            pause = max(pause, 15)
        logger.info("Cooling down for %.0fs before next phase", pause)
        time.sleep(pause)


# Module-level rate limiter shared across functions within a scrape run
_limiter = _RateLimiter()

# Common heading patterns for structured product sections
SECTION_PATTERNS = {
    "ingredients": re.compile(
        r"(?:key\s+)?ingredients?", re.IGNORECASE
    ),
    "how_to_use": re.compile(
        r"how\s+to\s+use|tips?\s+for\s+use|directions?|usage|instructions?", re.IGNORECASE
    ),
    "benefits": re.compile(
        r"benefits?|why\s+you.ll\s+love|why\s+clients?\s+love|features?", re.IGNORECASE
    ),
    "what_it_is": re.compile(
        r"what\s+it\s+is|about\s+this\s+product|overview", re.IGNORECASE
    ),
    "who_its_for": re.compile(
        r"who\s+(?:this|it)\s+is\s+for|ideal\s+for|best\s+for|recommended\s+for", re.IGNORECASE
    ),
}


def _extract_vendor_from_title(title: str) -> tuple[str, str]:
    """Split 'Product Name | Vendor' into (clean_title, vendor).

    Many Squarespace stores use a pipe separator in product titles.
    Returns the original title unchanged if no vendor is detected.
    """
    if "|" not in title:
        return title.strip(), ""

    parts = [p.strip() for p in title.split("|", 1)]
    # Heuristic: if the right-hand side is short (likely a brand), treat
    # it as the vendor.  If it's long, it's probably a subtitle.
    if len(parts) == 2 and len(parts[1]) < 60:
        return parts[0], parts[1]
    return title.strip(), ""


def clean_image_url(url: str) -> str:
    """Return the original full-size image URL.

    Squarespace appends query params like ``?format=300w`` or
    ``?format=750w`` to resize images.  We strip those so the
    caller gets the unmodified source image.
    """
    if not url:
        return url

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # Remove Squarespace resizing params
    params.pop("format", None)
    params.pop("quality", None)

    clean_query = urlencode(params, doseq=True)
    cleaned = parsed._replace(query=clean_query)
    return cleaned.geturl().rstrip("?")


def _get_json(session: requests.Session, url: str, retries: int = 4) -> dict | None:
    """Fetch a URL with ``?format=json`` and return the parsed JSON."""
    separator = "&" if "?" in url else "?"
    json_url = f"{url}{separator}format=json"

    for attempt in range(retries):
        try:
            resp = session.get(json_url, headers=DEFAULT_HEADERS, timeout=30)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 0))
                _limiter.on_rate_limit(retry_after)
                logger.warning("Attempt %d for %s: 429 Too Many Requests", attempt + 1, json_url)
                if attempt < retries - 1:
                    wait = retry_after if retry_after > 0 else min(30, 3 * (2 ** attempt))
                    time.sleep(wait)
                continue
            resp.raise_for_status()
            _limiter.on_success()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, json_url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _get_html(session: requests.Session, url: str, retries: int = 4) -> str | None:
    """Fetch a URL and return raw HTML."""
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
            if resp.status_code == 404:
                return None  # permanent – never retry
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 0))
                _limiter.on_rate_limit(retry_after)
                logger.warning("Attempt %d for %s: 429 Too Many Requests", attempt + 1, url)
                if attempt < retries - 1:
                    wait = retry_after if retry_after > 0 else min(30, 3 * (2 ** attempt))
                    time.sleep(wait)
                continue
            resp.raise_for_status()
            _limiter.on_success()
            return resp.text
        except requests.RequestException as exc:
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _empty_sections() -> dict:
    """Return a blank sections dict with all known keys."""
    return {
        "ingredients": "",
        "how_to_use": "",
        "benefits": "",
        "what_it_is": "",
        "who_its_for": "",
    }


def _parse_sections_from_html(html: str) -> dict:
    """Extract structured sections from HTML body."""
    sections = _empty_sections()

    if not html:
        return sections

    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: Look for headings (h1-h6, strong, b) followed by content
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "p"])

    for heading in headings:
        text = heading.get_text(strip=True)
        for key, pattern in SECTION_PATTERNS.items():
            if pattern.search(text):
                # Collect all sibling content until next heading
                content_parts = []
                sibling = heading.find_next_sibling()
                while sibling:
                    if sibling.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                        break
                    part_text = sibling.get_text(separator="\n", strip=True)
                    if part_text:
                        content_parts.append(part_text)
                    sibling = sibling.find_next_sibling()

                # If heading was a <p> with <strong>, the content might be
                # in the same <p> or the next elements
                if not content_parts and heading.name in ["strong", "b"]:
                    parent = heading.parent
                    if parent and parent.name == "p":
                        remaining = parent.get_text(strip=True)
                        remaining = pattern.sub("", remaining).strip(": \n")
                        if remaining:
                            content_parts.append(remaining)
                        sibling = parent.find_next_sibling()
                        while sibling:
                            if sibling.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                                break
                            part_text = sibling.get_text(separator="\n", strip=True)
                            if part_text:
                                content_parts.append(part_text)
                            sibling = sibling.find_next_sibling()

                if content_parts:
                    sections[key] = "\n".join(content_parts)
                break

    # Strategy 2: Look for accordion / collapsible blocks (Squarespace uses these)
    accordions = soup.find_all(class_=re.compile(r"accordion|collapsible|toggle", re.I))
    for accordion in accordions:
        title_el = accordion.find(
            class_=re.compile(r"title|header|label|trigger", re.I)
        )
        if not title_el:
            title_el = accordion.find(["h1", "h2", "h3", "h4", "h5", "h6", "button"])

        if not title_el:
            continue

        title_text = title_el.get_text(strip=True)
        for key, pattern in SECTION_PATTERNS.items():
            if pattern.search(title_text):
                body_el = accordion.find(
                    class_=re.compile(r"body|content|panel|description", re.I)
                )
                if body_el:
                    sections[key] = body_el.get_text(separator="\n", strip=True)
                break

    return sections


def _extract_additional_info(product_data: dict) -> dict:
    """Pull structured info from Squarespace product JSON fields."""
    sections = _empty_sections()

    # Check structuredContent.additionalInfoSections
    structured = product_data.get("structuredContent", {})
    if isinstance(structured, dict):
        for info_section in structured.get("additionalInfoSections", []):
            title = info_section.get("title", "")
            body = info_section.get("body", "")
            for key, pattern in SECTION_PATTERNS.items():
                if pattern.search(title):
                    if body:
                        soup = BeautifulSoup(body, "html.parser")
                        sections[key] = soup.get_text(separator="\n", strip=True)
                    break

    # Check product customContent
    custom = product_data.get("customContent", {})
    if isinstance(custom, dict):
        for field_key, field_val in custom.items():
            if isinstance(field_val, str):
                for key, pattern in SECTION_PATTERNS.items():
                    if pattern.search(field_key):
                        sections[key] = field_val
                        break

    return sections


def _extract_jsonld(html: str) -> dict:
    """Extract Product JSON-LD data from a product page.

    Returns a dict with normalised keys:
        price, currency, sku, availability, name, brand, description, image
    All values are strings; missing keys are omitted.
    """
    result = {}
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Accept both {"@type":"Product"} and lists containing one
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    data = item
                    break
            else:
                continue

        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue

        if data.get("name"):
            result["name"] = data["name"]
        if data.get("brand"):
            b = data["brand"]
            result["brand"] = b.get("name", b) if isinstance(b, dict) else str(b)
        if data.get("description"):
            result["description"] = data["description"]
        if data.get("image"):
            img = data["image"]
            result["image"] = img if isinstance(img, str) else (img[0] if isinstance(img, list) else "")

        offers = data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            if offers.get("price") is not None:
                result["price"] = str(offers["price"])
            if offers.get("priceCurrency"):
                result["currency"] = offers["priceCurrency"]
            if offers.get("sku"):
                result["sku"] = offers["sku"]
            if offers.get("availability"):
                avail = offers["availability"]
                result["in_stock"] = "InStock" in avail

        break  # only need the first Product

    return result


def _scrape_category_paths_from_html(html: str) -> list[str]:
    """Extract sub-category paths from a listing page's category navigation."""
    paths = []
    if not html:
        return paths

    soup = BeautifulSoup(html, "html.parser")

    # Desktop breadcrumb links
    for link in soup.select("a.nested-category-breadcrumb-link"):
        href = link.get("href", "")
        if href and href not in paths:
            paths.append(href)

    # Mobile dropdown options
    for option in soup.select("select.product-filter-dropdown-select option"):
        href = option.get("value", "")
        if href and href not in paths:
            paths.append(href)

    return paths


def _scrape_products_from_html(html: str, base_url: str) -> list[dict]:
    """Fallback: extract product stubs from the rendered listing page HTML.

    Returns lightweight product dicts (id, title, handle, url, price,
    images, tags, sold_out) that can be enriched via individual product
    page fetches.
    """
    products = []
    if not html:
        return products

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("div.product-list-item[data-product-id]")

    for item in items:
        product_id = item.get("data-product-id", "")

        # Tags from CSS classes like tag-toner tag-acne
        css_classes = item.get("class", [])
        tags = [
            c.replace("tag-", "")
            for c in css_classes
            if c.startswith("tag-")
        ]

        # Sold-out detection
        sold_out = "sold-out" in css_classes

        # Title & link
        link_el = item.select_one("a.product-list-item-link")
        href = link_el.get("href", "") if link_el else ""
        title_el = item.select_one(".product-list-item-title")
        raw_title = title_el.get_text(strip=True) if title_el else ""
        title, vendor = _extract_vendor_from_title(raw_title)

        # Slug from URL
        slug = href.rstrip("/").split("/")[-1] if href else ""
        product_url = f"{base_url}{href}" if href else ""

        # Price
        price_el = item.select_one(".product-list-item-price")
        price_text = price_el.get_text(strip=True) if price_el else ""
        price_text = re.sub(r"^from\s+", "", price_text, flags=re.I)
        price_text = price_text.replace("$", "").replace(",", "").strip()

        # Images from data-src / data-image (full resolution, no ?format=)
        images = []
        for img in item.select("img[data-image]"):
            src = img.get("data-image", "") or img.get("data-src", "")
            if src:
                cleaned = clean_image_url(src)
                if cleaned not in images:
                    images.append(cleaned)

        products.append({
            "id": product_id,
            "title": title,
            "handle": slug,
            "url": product_url,
            "body_html": "",
            "description": "",
            "images": images,
            "variants": [{
                "sku": "",
                "price": price_text,
                "compare_at_price": "",
                "weight": "",
                "weight_unit": "lb",
                "inventory_qty": "0" if sold_out else "",
                "option_values": [],
            }],
            "tags": tags,
            "categories": [],
            "vendor": vendor,
            "product_type": "",
            "sold_out": sold_out,
            **_empty_sections(),
        })

    return products


def _enrich_product_from_html(session: requests.Session, product: dict) -> dict:
    """Fetch a product's detail page HTML and fill in missing fields.

    Extracts images from the gallery, body text from product-description
    and ProductItem-additional sections, and structured sections.
    """
    url = product.get("url", "")
    if not url:
        return product

    html = _get_html(session, url)
    if not html:
        return product

    soup = BeautifulSoup(html, "html.parser")

    # --- Images from gallery data-image attributes -------------------------
    gallery_imgs = []
    for img in soup.select(".product-gallery img[data-image]"):
        src = img.get("data-image", "")
        if src:
            cleaned = clean_image_url(src)
            if cleaned not in gallery_imgs:
                gallery_imgs.append(cleaned)

    if gallery_imgs and len(gallery_imgs) > len(product.get("images", [])):
        product["images"] = gallery_imgs

    # --- Body / description from product-description + additional section --
    body_parts = []

    # Primary product description
    for desc_el in soup.select(".product-description"):
        html_content = desc_el.decode_contents()
        if html_content.strip():
            body_parts.append(html_content.strip())
            break

    # Additional content area (often has ingredients, benefits, etc.)
    additional = soup.select_one("section.ProductItem-additional")
    if additional:
        additional_html = additional.decode_contents()
        if additional_html.strip():
            body_parts.append(additional_html.strip())

    if body_parts:
        combined_body = "\n".join(body_parts)
        if len(combined_body) > len(product.get("body_html", "")):
            product["body_html"] = combined_body
            clean_soup = BeautifulSoup(combined_body, "html.parser")
            product["description"] = clean_soup.get_text(separator="\n", strip=True)

        # Parse structured sections from the combined body
        sections = _parse_sections_from_html(combined_body)
        for key in sections:
            if sections[key] and not product.get(key):
                product[key] = sections[key]

    # --- Price from page (fallback) ----------------------------------------
    price_el = soup.select_one(".product-price-value")
    if price_el and not product["variants"][0].get("price"):
        price_text = price_el.get_text(strip=True)
        price_text = price_text.replace("$", "").replace(",", "").strip()
        if price_text:
            product["variants"][0]["price"] = price_text

    # --- Sold-out from page ------------------------------------------------
    add_btn = soup.select_one(".sqs-add-to-cart-button")
    if add_btn:
        btn_text = add_btn.get_text(strip=True).lower()
        if "sold out" in btn_text:
            product["sold_out"] = True
            product["variants"][0]["inventory_qty"] = "0"

    # --- Tags from product-detail classes ----------------------------------
    detail_el = soup.select_one(".product-detail[class]")
    if detail_el:
        css_classes = detail_el.get("class", [])
        html_tags = [c.replace("tag-", "") for c in css_classes if c.startswith("tag-")]
        if html_tags and not product.get("tags"):
            product["tags"] = html_tags

    return product


def scrape_products(base_url: str, shop_path: str = "/shop") -> list[dict]:
    """Scrape all products from a Squarespace store.

    Parameters
    ----------
    base_url : str
        The root URL of the Squarespace site, e.g. ``https://www.example.com``.
    shop_path : str
        The path to the shop/products collection page, e.g. ``/shop``.

    Returns
    -------
    list[dict]
        A list of product dicts ready for CSV export.
    """
    base_url = base_url.rstrip("/")
    session = requests.Session()

    # Reset the rate limiter for each scrape run
    global _limiter
    _limiter = _RateLimiter()

    products = []
    page = 1
    seen_ids = set()
    json_worked = False

    # ----- Paginate through the collection via JSON API --------------------
    while True:
        collection_url = f"{base_url}{shop_path}?page={page}"
        data = _get_json(session, collection_url)

        if not data:
            # Try alternate paths on first page
            if page == 1 and shop_path == "/shop":
                for alt_path in ["/products", "/store", "/all-products"]:
                    data = _get_json(session, f"{base_url}{alt_path}?page=1")
                    if data and data.get("items"):
                        shop_path = alt_path
                        break
            if not data:
                break

        items = data.get("items", [])
        if not items:
            break

        json_worked = True

        new_on_page = 0
        for item in items:
            item_id = item.get("id", "")
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            new_on_page += 1

            product = _process_product(session, base_url, item)
            if product:
                products.append(product)
                logger.info("Scraped product: %s", product["title"])

            _limiter.wait()

        # Stop if every item on this page was already seen (looping)
        if new_on_page == 0:
            logger.info("No new products on page %d, stopping pagination.", page)
            break

        # Check for more pages
        pagination = data.get("pagination", {})
        next_page = pagination.get("nextPage")
        if next_page:
            # nextPage can be a page number or boolean
            page = int(next_page) if isinstance(next_page, (int, float)) else page + 1
        elif len(items) >= 20:
            # Some themes don't return pagination info
            page += 1
        else:
            break

    # ----- HTML fallback if JSON API returned nothing ----------------------
    if not json_worked:
        logger.info("JSON API returned no items, falling back to HTML scraping")
        listing_html = _get_html(session, f"{base_url}{shop_path}")
        if listing_html:
            html_products = _scrape_products_from_html(listing_html, base_url)
            for p in html_products:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    # Enrich with detail page data
                    p = _enrich_product_from_html(session, p)
                    products.append(p)
                    logger.info("Scraped product (HTML): %s", p["title"])
                    _limiter.wait()

            # Only crawl sub-categories when using HTML fallback, since
            # the JSON API's "All" collection already returns every product.
            cat_paths = _scrape_category_paths_from_html(listing_html)
            for cat_path in cat_paths:
                if cat_path.rstrip("/") == shop_path.rstrip("/"):
                    continue

                cat_url = f"{base_url}{cat_path}"
                cat_data = _get_json(session, cat_url)
                if cat_data and cat_data.get("items"):
                    for item in cat_data["items"]:
                        item_id = item.get("id", "")
                        if item_id in seen_ids:
                            continue
                        seen_ids.add(item_id)
                        product = _process_product(session, base_url, item)
                        if product:
                            products.append(product)
                            logger.info("Scraped product (category %s): %s", cat_path, product["title"])
                        _limiter.wait()
                else:
                    cat_html = _get_html(session, cat_url)
                    if cat_html:
                        html_products = _scrape_products_from_html(cat_html, base_url)
                        for p in html_products:
                            if p["id"] not in seen_ids:
                                seen_ids.add(p["id"])
                                p = _enrich_product_from_html(session, p)
                                products.append(p)
                                logger.info("Scraped product (HTML cat %s): %s", cat_path, p["title"])
                                _limiter.wait()

                _limiter.wait()

    logger.info("Scraped %d products total.", len(products))
    return products, session


def _normalise_price(raw_price: str, currency: str = "") -> str:
    """Convert a raw price string to a dollar-formatted string.

    Squarespace stores prices inconsistently: some sites use cents
    (integer like 2600 for $26), others use dollars (26 or 26.0).
    We use heuristics to detect which format is in play:
      - If the value has a decimal with fractional cents (e.g. 26.00),
        it's already dollars.
      - If currency context is present AND the value is a large integer
        (>= 100), it's likely cents.
      - Small integers with currency context are ambiguous; we treat
        values < 100 as dollars to avoid turning $26 into $0.26.
    """
    if not raw_price:
        return raw_price
    try:
        price_val = float(raw_price)
        if price_val == 0:
            return "0.00"

        # If it already looks like a dollar amount (has decimal places
        # in the original string like "26.00" or "65.50"), keep as-is.
        if "." in raw_price:
            return f"{price_val:.2f}"

        # Integer value with currency context: decide cents vs dollars.
        # Squarespace cents are always whole numbers >= 100 (i.e. $1.00+).
        # Values < 100 with no decimal are ambiguous but more likely dollars
        # for real product prices.
        if currency and price_val >= 100 and price_val == int(price_val):
            return f"{price_val / 100:.2f}"

        # No currency context: only convert very large round numbers
        if not currency and price_val >= 1000 and price_val == int(price_val):
            return f"{price_val / 100:.2f}"

        # Otherwise treat as dollars
        return f"{price_val:.2f}" if price_val != int(price_val) else raw_price
    except (ValueError, TypeError):
        return raw_price


def _process_product(session: requests.Session, base_url: str, item: dict) -> dict | None:
    """Build a normalised product dict from a Squarespace item."""
    try:
        raw_title = item.get("title", "")
        title, vendor = _extract_vendor_from_title(raw_title)

        full_url = item.get("fullUrl", "")
        slug = item.get("urlId", "") or full_url.rstrip("/").split("/")[-1]

        # fullUrl has the correct path (e.g. /shop-skincare/p/slug)
        if full_url:
            product_url = f"{base_url}{full_url}" if full_url.startswith("/") else f"{base_url}/{full_url}"
        elif slug:
            product_url = f"{base_url}/{slug}"
        else:
            product_url = ""

        # ---- Body / description -------------------------------------------
        body_html = item.get("body", "") or ""
        excerpt = item.get("excerpt", "") or ""
        soup = BeautifulSoup(body_html, "html.parser") if body_html else None
        description = soup.get_text(separator="\n", strip=True) if soup else excerpt

        # ---- Images (full-size originals) ---------------------------------
        images = []
        # systemDataId based CDN images
        if item.get("assetUrl"):
            images.append(clean_image_url(item["assetUrl"]))

        for img in item.get("items", []):
            asset = img.get("assetUrl", "")
            if asset and clean_image_url(asset) not in images:
                images.append(clean_image_url(asset))

        # Images from the main image list
        for img_data in item.get("images", []):
            asset = img_data.get("assetUrl", "") or img_data.get("url", "")
            if asset and clean_image_url(asset) not in images:
                images.append(clean_image_url(asset))

        # ---- Variants & pricing -------------------------------------------
        variants = []
        for v in item.get("variants", []):
            # Determine price – prefer priceMoney (always cents)
            price_money = v.get("priceMoney", {})
            currency = price_money.get("currency", "")
            raw_price = str(price_money.get("value", "")) if price_money.get("value") is not None else ""

            if not raw_price:
                p = v.get("price")
                if isinstance(p, dict):
                    currency = p.get("currency", currency)
                    raw_price = str(p.get("value", ""))
                elif p is not None:
                    raw_price = str(p)

            variant = {
                "sku": v.get("sku", ""),
                "price": _normalise_price(raw_price, currency),
                "compare_at_price": "",
                "weight": v.get("weight", ""),
                "weight_unit": v.get("weightUnit", "POUNDS"),
                "inventory_qty": v.get("qtyInStock", ""),
                "option_values": [],
            }

            sale_money = v.get("salePriceMoney", {})
            sale_price_raw = str(sale_money.get("value", "")) if sale_money.get("value") is not None else ""
            sale_currency = sale_money.get("currency", currency)
            if sale_price_raw:
                sale_price = _normalise_price(sale_price_raw, sale_currency)
                if sale_price:
                    variant["compare_at_price"] = variant["price"]
                    variant["price"] = sale_price

            for attr in v.get("attributes", {}).items():
                variant["option_values"].append(attr)
            for opt in v.get("optionValues", []):
                variant["option_values"].append(
                    (opt.get("optionName", ""), opt.get("value", ""))
                )

            # Variant-specific image
            img_id = v.get("mainImageId", "")
            if img_id:
                for img_data in item.get("images", []):
                    if img_data.get("id") == img_id:
                        variant["image"] = clean_image_url(
                            img_data.get("assetUrl", "") or img_data.get("url", "")
                        )
                        break

            variants.append(variant)

        # If no variants, create a default one
        if not variants:
            price_money = item.get("priceMoney") or {}
            currency = ""
            if isinstance(price_money, dict):
                currency = price_money.get("currency", "")
                raw_price = str(price_money.get("value", ""))
            else:
                raw_price = ""

            if not raw_price:
                p = item.get("price")
                if isinstance(p, dict):
                    currency = p.get("currency", currency)
                    raw_price = str(p.get("value", ""))
                elif p is not None:
                    raw_price = str(p)

            price_str = _normalise_price(raw_price, currency)

            variants = [
                {
                    "sku": item.get("sku", ""),
                    "price": price_str,
                    "compare_at_price": "",
                    "weight": "",
                    "weight_unit": "lb",
                    "inventory_qty": "",
                    "option_values": [],
                }
            ]

        # ---- Sold-out detection -------------------------------------------
        sold_out = False
        # Check inventory across variants
        total_qty = 0
        has_qty = False
        for v_data in item.get("variants", []):
            qty = v_data.get("qtyInStock")
            if qty is not None:
                has_qty = True
                total_qty += int(qty) if isinstance(qty, (int, float)) else 0
        if has_qty and total_qty == 0:
            sold_out = True

        # Also check item-level flags
        if item.get("isAvailable") is False or item.get("isSoldOut") is True:
            sold_out = True

        # ---- Structured sections from body HTML ---------------------------
        sections = _parse_sections_from_html(body_html)

        # Also try the JSON-level additional info
        json_sections = _extract_additional_info(item)
        for key in sections:
            if not sections[key] and json_sections.get(key):
                sections[key] = json_sections[key]

        # ---- Fetch individual product page for more detail ----------------
        if product_url:
            detail_data = _get_json(session, product_url)
            if detail_data:
                detail_item = detail_data.get("item", detail_data)
                detail_body = detail_item.get("body", "")
                if detail_body and len(detail_body) > len(body_html):
                    body_html = detail_body
                    soup = BeautifulSoup(body_html, "html.parser")
                    description = soup.get_text(separator="\n", strip=True)
                    page_sections = _parse_sections_from_html(body_html)
                    for key in sections:
                        if not sections[key] and page_sections.get(key):
                            sections[key] = page_sections[key]

                # Pick up any extra images from detail
                for img_data in detail_item.get("images", []):
                    asset = img_data.get("assetUrl", "") or img_data.get("url", "")
                    if asset and clean_image_url(asset) not in images:
                        images.append(clean_image_url(asset))

                detail_json_sections = _extract_additional_info(detail_item)
                for key in sections:
                    if not sections[key] and detail_json_sections.get(key):
                        sections[key] = detail_json_sections[key]

            # Always fetch the HTML page to extract JSON-LD (reliable
            # price/SKU/availability) and to enrich body/images when
            # the JSON API returned limited data.
            html = _get_html(session, product_url)
            if html:
                # --- JSON-LD: authoritative price, SKU, availability ---
                ld = _extract_jsonld(html)
                if ld.get("price"):
                    # JSON-LD price is always in dollars – override the
                    # potentially-wrong cents-vs-dollars value from the API.
                    ld_price = ld["price"]
                    for v in variants:
                        # Only override if the current price looks wrong
                        # (e.g. $0.26 when JSON-LD says $26.00)
                        try:
                            api_price = float(v.get("price") or 0)
                            jsonld_price = float(ld_price)
                            # If they differ by ~100x, the API value was in cents
                            if api_price > 0 and jsonld_price > 0:
                                ratio = jsonld_price / api_price
                                if ratio > 50 or ratio < 0.02:
                                    v["price"] = f"{jsonld_price:.2f}"
                            elif api_price == 0 and jsonld_price > 0:
                                v["price"] = f"{jsonld_price:.2f}"
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
                if ld.get("sku") and variants and not variants[0].get("sku"):
                    variants[0]["sku"] = ld["sku"]
                if ld.get("in_stock") is False:
                    sold_out = True

                # --- Enrich body/images from HTML if JSON was sparse ---
                if not detail_data:
                    stub = {
                        "url": product_url,
                        "body_html": body_html,
                        "images": images,
                        "variants": variants,
                        "tags": [],
                    }
                    stub.update(sections)
                    enriched = _enrich_product_from_html(session, stub)
                    if len(enriched.get("body_html", "")) > len(body_html):
                        body_html = enriched["body_html"]
                        description = enriched.get("description", description)
                    if len(enriched.get("images", [])) > len(images):
                        images = enriched["images"]
                    for key in sections:
                        if not sections[key] and enriched.get(key):
                            sections[key] = enriched[key]

        # ---- Tags ---------------------------------------------------------
        tags = item.get("tags", []) or []
        categories = item.get("categories", []) or []

        # ---- Product type from categories ---------------------------------
        product_type = ""
        if categories:
            product_type = categories[0]

        product = {
            "id": item.get("id", ""),
            "title": title,
            "handle": slug,
            "url": product_url,
            "body_html": body_html,
            "description": description,
            "images": images,
            "variants": variants,
            "tags": tags,
            "categories": categories,
            "vendor": vendor,
            "product_type": product_type,
            "sold_out": sold_out,
            "ingredients": sections.get("ingredients", ""),
            "how_to_use": sections.get("how_to_use", ""),
            "benefits": sections.get("benefits", ""),
            "what_it_is": sections.get("what_it_is", ""),
            "who_its_for": sections.get("who_its_for", ""),
        }

        return product

    except Exception as exc:
        logger.error("Error processing product %s: %s", item.get("title", "?"), exc)
        return None


def scrape_reviews(
    base_url: str,
    products: list[dict],
    session: requests.Session | None = None,
) -> list[dict]:
    """Scrape product reviews from a Squarespace store.

    Strategies (tried in order):
    1. Squarespace review API — query the commerce reviews endpoint
       using the same session (with cookies) from product scraping.
    2. HTML fallback — parse any server-rendered reviews from a product
       page.

    Parameters
    ----------
    session : optional
        Re-use the session from ``scrape_products`` so cookies and
        rate-limit state carry over.

    Returns a flat list of review dicts.
    """
    base_url = base_url.rstrip("/")
    if session is None:
        session = requests.Session()
    all_reviews = []
    seen_reviews = set()

    handle_titles = {}
    for p in products:
        handle_titles[p.get("handle", "")] = p.get("title", "")

    def _add_reviews(reviews):
        for r in reviews:
            key = (r.get("product_handle", ""), r.get("author", ""), r.get("body", "")[:50])
            if key not in seen_reviews:
                seen_reviews.add(key)
                all_reviews.append(r)

    # Cool down after product scraping to let rate limits reset
    _limiter.cooldown(20)

    # ------------------------------------------------------------------
    # Method 1: Squarespace Review API
    # ------------------------------------------------------------------
    # The JS `ProductReviewsController` fetches reviews via an internal
    # API.  We try several known endpoint patterns, both store-wide and
    # per-product.  Logging is verbose so we can diagnose on first deploy.
    api_headers = {**DEFAULT_HEADERS, "Accept": "application/json"}
    api_reviews = []

    # 1a. Store-wide endpoints (gets ALL reviews at once)
    store_wide_paths = [
        "/api/commerce/reviews/published",
        "/api/commerce/reviews?status=PUBLISHED",
        "/api/commerce/reviews",
    ]
    for api_path in store_wide_paths:
        api_url = f"{base_url}{api_path}"
        try:
            resp = session.get(api_url, headers=api_headers, timeout=15)
            logger.info(
                "Review API [store-wide] %s → %d (%d bytes)",
                api_path, resp.status_code, len(resp.content),
            )
            if resp.status_code == 429:
                _limiter.on_rate_limit(
                    float(resp.headers.get("Retry-After", 0))
                )
                _limiter.wait()
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    logger.warning("Review API returned non-JSON for %s", api_path)
                    continue
                review_list = (
                    data if isinstance(data, list)
                    else data.get("reviews", data.get("items", []))
                )
                logger.info("  → parsed %d review(s)", len(review_list))
                for r in review_list:
                    api_reviews.append(_normalise_review(r, "", ""))
                if api_reviews:
                    break
        except Exception as exc:
            logger.warning("Review API error %s: %s", api_path, exc)
        _limiter.wait()

    # 1b. Per-product endpoint (try first product only as a probe)
    if not api_reviews and products:
        probe = products[0]
        product_id = probe.get("id", "")
        per_product_paths = [
            f"/api/commerce/reviews/published?productId={product_id}",
            f"/api/commerce/reviews?productId={product_id}&status=PUBLISHED",
            f"/api/commerce/reviews?productId={product_id}",
        ]
        for api_path in per_product_paths:
            api_url = f"{base_url}{api_path}"
            try:
                resp = session.get(api_url, headers=api_headers, timeout=15)
                logger.info(
                    "Review API [per-product] %s → %d (%d bytes)",
                    api_path.split("?")[0], resp.status_code,
                    len(resp.content),
                )
                if resp.status_code == 429:
                    _limiter.on_rate_limit(
                        float(resp.headers.get("Retry-After", 0))
                    )
                    _limiter.wait()
                    continue
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        logger.warning("Review API returned non-JSON for %s", api_path)
                        continue
                    review_list = (
                        data if isinstance(data, list)
                        else data.get("reviews", data.get("items", []))
                    )
                    logger.info("  → parsed %d review(s)", len(review_list))
                    if review_list:
                        # This endpoint works — now fetch all products
                        for r in review_list:
                            api_reviews.append(
                                _normalise_review(r, probe["title"], probe["handle"])
                            )
                        # Fetch remaining products
                        for product in products[1:]:
                            pid = product.get("id", "")
                            url = f"{base_url}{api_path.split('?')[0]}?productId={pid}"
                            _limiter.wait()
                            try:
                                r2 = session.get(url, headers=api_headers, timeout=15)
                                if r2.status_code == 200:
                                    d2 = r2.json()
                                    rl2 = (
                                        d2 if isinstance(d2, list)
                                        else d2.get("reviews", d2.get("items", []))
                                    )
                                    for rv in rl2:
                                        api_reviews.append(
                                            _normalise_review(
                                                rv, product["title"], product["handle"]
                                            )
                                        )
                                elif r2.status_code == 429:
                                    _limiter.on_rate_limit(
                                        float(r2.headers.get("Retry-After", 0))
                                    )
                            except Exception:
                                pass
                        break
            except Exception as exc:
                logger.warning("Review API error %s: %s", api_path, exc)
            _limiter.wait()

    if api_reviews:
        # Enrich with titles from handle lookup
        for r in api_reviews:
            handle = r.get("product_handle", "")
            if handle and handle in handle_titles and not r.get("product_title"):
                r["product_title"] = handle_titles[handle]
        _add_reviews(api_reviews)
        logger.info("Review API returned %d reviews total", len(api_reviews))

    # ------------------------------------------------------------------
    # Method 2: HTML fallback — parse server-rendered reviews
    # ------------------------------------------------------------------
    if not all_reviews:
        logger.info("Review API found nothing, trying HTML scrape")
        products_with_urls = [p for p in products if p.get("url")]
        for probe_product in products_with_urls[:3]:
            probe_url = probe_product["url"]
            logger.info("Fetching reviews from product page: %s", probe_url)
            html = _get_html(session, probe_url)
            if not html:
                logger.warning("Could not fetch %s for reviews", probe_url)
                _limiter.wait()
                continue

            has_container = "reviewsContainer" in html or "reviewDetails" in html
            logger.info(
                "HTML page %s: %d bytes, review container: %s",
                probe_url, len(html), has_container,
            )

            if not has_container:
                _limiter.wait()
                continue

            reviews = _scrape_reviews_from_html(html, "", "")
            if reviews:
                for r in reviews:
                    handle = r.get("product_handle", "")
                    if handle and handle in handle_titles:
                        r["product_title"] = handle_titles[handle]
                _add_reviews(reviews)
                logger.info("Found %d reviews from HTML on %s", len(reviews), probe_url)
                break
            else:
                logger.info("Review container found but 0 reviews parsed (JS-rendered)")
            _limiter.wait()

    logger.info("Scraped %d reviews total.", len(all_reviews))
    return all_reviews


def _normalise_review(review_data: dict, product_title: str, product_handle: str) -> dict:
    """Convert a raw Squarespace review object to a normalised dict."""
    return {
        "product_title": product_title,
        "product_handle": product_handle,
        "rating": review_data.get("rating", review_data.get("value", "")),
        "author": review_data.get("authorName", "")
        or review_data.get("author", {}).get("displayName", "")
        or review_data.get("reviewer", ""),
        "email": review_data.get("authorEmail", "")
        or review_data.get("author", {}).get("email", ""),
        "title": review_data.get("title", ""),
        "body": review_data.get("body", "") or review_data.get("content", ""),
        "created_at": review_data.get("addedOn", "")
        or review_data.get("createdOn", "")
        or review_data.get("date", ""),
    }


def _scrape_reviews_from_html(html: str, product_title: str, product_handle: str) -> list[dict]:
    """Extract reviews from rendered product/review page HTML.

    Supports two strategies:
    1. Squarespace native reviews (data-testid attributes like reviewName,
       reviewStars, reviewDesc, review-date, review-product-name)
    2. Generic fallback for third-party review widgets
    """
    reviews = []
    soup = BeautifulSoup(html, "html.parser")

    # ---- Strategy 1: Squarespace native reviews (data-testid) ----
    review_blocks = soup.select("div.reviewDetails")
    if review_blocks:
        for block in review_blocks:
            # Author
            author_el = block.select_one('[data-testid="reviewer-name"]')
            author = author_el.get_text(strip=True) if author_el else ""

            # Date
            date_el = block.select_one('[data-testid="review-date"]')
            date = date_el.get_text(strip=True) if date_el else ""

            # Rating – from sr-only label like "5.00 out of 5 stars"
            rating = ""
            stars_el = block.select_one('[data-testid="review-stars"]')
            if stars_el:
                sr_label = stars_el.select_one("label.sr-only")
                if sr_label:
                    match = re.search(r"([\d.]+)\s+out\s+of", sr_label.get_text())
                    if match:
                        rating = match.group(1)
                # Fallback: count SVG star elements
                if not rating:
                    star_svgs = stars_el.find_all("svg", class_="star")
                    if star_svgs:
                        rating = str(len(star_svgs))

            # Body / description
            desc_el = block.select_one('[data-testid="review-desc"]')
            body = desc_el.get_text(strip=True) if desc_el else ""

            # Product link – reviews may be cross-product
            review_handle = product_handle
            review_title = product_title
            product_link = block.select_one("a.productLink")
            if product_link:
                href = product_link.get("href", "")
                if href:
                    review_handle = href.rstrip("/").split("/")[-1]
                link_text = product_link.get_text(strip=True)
                if link_text:
                    review_title = link_text

            if body or rating:
                reviews.append({
                    "product_title": review_title,
                    "product_handle": review_handle,
                    "rating": rating,
                    "author": author,
                    "email": "",
                    "title": "",
                    "body": body,
                    "created_at": date,
                })

        return reviews

    # ---- Strategy 2: Generic fallback ----
    review_containers = soup.find_all(
        class_=re.compile(r"review(?!-rating)|testimonial", re.I)
    )

    for container in review_containers:
        # Skip summary/count/stars-only elements
        classes_str = " ".join(container.get("class", []))
        if re.search(r"count|summary|overall|header|container|wrapper", classes_str, re.I):
            continue

        rating_el = container.find(class_=re.compile(r"rating|stars?", re.I))
        rating = ""
        if rating_el:
            # Check for sr-only label first
            sr_label = rating_el.find("label", class_="sr-only")
            if sr_label:
                match = re.search(r"([\d.]+)\s+out\s+of", sr_label.get_text())
                if match:
                    rating = match.group(1)

            if not rating:
                rating_text = rating_el.get_text(strip=True)
                match = re.search(r"(\d(?:\.\d)?)", rating_text)
                if match:
                    rating = match.group(1)

            if not rating:
                # Count SVG stars or filled star elements
                star_svgs = rating_el.find_all("svg", class_="star")
                if star_svgs:
                    rating = str(len(star_svgs))
                else:
                    stars = rating_el.find_all(
                        class_=re.compile(r"filled|active|full|solid", re.I)
                    )
                    if stars:
                        rating = str(len(stars))

        author_el = container.find(class_=re.compile(r"author|reviewer|name", re.I))
        author = author_el.get_text(strip=True) if author_el else ""

        body_el = container.find(
            class_=re.compile(r"body|content|text|comment|desc", re.I)
        )
        body = body_el.get_text(strip=True) if body_el else ""

        date_el = container.find(class_=re.compile(r"date|time|created", re.I))
        date = date_el.get_text(strip=True) if date_el else ""

        if body or rating:
            reviews.append({
                "product_title": product_title,
                "product_handle": product_handle,
                "rating": rating,
                "author": author,
                "email": "",
                "title": "",
                "body": body,
                "created_at": date,
            })

    return reviews
