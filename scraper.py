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
_found_page_reviews = False

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


def _extract_crumb(html: str) -> str:
    """Extract the Squarespace crumb (CSRF) token from a page.

    The crumb is used by Squarespace's JS controllers to authenticate
    internal API requests.  It appears in a meta tag or in the page's
    inline JavaScript context object.
    """
    # <meta> tag: <meta name="csrf-token" content="...">
    meta_match = re.search(
        r'<meta\s[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)',
        html,
    )
    if meta_match:
        return meta_match.group(1)

    # Squarespace context: "crumb":"..."
    crumb_match = re.search(r'"crumb"\s*:\s*"([^"]+)"', html)
    if crumb_match:
        return crumb_match.group(1)

    # Cookie-based crumb in Set-Cookie or inline script
    crumb_match2 = re.search(r'crumb=([A-Za-z0-9_-]+)', html)
    if crumb_match2:
        return crumb_match2.group(1)

    return ""


def _format_review_date(date_val) -> str:
    """Convert a Squarespace review date to Judge.me format.

    The API returns ``reviewDate`` as epoch milliseconds (e.g. 1734278400000)
    or an ISO string.  Judge.me expects ``YYYY-MM-DD HH:mm:ss UTC``.
    """
    if not date_val:
        return ""
    from datetime import datetime, timezone

    # Epoch milliseconds (large integer or numeric string)
    try:
        ts = float(date_val)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, OSError):
        pass

    # ISO string or human-readable
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%b %d, %Y",  # "Dec 15, 2025"
    ]:
        try:
            dt = datetime.strptime(str(date_val), fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            continue

    return str(date_val)


def _normalise_rating(val) -> str:
    """Ensure rating is an integer 1-5 as a string."""
    if not val and val != 0:
        return ""
    try:
        n = float(val)
        if n < 1 or n > 5:
            return str(val)
        return str(round(n))
    except (ValueError, TypeError):
        return str(val)


def _extract_website_id(html: str) -> str:
    """Extract the Squarespace websiteId from page HTML.

    The websiteId is needed for the ``/api/commerce/product/reviews`` API.
    It appears in the Squarespace context JSON, controller data attributes,
    or inline scripts.
    """
    # "websiteId":"<uuid>"
    m = re.search(r'"websiteId"\s*:\s*"([a-f0-9-]+)"', html)
    if m:
        return m.group(1)
    # data-website-id="<uuid>"
    m = re.search(r'data-website-id=["\']([a-f0-9-]+)', html)
    if m:
        return m.group(1)
    return ""


def _fetch_reviews_via_api(
    session: requests.Session,
    base_url: str,
    website_id: str,
    crumb: str,
    product_id: str,
    product_title: str,
    product_handle: str,
) -> list[dict]:
    """Fetch reviews for a product using the internal Squarespace reviews API.

    Endpoint: /api/commerce/product/reviews?websiteId=X&productId=Y&page=N&size=50

    This is the same API that Squarespace's ProductReviewsController calls
    client-side.  It returns JSON with ``{productReviews: [...]}``.
    """
    reviews: list[dict] = []
    page_size = 50
    max_pages = 20
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }
    if crumb:
        headers["X-CSRF-Token"] = crumb

    for page_num in range(max_pages):
        url = (
            f"{base_url}/api/commerce/product/reviews"
            f"?websiteId={website_id}"
            f"&productId={product_id}"
            f"&page={page_num}"
            f"&size={page_size}"
        )
        try:
            resp = session.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.debug("Reviews API returned %d for product %s", resp.status_code, product_id)
                break
            data = resp.json()
            batch = data.get("productReviews", [])
            if not batch:
                break

            for r in batch:
                first_name = r.get("userFirstName", "")
                last_initial = (r.get("userLastName") or "")[:1]
                author = (first_name + (" " + last_initial if last_initial else "")).strip()
                if not author:
                    author = r.get("userLoginName", "")

                reviews.append({
                    "product_title": product_title,
                    "product_handle": product_handle,
                    "rating": _normalise_rating(r.get("starRating")),
                    "author": author,
                    "email": "",
                    "title": "",  # Squarespace reviews have no title/headline
                    "body": r.get("text", ""),
                    "created_at": _format_review_date(r.get("reviewDate")),
                })

            if len(batch) < page_size:
                break
        except Exception as exc:
            logger.debug("Reviews API error for product %s page %d: %s", product_id, page_num, exc)
            break

    return reviews


def _fetch_store_reviews_via_api(
    session: requests.Session,
    base_url: str,
    website_id: str,
    crumb: str,
    products: list[dict],
) -> list[dict]:
    """Fetch all store reviews (not product-specific) via the internal API.

    Endpoint: /api/commerce/product/reviews?websiteId=X&page=N&size=50
    (no productId = all store reviews)
    """
    reviews: list[dict] = []
    page_size = 50
    max_pages = 20
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }
    if crumb:
        headers["X-CSRF-Token"] = crumb

    # Build a lookup for matching reviews to products
    handle_by_url = {}
    title_by_handle = {}
    for p in products:
        h = p.get("handle", "")
        t = p.get("title", "")
        u = p.get("url", "")
        if h:
            title_by_handle[h] = t
        if u and h:
            handle_by_url[u] = h

    for page_num in range(max_pages):
        url = (
            f"{base_url}/api/commerce/product/reviews"
            f"?websiteId={website_id}"
            f"&page={page_num}"
            f"&size={page_size}"
        )
        try:
            resp = session.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            batch = data.get("productReviews", [])
            if not batch:
                break

            for r in batch:
                first_name = r.get("userFirstName", "")
                last_initial = (r.get("userLastName") or "")[:1]
                author = (first_name + (" " + last_initial if last_initial else "")).strip()
                if not author:
                    author = r.get("userLoginName", "")

                # Try to match to a product
                product_url = r.get("productURL", "")
                matched_handle = ""
                matched_title = r.get("productName", "")
                for h, t in title_by_handle.items():
                    if product_url and h in product_url:
                        matched_handle = h
                        matched_title = t
                        break
                    if matched_title and t.lower() == matched_title.lower():
                        matched_handle = h
                        break

                reviews.append({
                    "product_title": matched_title,
                    "product_handle": matched_handle,
                    "rating": _normalise_rating(r.get("starRating")),
                    "author": author,
                    "email": "",
                    "title": "",
                    "body": r.get("text", ""),
                    "created_at": _format_review_date(r.get("reviewDate")),
                })

            if len(batch) < page_size:
                break
        except Exception as exc:
            logger.debug("Store reviews API error page %d: %s", page_num, exc)
            break

    return reviews


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


def scrape_products(base_url: str, shop_path: str = "/shop", max_products: int = 0) -> list[dict]:
    """Scrape all products from a Squarespace store.

    Parameters
    ----------
    base_url : str
        The root URL of the Squarespace site, e.g. ``https://www.example.com``.
    shop_path : str
        The path to the shop/products collection page, e.g. ``/shop``.
    max_products : int
        If > 0, stop after scraping this many products (test mode).

    Returns
    -------
    list[dict]
        A list of product dicts ready for CSV export.
    """
    base_url = base_url.rstrip("/")
    session = requests.Session()

    # Reset the rate limiter and review flag for each scrape run
    global _limiter, _found_page_reviews
    _limiter = _RateLimiter()
    _found_page_reviews = False

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

        # Extract the collection path from the JSON response — this gives
        # us the correct base for product URLs (e.g. /shop-skincare)
        collection_data = data.get("collection", {})
        collection_url_path = collection_data.get("fullUrl", "") or shop_path
        if page == 1:
            item_keys = list(items[0].keys()) if items else []
            logger.info("JSON API item keys: %s", item_keys)
            logger.info("Collection data keys: %s", list(collection_data.keys()))
            logger.info("Collection fullUrl: %r", collection_data.get("fullUrl", ""))
            if items:
                first = items[0]
                logger.info("First item URL fields: fullUrl=%r, urlId=%r, url=%r",
                            first.get("fullUrl", ""), first.get("urlId", ""),
                            first.get("url", ""))
            logger.info("Using collection_url_path=%r for product URLs", collection_url_path)

        new_on_page = 0
        for item in items:
            item_id = item.get("id", "")
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            new_on_page += 1

            product = _process_product(session, base_url, item, collection_url_path)
            if product:
                products.append(product)
                logger.info("Scraped product: %s (url=%s)", product["title"], product.get("url", ""))

            _limiter.wait()

            if max_products and len(products) >= max_products:
                break

        # Stop if hit product limit
        if max_products and len(products) >= max_products:
            logger.info("Test mode: stopping after %d product(s).", len(products))
            break

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
                        product = _process_product(session, base_url, item, cat_path)
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


def _process_product(session: requests.Session, base_url: str, item: dict, shop_path: str = "/shop") -> dict | None:
    """Build a normalised product dict from a Squarespace item."""
    try:
        raw_title = item.get("title", "")
        title, vendor = _extract_vendor_from_title(raw_title)

        full_url = item.get("fullUrl", "")
        slug = item.get("urlId", "") or full_url.rstrip("/").split("/")[-1]

        # Build the product page URL.
        # fullUrl (when present) has the correct path (e.g. /shop-skincare/p/slug).
        # When missing, construct it from the collection path + /p/ + slug.
        if full_url and "/p/" in full_url:
            product_url = f"{base_url}{full_url}" if full_url.startswith("/") else f"{base_url}/{full_url}"
        elif slug:
            # shop_path is the collection URL (e.g. /shop-skincare)
            product_url = f"{base_url}{shop_path}/p/{slug}"
        else:
            product_url = ""

        logger.info("Product URL for %r: %s (fullUrl=%r, slug=%r, shop_path=%r)", title, product_url, full_url, slug, shop_path)

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
        page_reviews = []
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
            logger.info("HTML fetch for %s: %s (%d bytes)",
                        slug, "OK" if html else "FAILED",
                        len(html) if html else 0)
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

                # --- Always extract sections from ProductItem-additional ---
                # The JSON body rarely includes the additional content area
                # (ingredients, how to use, benefits, etc.), so parse it
                # directly from the HTML page regardless of JSON detail.
                page_soup = BeautifulSoup(html, "html.parser")
                additional_el = page_soup.select_one(
                    "section.ProductItem-additional"
                )
                if additional_el:
                    additional_html = additional_el.decode_contents()
                    if additional_html.strip():
                        html_sections = _parse_sections_from_html(
                            additional_html
                        )
                        for key in html_sections:
                            if html_sections[key] and not sections.get(key):
                                sections[key] = html_sections[key]

                        # Also merge additional body into body_html
                        if len(additional_html) > 50:
                            combined = body_html + "\n" + additional_html
                            if len(combined) > len(body_html):
                                body_html = combined
                                clean_soup = BeautifulSoup(
                                    combined, "html.parser"
                                )
                                description = clean_soup.get_text(
                                    separator="\n", strip=True
                                )

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

                # --- Grab reviews from this product page ---
                # Squarespace renders review containers empty and fills
                # them via JS. But the data is often embedded in <script>
                # tags as JSON. Try multiple extraction strategies.
                global _found_page_reviews
                if not _found_page_reviews:
                    page_reviews = _extract_reviews_from_page(
                        html, title, slug,
                    )
                    if page_reviews:
                        _found_page_reviews = True
                        logger.info(
                            "Found %d reviews on product page %s",
                            len(page_reviews), slug,
                        )

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
            "_page_reviews": page_reviews,
        }

        return product

    except Exception as exc:
        logger.error("Error processing product %s: %s", item.get("title", "?"), exc)
        return None


def _scrape_reviews_with_browser(
    products: list[dict],
    base_url: str,
) -> tuple[list[dict], list[dict]]:
    """Visit product pages with a headless browser to extract JS-rendered reviews."""
    reviews: list[dict] = []
    debug_log: list[dict] = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed — cannot render JS for reviews")
        debug_log.append({"label": "Playwright not installed", "error": "pip install playwright"})
        return reviews, debug_log

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=DEFAULT_HEADERS["User-Agent"])

            for product in products:
                url = product.get("url", "")
                title = product.get("title", "")
                handle = product.get("handle", "")
                if not url:
                    continue

                logger.info("Playwright: visiting %s for reviews", url)
                try:
                    page.goto(url, wait_until="networkidle", timeout=30_000)
                    # Extra wait for lazy-loaded review widgets
                    page.wait_for_timeout(3000)

                    html = page.content()
                    logger.info(
                        "Playwright: got %d bytes of rendered HTML for %s",
                        len(html), handle,
                    )

                    page_reviews = _extract_reviews_from_page(html, title, handle)
                    if page_reviews:
                        reviews.extend(page_reviews)
                        logger.info("Playwright: found %d reviews on %s", len(page_reviews), handle)
                        debug_log.append({
                            "label": f"Browser reviews: {title}",
                            "count": len(page_reviews),
                        })
                    else:
                        logger.info("Playwright: no reviews found on %s", handle)
                        debug_log.append({
                            "label": f"Browser: no reviews on {title}",
                            "note": "Page rendered but no review elements found",
                        })
                except Exception as exc:
                    logger.warning("Playwright: error on %s: %s", url, exc)
                    debug_log.append({
                        "label": f"Browser error: {title}",
                        "error": str(exc),
                    })

                _limiter.wait()

            browser.close()
    except Exception as exc:
        logger.error("Playwright browser launch failed: %s", exc)
        debug_log.append({"label": "Browser launch failed", "error": str(exc)})

    return reviews, debug_log


def scrape_reviews(
    base_url: str,
    products: list[dict],
    session: requests.Session | None = None,
) -> tuple[list[dict], list[dict]]:
    """Scrape product reviews from a Squarespace store.

    Strategies (tried in order):
    1. Static HTML — reviews already extracted during product scraping.
    2. Headless browser — render JS on each product page and extract
       reviews from the fully-rendered DOM.

    Parameters
    ----------
    session : optional
        Re-use the session from ``scrape_products`` so cookies and
        rate-limit state carry over.

    Returns
    -------
    (reviews, debug_log) — a flat list of review dicts, plus a list
    of debug entries showing every request/response during review
    scraping so the UI can display what happened.
    """
    base_url = base_url.rstrip("/")
    if session is None:
        session = requests.Session()
    all_reviews = []
    seen_reviews = set()
    debug_log: list[dict] = []

    def _dbg(label: str, **kwargs):
        """Append a debug entry."""
        entry = {"label": label}
        entry.update(kwargs)
        debug_log.append(entry)

    handle_titles = {}
    for p in products:
        handle_titles[p.get("handle", "")] = p.get("title", "")

    def _add_reviews(reviews):
        for r in reviews:
            key = (r.get("product_handle", ""), r.get("author", ""), r.get("body", "")[:50])
            if key not in seen_reviews:
                seen_reviews.add(key)
                all_reviews.append(r)

    # ------------------------------------------------------------------
    # Method 1: Call /api/commerce/product/reviews directly
    # This is the same API that Squarespace's ProductReviewsController
    # calls client-side.  No browser needed — just requires websiteId
    # and crumb token from the page HTML.
    # ------------------------------------------------------------------
    # Get websiteId and crumb from any product page (or the shop page)
    website_id = ""
    crumb = ""
    sample_url = f"{base_url}/shop"
    if products and products[0].get("url"):
        sample_url = products[0]["url"]
    try:
        resp = session.get(sample_url, timeout=15)
        if resp.ok:
            website_id = _extract_website_id(resp.text)
            crumb = _extract_crumb(resp.text)
            # Also check cookie jar for crumb
            if not crumb:
                crumb = session.cookies.get("crumb", "")
    except Exception as exc:
        logger.debug("Could not fetch page for websiteId/crumb: %s", exc)

    if website_id:
        logger.info("Reviews API: websiteId=%s, crumb=%s", website_id, "present" if crumb else "missing")
        _dbg("Reviews API", websiteId=website_id, crumb="present" if crumb else "missing")

        # Try per-product reviews first
        for p in products:
            pid = p.get("id", "")
            if not pid:
                continue
            api_reviews = _fetch_reviews_via_api(
                session, base_url, website_id, crumb,
                pid, p.get("title", ""), p.get("handle", ""),
            )
            if api_reviews:
                _add_reviews(api_reviews)
                _dbg(f"API reviews: {p.get('title', '?')}", count=len(api_reviews))
            _limiter.wait()

        # If no per-product reviews, try store-wide reviews
        if not all_reviews:
            logger.info("No per-product reviews via API, trying store-wide reviews...")
            store_reviews = _fetch_store_reviews_via_api(
                session, base_url, website_id, crumb, products,
            )
            if store_reviews:
                _add_reviews(store_reviews)
                _dbg("Store reviews via API", count=len(store_reviews))

        if all_reviews:
            logger.info("Reviews API: collected %d reviews total", len(all_reviews))
    else:
        logger.info("Could not find websiteId — skipping reviews API")
        _dbg("Reviews API skipped", note="websiteId not found")

    # ------------------------------------------------------------------
    # Method 2: Collect reviews already extracted during product scraping
    # (zero extra HTTP requests — we already had the HTML)
    # ------------------------------------------------------------------
    if not all_reviews:
        for p in products:
            page_reviews = p.pop("_page_reviews", [])
            if page_reviews:
                for r in page_reviews:
                    handle = r.get("product_handle", "")
                    if handle and handle in handle_titles:
                        r["product_title"] = handle_titles[handle]
                _add_reviews(page_reviews)
                _dbg(
                    f"Reviews from product page: {p.get('title', '?')}",
                    count=len(page_reviews),
                )

        if all_reviews:
            logger.info("Collected %d reviews from product page HTML", len(all_reviews))
        else:
            _dbg("No reviews found on product pages", note="Products had no server-rendered reviews")
            logger.info("No reviews found on product pages during product scraping")
    else:
        # Still pop _page_reviews to avoid leftover data
        for p in products:
            p.pop("_page_reviews", None)

    # ------------------------------------------------------------------
    # Method 3: If API and static HTML had no reviews, use a headless
    # browser to render JS and extract reviews.
    # ------------------------------------------------------------------
    if not all_reviews:
        _limiter.cooldown(20)
        browser_reviews, browser_debug = _scrape_reviews_with_browser(
            products, base_url,
        )
        debug_log.extend(browser_debug)
        _add_reviews(browser_reviews)

    # Sort reviews by product so they're grouped in the CSV
    all_reviews.sort(key=lambda r: (r.get("product_handle", ""), r.get("created_at", "")))

    logger.info("Scraped %d reviews total.", len(all_reviews))
    _dbg("Final result", total_reviews=len(all_reviews))
    return all_reviews, debug_log


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


def _extract_reviews_from_page(html: str, product_title: str, product_handle: str) -> list[dict]:
    """Extract reviews from a product page using every available strategy.

    Squarespace renders review containers empty and fills them via
    JavaScript (ProductReviewsController). The actual review data is
    often embedded in the page as JSON inside <script> tags. We try:

    1. Rendered HTML (div.reviewDetails) — works if server-rendered
    2. Embedded JSON in <script> tags — the JS controller's data source
    3. Static.SQUARESPACE_CONTEXT or similar config objects
    """
    # Strategy 1: server-rendered reviews (rare but check first)
    reviews = _scrape_reviews_from_html(html, product_title, product_handle)
    if reviews:
        logger.info("Strategy 1 (rendered HTML): %d reviews", len(reviews))
        return reviews

    # Strategy 2: find review data in <script> tags
    soup = BeautifulSoup(html, "html.parser")
    reviews = _extract_reviews_from_scripts(soup, product_title, product_handle)
    if reviews:
        logger.info("Strategy 2 (script JSON): %d reviews", len(reviews))
        return reviews

    # Strategy 3: log what script content IS on the page for debugging
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 100:
            continue
        # Log a preview of any script that mentions "review"
        if re.search(r"\breview", text, re.I):
            preview = re.sub(r"\s+", " ", text[:300]).strip()
            logger.info("Script tag with 'review' (%d chars): %s...", len(text), preview)

    logger.info("No reviews found via any strategy on page %s", product_handle)
    return []


def _extract_reviews_from_scripts(
    soup: BeautifulSoup, product_title: str, product_handle: str,
) -> list[dict]:
    """Search <script> tags for embedded review JSON data."""
    reviews = []

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 50:
            continue

        # Look for JSON objects/arrays containing review-like data
        # Squarespace embeds data in various ways:
        #   - Static.SQUARESPACE_CONTEXT = {...}
        #   - window.__INITIAL_STATE__ = {...}
        #   - Inline JSON with "reviews" key
        #   - ProductReviewsController config

        # Try to find JSON blobs with review data
        for pattern in [
            # {"reviews": [...]}  or  {"items": [..., {review}]}
            r'"reviews"\s*:\s*\[',
            r'"reviewItems"\s*:\s*\[',
            r'"storeReviews"\s*:\s*\[',
            # ProductReviewsController data
            r'"reviewDetails"',
            r'"authorName"',
            r'"reviewBody"',
        ]:
            if not re.search(pattern, text):
                continue

            logger.info("Found review-like data in script tag (pattern: %s, length: %d)",
                        pattern, len(text))

            # Try to extract the whole JSON object
            extracted = _try_parse_json_from_script(text)
            if extracted:
                found = _walk_json_for_reviews(extracted, product_title, product_handle)
                if found:
                    reviews.extend(found)
                    return reviews

            # Try to extract just the array after "reviews":
            for key in ["reviews", "reviewItems", "storeReviews", "items"]:
                array_match = re.search(
                    rf'"{key}"\s*:\s*(\[[\s\S]*?\])\s*[,}}]', text,
                )
                if array_match:
                    try:
                        arr = json.loads(array_match.group(1))
                        if isinstance(arr, list) and arr:
                            found = _normalise_review_list(arr, product_title, product_handle)
                            if found:
                                reviews.extend(found)
                                return reviews
                    except (json.JSONDecodeError, ValueError):
                        pass

    return reviews


def _try_parse_json_from_script(text: str) -> dict | list | None:
    """Try to extract a JSON object or array from a script tag's text."""
    # Try direct parse (pure JSON script tags)
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass

    # Try to find JSON after common assignment patterns
    for prefix_pattern in [
        r'(?:window\.)?\w+\s*=\s*',           # var = {...}
        r'Static\.SQUARESPACE_CONTEXT\s*=\s*',  # Squarespace context
        r'__INITIAL_STATE__\s*=\s*',             # React pattern
    ]:
        match = re.search(prefix_pattern + r'(\{[\s\S]+\})\s*;?\s*$', text)
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

    return None


def _walk_json_for_reviews(
    data, product_title: str, product_handle: str, depth: int = 0,
) -> list[dict]:
    """Recursively walk a JSON structure looking for review arrays."""
    if depth > 8:
        return []

    if isinstance(data, list):
        # Check if this looks like a list of reviews
        reviews = _normalise_review_list(data, product_title, product_handle)
        if reviews:
            return reviews

    if isinstance(data, dict):
        # Check known keys first
        for key in ["reviews", "reviewItems", "storeReviews", "items"]:
            if key in data and isinstance(data[key], list):
                reviews = _normalise_review_list(data[key], product_title, product_handle)
                if reviews:
                    return reviews

        # Recurse into values
        for val in data.values():
            if isinstance(val, (dict, list)):
                reviews = _walk_json_for_reviews(val, product_title, product_handle, depth + 1)
                if reviews:
                    return reviews

    return []


def _normalise_review_list(
    items: list, product_title: str, product_handle: str,
) -> list[dict]:
    """Try to normalise a list of dicts as reviews. Returns [] if they
    don't look like reviews."""
    reviews = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Must have at least a body/content or rating to count as a review
        body = (item.get("body") or item.get("content") or
                item.get("reviewBody") or item.get("comment") or "")
        rating = (item.get("rating") or item.get("value") or
                  item.get("starRating") or "")
        if not body and not rating:
            continue

        author = (item.get("authorName") or item.get("reviewer") or
                  item.get("author", ""))
        if isinstance(author, dict):
            author = author.get("displayName", "") or author.get("name", "")

        # Extract product info if available
        handle = product_handle
        title = product_title
        product_info = item.get("product") or {}
        if isinstance(product_info, dict):
            handle = (product_info.get("urlSlug") or
                      product_info.get("slug") or
                      product_info.get("handle") or handle)
            title = product_info.get("title") or title

        # Handle product URL in the review
        product_url = item.get("productUrl") or ""
        if product_url and not handle:
            handle = product_url.rstrip("/").split("/")[-1]

        reviews.append({
            "product_title": title,
            "product_handle": handle,
            "rating": str(rating) if rating else "",
            "author": str(author),
            "email": item.get("authorEmail") or item.get("email") or "",
            "title": item.get("title") or "",
            "body": str(body),
            "created_at": (item.get("addedOn") or item.get("createdOn") or
                           item.get("date") or ""),
        })
    return reviews


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
