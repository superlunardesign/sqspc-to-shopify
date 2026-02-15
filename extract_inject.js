/**
 * Injectable Squarespace product extractor.
 *
 * This script is injected into a Squarespace page by Playwright.
 * It uses same-origin fetch() to access the site's JSON API,
 * extracts products and reviews, and stores the result on
 * window.__SQSPC_RESULT for the Python caller to retrieve.
 *
 * Configuration (set before injection):
 *   window.__SQSPC_SHOP_PATH   – shop page path (e.g. "/shop-skincare")
 *   window.__SQSPC_MAX_PRODUCTS – limit products (0 = unlimited)
 */
(async function sqspcExtract() {
  "use strict";

  const SHOP_PATHS_TO_TRY = [
    window.__SQSPC_SHOP_PATH || "",
    "/shop",
    "/shop-skincare",
    "/shop-all",
    "/products",
    "/store",
    "/all-products",
  ].filter(Boolean);

  const MAX_PRODUCTS = window.__SQSPC_MAX_PRODUCTS || 0;
  const DELAY_MS = 800;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ── Utility helpers ──────────────────────────────────────────────

  function slugify(text) {
    return text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
  }

  function cleanImageUrl(url) {
    if (!url) return "";
    try {
      const u = new URL(url, location.origin);
      u.search = "";
      return u.href;
    } catch {
      return url.split("?")[0];
    }
  }

  function stripHtml(html) {
    const el = document.createElement("div");
    el.innerHTML = html || "";
    return el.textContent.trim();
  }

  // ── Squarespace JSON API fetcher ─────────────────────────────────

  async function fetchJson(url) {
    try {
      const resp = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!resp.ok) return null;
      return resp.json();
    } catch {
      return null;
    }
  }

  async function discoverShopPath() {
    for (const path of SHOP_PATHS_TO_TRY) {
      console.log("[sqspc] Trying " + path + "...");
      const data = await fetchJson(path + "?format=json&page=1");
      if (data && data.items && data.items.length > 0) {
        console.log("[sqspc] Found products at " + path);
        return path;
      }
      await sleep(DELAY_MS);
    }
    return null;
  }

  // ── Scrape products via JSON API with pagination ────────────────

  async function scrapeProductList(shopPath) {
    const allItems = [];
    let page = 1;
    while (true) {
      const url = shopPath + "?format=json&page=" + page;
      console.log("[sqspc] Fetching listing page " + page + "...");
      const data = await fetchJson(url);
      if (!data || !data.items || data.items.length === 0) break;
      allItems.push(...data.items);
      console.log("[sqspc]   Got " + data.items.length + " items (total: " + allItems.length + ")");
      if (MAX_PRODUCTS > 0 && allItems.length >= MAX_PRODUCTS) {
        allItems.length = MAX_PRODUCTS;
        break;
      }
      if (!data.pagination || data.pagination.nextPage === false) break;
      page++;
      await sleep(DELAY_MS);
    }
    return allItems;
  }

  // ── Fetch individual product detail pages ──────────────────────

  async function fetchProductDetail(item) {
    const detailUrl = item.fullUrl
      ? item.fullUrl + "?format=json"
      : null;
    if (!detailUrl) return {};
    const data = await fetchJson(detailUrl);
    return data?.item || data || {};
  }

  // ── Extract structured sections from product HTML ──────────────

  function extractSections(bodyHtml) {
    const sections = {
      ingredients: "",
      how_to_use: "",
      benefits: "",
      what_it_is: "",
      who_its_for: "",
    };

    if (!bodyHtml) return sections;

    const div = document.createElement("div");
    div.innerHTML = bodyHtml;

    const patterns = {
      ingredients: /ingredient/i,
      how_to_use: /how\s*to\s*use|direction|usage|application/i,
      benefits: /benefit|why\s*you/i,
      what_it_is: /what\s*(it\s*)?is|about\s*this/i,
      who_its_for: /who\s*(it'?s?\s*)?for|ideal\s*for|suitable\s*for|skin\s*type/i,
    };

    const headings = div.querySelectorAll("h1, h2, h3, h4, h5, h6, strong, b");
    for (const heading of headings) {
      const text = heading.textContent.trim();
      for (const [key, regex] of Object.entries(patterns)) {
        if (regex.test(text) && !sections[key]) {
          const content = [];
          let sibling =
            heading.nextElementSibling ||
            heading.parentElement?.nextElementSibling;
          while (sibling && !sibling.matches("h1, h2, h3, h4, h5, h6")) {
            const t = sibling.textContent.trim();
            if (t) content.push(t);
            sibling = sibling.nextElementSibling;
          }
          if (content.length > 0) {
            sections[key] = content.join("\n");
          }
        }
      }
    }
    return sections;
  }

  // ── Extract reviews from a product page ─────────────────────────

  async function extractReviewsFromPage(productUrl) {
    const reviews = [];
    try {
      const resp = await fetch(productUrl, { credentials: "same-origin" });
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, "text/html");

      // Strategy 1: Squarespace native review elements
      const reviewEls = doc.querySelectorAll(
        '.review-item, [data-testid="review-item"], .sqs-review'
      );
      for (const el of reviewEls) {
        const author =
          el.querySelector('[data-testid="reviewer-name"], .review-author')
            ?.textContent?.trim() || "";
        const body =
          el.querySelector('[data-testid="review-desc"], .review-body, .review-content')
            ?.textContent?.trim() || "";
        const date =
          el.querySelector('[data-testid="review-date"], .review-date')
            ?.textContent?.trim() || "";
        const starsEl = el.querySelector(
          '[data-testid="review-stars"], .review-stars, .star-rating'
        );
        let rating = "";
        if (starsEl) {
          const filled = starsEl.querySelectorAll(
            ".filled, .star--filled, [data-active]"
          );
          if (filled.length > 0) rating = String(filled.length);
          else {
            const ariaLabel = starsEl.getAttribute("aria-label") || "";
            const m = ariaLabel.match(/(\d+(\.\d+)?)/);
            if (m) rating = m[1];
          }
        }
        if (author || body) {
          reviews.push({ author, body, rating, title: "", date, email: "" });
        }
      }

      // Strategy 2: Embedded JSON in script tags
      if (reviews.length === 0) {
        const scripts = doc.querySelectorAll("script");
        for (const script of scripts) {
          const text = script.textContent || "";
          if (text.includes("review") && text.includes("{")) {
            try {
              const jsonMatch = text.match(
                /(?:reviews|reviewItems|storeReviews)\s*[:=]\s*(\[[\s\S]*?\])/
              );
              if (jsonMatch) {
                const arr = JSON.parse(jsonMatch[1]);
                for (const r of arr) {
                  reviews.push({
                    author:
                      r.authorName ||
                      r.author?.displayName ||
                      r.reviewer ||
                      "",
                    body:
                      r.body ||
                      r.content ||
                      r.reviewBody ||
                      r.comment ||
                      "",
                    rating: String(r.rating || r.value || r.starRating || ""),
                    title: r.title || "",
                    date: r.addedOn || r.createdOn || r.date || "",
                    email: r.email || r.authorEmail || "",
                  });
                }
              }
            } catch {
              /* ignore parse errors */
            }
          }
        }
      }
    } catch (e) {
      console.warn("[sqspc] Error extracting reviews: " + e.message);
    }
    return reviews;
  }

  // ── Process a raw Squarespace item into structured product ──────

  function processItem(item, detail) {
    const merged = { ...item, ...detail };

    // Title and vendor
    let title = merged.title || "";
    let vendor = "";
    if (title.includes("|")) {
      const parts = title.split("|").map((s) => s.trim());
      title = parts[0];
      vendor = parts.slice(1).join(" ").trim();
    }

    const handle = slugify(merged.urlId || title);
    const fullUrl = merged.fullUrl
      ? new URL(merged.fullUrl, location.origin).href
      : "";
    const bodyHtml = merged.body || merged.excerpt || "";
    const description = stripHtml(bodyHtml);
    const sections = extractSections(bodyHtml);

    // Check structuredContent additional info
    if (merged.structuredContent) {
      const sc = merged.structuredContent;
      if (sc.productAddlInfoSections) {
        for (const sec of sc.productAddlInfoSections) {
          const secTitle = (sec.title || "").toLowerCase();
          const secBody = stripHtml(sec.body || "");
          if (!secBody) continue;
          if (/ingredient/i.test(secTitle) && !sections.ingredients)
            sections.ingredients = secBody;
          else if (/how\s*to/i.test(secTitle) && !sections.how_to_use)
            sections.how_to_use = secBody;
          else if (/benefit/i.test(secTitle) && !sections.benefits)
            sections.benefits = secBody;
          else if (/what\s*(it\s*)?is/i.test(secTitle) && !sections.what_it_is)
            sections.what_it_is = secBody;
          else if (/who/i.test(secTitle) && !sections.who_its_for)
            sections.who_its_for = secBody;
        }
      }
    }

    // Images
    const images = [];
    if (merged.items && Array.isArray(merged.items)) {
      for (const img of merged.items) {
        if (img.assetUrl) images.push(cleanImageUrl(img.assetUrl));
      }
    }
    if (merged.assetUrl) images.push(cleanImageUrl(merged.assetUrl));
    if (images.length === 0 && merged.imageUrl) {
      images.push(cleanImageUrl(merged.imageUrl));
    }

    // Variants
    const variants = [];
    const sqVariants =
      merged.variants || merged.structuredContent?.variants || [];
    if (sqVariants.length > 0) {
      for (const v of sqVariants) {
        const optValues = [];
        const attrs = v.attributes || v.optionValues || [];
        if (Array.isArray(attrs)) {
          for (const attr of attrs) {
            if (typeof attr === "object") {
              optValues.push([attr.optionName || "Option", attr.value || ""]);
            } else if (typeof attr === "string") {
              optValues.push(["Option", attr]);
            }
          }
        }

        let price = "";
        let comparePrice = "";
        const rawPrice =
          v.priceMoney?.value ?? v.price ?? v.retailPrice ?? merged.basicPrice;
        if (rawPrice != null) {
          const cents = Number(rawPrice);
          price = cents >= 100 ? (cents / 100).toFixed(2) : cents.toFixed(2);
        }
        const rawSale = v.salePriceMoney?.value ?? v.salePrice;
        if (rawSale != null && rawPrice != null) {
          comparePrice = price;
          const saleCents = Number(rawSale);
          price =
            saleCents >= 100
              ? (saleCents / 100).toFixed(2)
              : saleCents.toFixed(2);
        }

        const qty = v.qtyInStock ?? v.stock ?? "";
        const soldOut = v.soldOut ?? qty === 0;

        variants.push({
          sku: v.sku || "",
          price: price,
          compare_at_price: comparePrice,
          weight: String(v.weight ?? ""),
          weight_unit: v.weightUnit || "lb",
          inventory_qty: soldOut ? "0" : String(qty),
          option_values: optValues,
          image: cleanImageUrl(v.imageUrl || v.mainImageUrl || ""),
        });
      }
    }

    // Fallback: single default variant
    if (variants.length === 0) {
      let price = "";
      const rawPrice = merged.basicPrice ?? merged.priceMoney?.value;
      if (rawPrice != null) {
        const cents = Number(rawPrice);
        price = cents >= 100 ? (cents / 100).toFixed(2) : cents.toFixed(2);
      }
      variants.push({
        sku: merged.sku || "",
        price: price,
        compare_at_price: "",
        weight: "",
        weight_unit: "lb",
        inventory_qty: "",
        option_values: [],
        image: "",
      });
    }

    const tags = (merged.tags || []).map(String);
    const categories = (merged.categories || []).map(String);
    const soldOut =
      merged.isAvailable === false ||
      merged.isSoldOut === true ||
      variants.every((v) => v.inventory_qty === "0");

    return {
      id: merged.id || "",
      title: title,
      handle: handle,
      url: fullUrl,
      body_html: bodyHtml,
      description: description,
      images: images,
      variants: variants,
      tags: tags,
      categories: categories,
      vendor: vendor,
      product_type: categories[0] || "",
      sold_out: soldOut,
      ingredients: sections.ingredients,
      how_to_use: sections.how_to_use,
      benefits: sections.benefits,
      what_it_is: sections.what_it_is,
      who_its_for: sections.who_its_for,
    };
  }

  // ── MAIN ────────────────────────────────────────────────────────

  console.log("[sqspc] === Browser Extractor Starting ===");

  const shopPath = await discoverShopPath();
  if (!shopPath) {
    console.error("[sqspc] Could not find products at any known shop path");
    window.__SQSPC_RESULT = { products: [], reviews: [], error: "No products found" };
    return;
  }

  // Phase 1: Get product list
  console.log("[sqspc] Phase 1: Fetching product listings...");
  const rawItems = await scrapeProductList(shopPath);
  console.log("[sqspc] Found " + rawItems.length + " products in listings");

  if (rawItems.length === 0) {
    window.__SQSPC_RESULT = { products: [], reviews: [] };
    return;
  }

  // Phase 2: Fetch individual product details
  console.log("[sqspc] Phase 2: Fetching product details...");
  const products = [];
  for (let i = 0; i < rawItems.length; i++) {
    const item = rawItems[i];
    console.log("[sqspc]   (" + (i + 1) + "/" + rawItems.length + ") " + (item.title || "untitled"));
    const detail = await fetchProductDetail(item);
    const product = processItem(item, detail);
    products.push(product);
    await sleep(DELAY_MS);
  }

  // Reviews are NOT extracted here — the JSON API and static HTML
  // fetches cannot see JS-rendered review widgets.  Reviews are
  // extracted by Playwright in Phase 2 (browser_extractor.py) which
  // actually renders the page, scrolls down, clicks "Show All", and
  // reads the live DOM.

  // Store result for Python to retrieve
  console.log("[sqspc] Done: " + products.length + " products (reviews handled by Playwright)");
  window.__SQSPC_RESULT = {
    products: products,
    reviews: [],  // Playwright handles review extraction
  };
})();
