"""
Squarespace product scraper.

Scrapes products, images, descriptions, structured sections
(ingredients, how to use, benefits), and reviews from a
Squarespace storefront using its public JSON API.
"""

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

# Common heading patterns for structured product sections
SECTION_PATTERNS = {
    "ingredients": re.compile(
        r"ingredients?", re.IGNORECASE
    ),
    "how_to_use": re.compile(
        r"how\s+to\s+use|directions?|usage|instructions?", re.IGNORECASE
    ),
    "benefits": re.compile(
        r"benefits?|why\s+you.ll\s+love|features?", re.IGNORECASE
    ),
}


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


def _get_json(session: requests.Session, url: str, retries: int = 3) -> dict | None:
    """Fetch a URL with ``?format=json`` and return the parsed JSON."""
    separator = "&" if "?" in url else "?"
    json_url = f"{url}{separator}format=json"

    for attempt in range(retries):
        try:
            resp = session.get(json_url, headers=DEFAULT_HEADERS, timeout=30)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, json_url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _get_html(session: requests.Session, url: str, retries: int = 3) -> str | None:
    """Fetch a URL and return raw HTML."""
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _parse_sections_from_html(html: str) -> dict:
    """Extract structured sections (ingredients, how to use, benefits) from HTML body."""
    sections = {"ingredients": "", "how_to_use": "", "benefits": ""}

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
    sections = {"ingredients": "", "how_to_use": "", "benefits": ""}

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

    products = []
    page = 1
    seen_ids = set()

    # ----- Paginate through the collection ----------------------------------
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

        for item in items:
            item_id = item.get("id", "")
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            product = _process_product(session, base_url, item)
            if product:
                products.append(product)
                logger.info("Scraped product: %s", product["title"])

            # Be polite
            time.sleep(0.5)

        # Check for more pages
        pagination = data.get("pagination", {})
        if pagination.get("nextPage"):
            page += 1
        elif len(items) >= 20:
            # Some themes don't return pagination info
            page += 1
        else:
            break

    logger.info("Scraped %d products total.", len(products))
    return products


def _process_product(session: requests.Session, base_url: str, item: dict) -> dict | None:
    """Build a normalised product dict from a Squarespace item."""
    try:
        title = item.get("title", "")
        slug = item.get("urlId", "") or item.get("fullUrl", "").rstrip("/").split("/")[-1]
        product_url = f"{base_url}/{slug}" if slug else ""

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
            variant = {
                "sku": v.get("sku", ""),
                "price": v.get("priceMoney", {}).get("value", "")
                or str(v.get("price", {}).get("value", ""))
                if isinstance(v.get("price"), dict)
                else str(v.get("price", "")),
                "compare_at_price": "",
                "weight": v.get("weight", ""),
                "weight_unit": v.get("weightUnit", "POUNDS"),
                "inventory_qty": v.get("qtyInStock", ""),
                "option_values": [],
            }
            # Normalise price from cents to dollars
            try:
                price_val = float(variant["price"])
                if price_val > 100:  # likely in cents
                    variant["price"] = f"{price_val / 100:.2f}"
            except (ValueError, TypeError):
                pass

            sale_price = v.get("salePriceMoney", {}).get("value", "")
            if sale_price:
                try:
                    sale_val = float(sale_price)
                    if sale_val > 100:
                        sale_val = sale_val / 100
                    variant["compare_at_price"] = f"{float(variant['price']):.2f}"
                    variant["price"] = f"{sale_val:.2f}"
                except (ValueError, TypeError):
                    pass

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
            price_str = ""
            price_data = item.get("priceMoney") or item.get("price")
            if isinstance(price_data, dict):
                price_str = str(price_data.get("value", ""))
            elif price_data is not None:
                price_str = str(price_data)

            try:
                price_val = float(price_str)
                if price_val > 100:
                    price_str = f"{price_val / 100:.2f}"
            except (ValueError, TypeError):
                pass

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

        # ---- Structured sections from body HTML ---------------------------
        sections = _parse_sections_from_html(body_html)

        # Also try the JSON-level additional info
        json_sections = _extract_additional_info(item)
        for key in sections:
            if not sections[key] and json_sections[key]:
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
                        if not sections[key] and page_sections[key]:
                            sections[key] = page_sections[key]

                # Pick up any extra images from detail
                for img_data in detail_item.get("images", []):
                    asset = img_data.get("assetUrl", "") or img_data.get("url", "")
                    if asset and clean_image_url(asset) not in images:
                        images.append(clean_image_url(asset))

                detail_json_sections = _extract_additional_info(detail_item)
                for key in sections:
                    if not sections[key] and detail_json_sections[key]:
                        sections[key] = detail_json_sections[key]

        # ---- Tags ---------------------------------------------------------
        tags = item.get("tags", []) or []
        categories = item.get("categories", []) or []

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
            "vendor": "",
            "product_type": "",
            "ingredients": sections["ingredients"],
            "how_to_use": sections["how_to_use"],
            "benefits": sections["benefits"],
        }

        return product

    except Exception as exc:
        logger.error("Error processing product %s: %s", item.get("title", "?"), exc)
        return None


def scrape_reviews(base_url: str, products: list[dict]) -> list[dict]:
    """Scrape product reviews for each product.

    Tries multiple known Squarespace review API patterns and
    falls back to HTML scraping.

    Returns a flat list of review dicts.
    """
    base_url = base_url.rstrip("/")
    session = requests.Session()
    all_reviews = []

    for product in products:
        product_id = product.get("id", "")
        product_title = product.get("title", "")
        product_handle = product.get("handle", "")

        reviews = []

        # --- Method 1: Squarespace review API endpoints --------------------
        api_paths = [
            f"/api/commerce/reviews/published?productId={product_id}",
            f"/api/review/public/reviews?productId={product_id}",
        ]

        for api_path in api_paths:
            try:
                resp = session.get(
                    f"{base_url}{api_path}",
                    headers=DEFAULT_HEADERS,
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    review_list = data if isinstance(data, list) else data.get("reviews", [])
                    for r in review_list:
                        reviews.append(_normalise_review(r, product_title, product_handle))
                    if reviews:
                        break
            except Exception:
                continue

        # --- Method 2: Scrape reviews from product page HTML ---------------
        if not reviews and product.get("url"):
            html = _get_html(session, product["url"])
            if html:
                reviews = _scrape_reviews_from_html(html, product_title, product_handle)

        all_reviews.extend(reviews)
        logger.info("Found %d reviews for %s", len(reviews), product_title)
        time.sleep(0.3)

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
    """Fallback: extract reviews from the rendered product page HTML."""
    reviews = []
    soup = BeautifulSoup(html, "html.parser")

    # Look for common review container patterns
    review_containers = soup.find_all(
        class_=re.compile(r"review(?!-rating)|testimonial", re.I)
    )

    for container in review_containers:
        # Skip if it's just a review summary/count element
        if "count" in (container.get("class", [""])[0] if container.get("class") else ""):
            continue

        rating_el = container.find(class_=re.compile(r"rating|stars?", re.I))
        rating = ""
        if rating_el:
            # Try to find numeric rating
            rating_text = rating_el.get_text(strip=True)
            match = re.search(r"(\d(?:\.\d)?)", rating_text)
            if match:
                rating = match.group(1)
            else:
                # Count star elements
                stars = rating_el.find_all(
                    class_=re.compile(r"filled|active|full|solid", re.I)
                )
                if stars:
                    rating = str(len(stars))

        author_el = container.find(class_=re.compile(r"author|reviewer|name", re.I))
        author = author_el.get_text(strip=True) if author_el else ""

        title_el = container.find(class_=re.compile(r"title|subject|headline", re.I))
        title = title_el.get_text(strip=True) if title_el else ""

        body_el = container.find(class_=re.compile(r"body|content|text|comment", re.I))
        body = body_el.get_text(strip=True) if body_el else ""

        date_el = container.find(class_=re.compile(r"date|time|created", re.I))
        date = date_el.get_text(strip=True) if date_el else ""

        if body or rating:
            reviews.append(
                {
                    "product_title": product_title,
                    "product_handle": product_handle,
                    "rating": rating,
                    "author": author,
                    "email": "",
                    "title": title,
                    "body": body,
                    "created_at": date,
                }
            )

    return reviews
