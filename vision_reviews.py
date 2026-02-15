"""
Vision-based review extraction using Playwright screenshots + Claude.

Last-resort fallback: when the Playwright DOM extraction can't find
review elements (e.g. reviews rendered inside a shadow DOM or canvas),
this module takes a full-page screenshot and sends it to Claude's
vision API to read the review content visually.

Before screenshotting, it scrolls the page and clicks "Show All" /
"Load More" buttons to ensure all reviews are visible.

Requires:
  - ANTHROPIC_API_KEY environment variable
  - playwright (with Chromium installed)
  - anthropic Python package
"""

import base64
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

REVIEW_EXTRACTION_PROMPT = """\
Look at this screenshot of a product page. Extract ALL customer reviews visible on the page.

For each review, extract:
- author: the reviewer's name/display name
- rating: star rating as a number (1-5)
- title: review title if present (empty string if none)
- body: the full review text
- date: the date of the review if visible (empty string if none)

Return ONLY a JSON array of review objects with those exact keys. No other text.
Example: [{"author": "Jane D.", "rating": 5, "title": "Amazing!", "body": "Love this product", "date": "Jan 15, 2024"}]

If there are no reviews visible, return an empty array: []
"""

# Reuse the same expand-button selectors from the main extractor
_SHOW_ALL_SELECTORS = [
    'button:has-text("Show All")',
    'button:has-text("show all")',
    'a:has-text("Show All")',
    'button:has-text("See All Reviews")',
    'button:has-text("View All Reviews")',
    'button:has-text("Load More")',
    'button:has-text("load more")',
    'a:has-text("Load More")',
    'button:has-text("Show more")',
    'button:has-text("More Reviews")',
    '.load-more-btn',
    '.show-all-btn',
    '.load-more',
    '.yotpo-load-more',
    '.jdgm-load-more',
    '.stamped-load-more',
]


def _click_all_expand_buttons(page) -> int:
    """Click expand/load-more buttons until none remain. Returns click count."""
    total = 0
    for _ in range(20):
        clicked = False
        for sel in _SHOW_ALL_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=400):
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                    btn.click()
                    total += 1
                    clicked = True
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        if not clicked:
            break
    return total


def extract_reviews_via_screenshot(
    page,
    product_url: str,
    product_title: str = "",
    product_handle: str = "",
) -> list[dict]:
    """Take a screenshot of a product page and use Claude vision to extract reviews.

    Parameters
    ----------
    page : playwright Page object
        An already-open Playwright page (reuses the browser session).
    product_url : str
        URL of the product page to screenshot.
    product_title : str
        Product title (for logging and result tagging).
    product_handle : str
        Product handle (for result tagging).

    Returns
    -------
    list[dict]
        List of review dicts with keys: product_handle, product_title,
        rating, author, title, body, created_at, email.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — cannot use vision review extraction")
        return []

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed — pip install anthropic")
        return []

    reviews = []

    try:
        logger.info("Vision reviews: navigating to %s", product_url)
        page.goto(product_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(4000)

        # Scroll down to trigger lazy-loading of review sections
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Click "Show All" / "Load More" buttons to expand all reviews
        clicks = _click_all_expand_buttons(page)
        if clicks:
            logger.info("Vision reviews: clicked %d expand buttons on %s", clicks, product_title)
            # Scroll again after expanding
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)

        # Take a full-page screenshot
        screenshot_bytes = page.screenshot(full_page=True, type="png")
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        logger.info(
            "Vision reviews: captured screenshot (%d KB) for %s",
            len(screenshot_bytes) // 1024,
            product_title,
        )

        # If the screenshot is very large (>10MB), crop to the review area
        if len(screenshot_bytes) > 10 * 1024 * 1024:
            logger.info("Vision reviews: screenshot too large, taking bottom-half crop")
            page_height = page.evaluate("document.body.scrollHeight")
            screenshot_bytes = page.screenshot(
                clip={"x": 0, "y": page_height // 2, "width": 1280, "height": page_height // 2},
                type="png",
            )
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Send to Claude vision API
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": REVIEW_EXTRACTION_PROMPT,
                        },
                    ],
                }
            ],
        )

        # Parse Claude's response
        response_text = message.content[0].text.strip()
        logger.info("Vision reviews: Claude response for %s: %s", product_title, response_text[:200])

        # Extract JSON from the response (handle markdown code blocks)
        json_match = re.search(r"\[[\s\S]*\]", response_text)
        if json_match:
            raw_reviews = json.loads(json_match.group())
        else:
            logger.warning("Vision reviews: no JSON array found in response")
            return []

        # Normalize into our review format
        for r in raw_reviews:
            reviews.append({
                "product_title": product_title,
                "product_handle": product_handle,
                "rating": str(r.get("rating", "")),
                "author": r.get("author", ""),
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "created_at": r.get("date", ""),
                "email": "",
            })

        logger.info(
            "Vision reviews: extracted %d reviews for %s",
            len(reviews),
            product_title,
        )

    except json.JSONDecodeError as exc:
        logger.error("Vision reviews: failed to parse JSON from Claude response: %s", exc)
    except Exception as exc:
        logger.error("Vision reviews: error processing %s: %s", product_url, exc)

    return reviews
