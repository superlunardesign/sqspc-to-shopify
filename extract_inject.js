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

  // ── Extract reviews via Squarespace internal API ────────────────
  //
  // Squarespace loads reviews client-side via ProductReviewsController
  // which calls /api/commerce/product/reviews.  We call the same API
  // directly — no DOM scraping needed.
  //
  // Required:  websiteId  (from Static.SQUARESPACE_CONTEXT or page HTML)
  //            productId  (from the JSON API product data)
  //            crumb      (CSRF token from cookie)
  // ─────────────────────────────────────────────────────────────────

  function getCrumb() {
    const match = document.cookie.match(/(?:^|;\s*)crumb=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function getWebsiteId() {
    // Method 1: Static.SQUARESPACE_CONTEXT (most common)
    try {
      if (window.Static && window.Static.SQUARESPACE_CONTEXT) {
        const wid = window.Static.SQUARESPACE_CONTEXT.websiteId;
        if (wid) return wid;
      }
    } catch { /* ignore */ }

    // Method 2: data-context on product-reviews controller
    try {
      const el = document.querySelector('[data-controller="ProductReviewsController"]');
      if (el && el.dataset.context) {
        const ctx = JSON.parse(el.dataset.context);
        if (ctx.websiteId) return ctx.websiteId;
      }
    } catch { /* ignore */ }

    // Method 3: Search script tags for websiteId
    try {
      for (const script of document.querySelectorAll("script")) {
        const text = script.textContent || "";
        const m = text.match(/"websiteId"\s*:\s*"([a-f0-9-]+)"/);
        if (m) return m[1];
      }
    } catch { /* ignore */ }

    return "";
  }

  function formatReviewDate(dateVal) {
    if (!dateVal) return "";
    try {
      const d = new Date(dateVal);
      if (isNaN(d.getTime())) return String(dateVal);
      // Judge.me accepts YYYY-MM-DD HH:mm:ss UTC
      const pad = (n) => String(n).padStart(2, "0");
      return (
        d.getUTCFullYear() + "-" +
        pad(d.getUTCMonth() + 1) + "-" +
        pad(d.getUTCDate()) + " " +
        pad(d.getUTCHours()) + ":" +
        pad(d.getUTCMinutes()) + ":" +
        pad(d.getUTCSeconds()) + " UTC"
      );
    } catch {
      return String(dateVal);
    }
  }

  function normaliseRating(val) {
    if (!val && val !== 0) return "";
    const n = Number(val);
    if (isNaN(n) || n < 1 || n > 5) return String(val);
    return String(Math.round(n));
  }

  async function fetchReviewsViaAPI(websiteId, productId, crumb) {
    const reviews = [];
    if (!websiteId || !productId) return reviews;

    const headers = {
      "Content-type": "application/json; charset=UTF-8",
    };
    if (crumb) headers["X-CSRF-Token"] = crumb;

    const PAGE_SIZE = 50;
    let page = 0;
    const MAX_PAGES = 20; // safety limit

    while (page < MAX_PAGES) {
      try {
        const url =
          "/api/commerce/product/reviews" +
          "?websiteId=" + encodeURIComponent(websiteId) +
          "&productId=" + encodeURIComponent(productId) +
          "&page=" + page +
          "&size=" + PAGE_SIZE;
        const resp = await fetch(url, { headers, credentials: "same-origin" });
        if (!resp.ok) break;
        const data = await resp.json();
        const batch = data.productReviews || [];
        if (batch.length === 0) break;

        for (const r of batch) {
          const firstName = r.userFirstName || "";
          const lastInitial = r.userLastName ? r.userLastName[0] : "";
          const author = (firstName + (lastInitial ? " " + lastInitial : ""))
            || r.userLoginName || "";

          reviews.push({
            author: author.trim(),
            body: r.text || "",
            rating: normaliseRating(r.starRating),
            title: "",  // Squarespace reviews have no title/headline
            created_at: formatReviewDate(r.reviewDate),
            email: "",
            product_name: r.productName || "",
            product_url: r.productURL || "",
            source_type: r.sourceType || "",
            image_urls: r.imageUrls || [],
          });
        }

        if (batch.length < PAGE_SIZE) break;
        page++;
        await sleep(300);
      } catch (e) {
        console.warn("[sqspc] Review API error (page " + page + "): " + e.message);
        break;
      }
    }
    return reviews;
  }

  async function fetchStoreReviewsViaAPI(websiteId, crumb) {
    const reviews = [];
    if (!websiteId) return reviews;

    const headers = {
      "Content-type": "application/json; charset=UTF-8",
    };
    if (crumb) headers["X-CSRF-Token"] = crumb;

    const PAGE_SIZE = 50;
    let page = 0;
    const MAX_PAGES = 20;

    while (page < MAX_PAGES) {
      try {
        const url =
          "/api/commerce/product/reviews" +
          "?websiteId=" + encodeURIComponent(websiteId) +
          "&page=" + page +
          "&size=" + PAGE_SIZE;
        const resp = await fetch(url, { headers, credentials: "same-origin" });
        if (!resp.ok) break;
        const data = await resp.json();
        const batch = data.productReviews || [];
        if (batch.length === 0) break;

        for (const r of batch) {
          const firstName = r.userFirstName || "";
          const lastInitial = r.userLastName ? r.userLastName[0] : "";
          const author = (firstName + (lastInitial ? " " + lastInitial : ""))
            || r.userLoginName || "";

          reviews.push({
            author: author.trim(),
            body: r.text || "",
            rating: normaliseRating(r.starRating),
            title: "",  // Squarespace reviews have no title/headline
            created_at: formatReviewDate(r.reviewDate),
            email: "",
            product_name: r.productName || "",
            product_url: r.productURL || "",
            source_type: r.sourceType || "",
            image_urls: r.imageUrls || [],
          });
        }

        if (batch.length < PAGE_SIZE) break;
        page++;
        await sleep(300);
      } catch (e) {
        console.warn("[sqspc] Store review API error (page " + page + "): " + e.message);
        break;
      }
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

  // Phase 3: Extract reviews via Squarespace internal API
  //
  // Squarespace loads reviews client-side via ProductReviewsController
  // which calls /api/commerce/product/reviews.  We call the same API
  // directly — much more reliable than DOM scraping.
  console.log("[sqspc] Phase 3: Extracting reviews via internal API...");
  const websiteId = getWebsiteId();
  const crumb = getCrumb();
  const allReviews = [];

  if (!websiteId) {
    console.warn("[sqspc] Could not find websiteId — skipping API review extraction");
    console.log("[sqspc] Reviews will be attempted by Playwright DOM fallback");
  } else {
    console.log("[sqspc] Found websiteId: " + websiteId);
    console.log("[sqspc] Crumb token: " + (crumb ? "present" : "missing"));

    // Strategy 1: Fetch product-specific reviews for each product
    for (const product of products) {
      if (!product.id) continue;
      const reviews = await fetchReviewsViaAPI(websiteId, product.id, crumb);
      for (const r of reviews) {
        allReviews.push({
          product_title: product.title,
          product_handle: product.handle,
          ...r,
        });
      }
      if (reviews.length > 0) {
        console.log("[sqspc]   " + product.title + ": " + reviews.length + " review(s)");
      }
      await sleep(300);
    }

    // Strategy 2: If no per-product reviews, try fetching all store reviews
    if (allReviews.length === 0) {
      console.log("[sqspc] No per-product reviews found, trying store-wide reviews...");
      const storeReviews = await fetchStoreReviewsViaAPI(websiteId, crumb);
      if (storeReviews.length > 0) {
        console.log("[sqspc] Found " + storeReviews.length + " store review(s)");

        // Match store reviews to products by product_url or product_name
        for (const r of storeReviews) {
          let matchedProduct = null;
          if (r.product_url) {
            matchedProduct = products.find(
              (p) => p.url && r.product_url.includes(p.handle)
            );
          }
          if (!matchedProduct && r.product_name) {
            matchedProduct = products.find(
              (p) => p.title && p.title.toLowerCase() === r.product_name.toLowerCase()
            );
          }
          allReviews.push({
            product_title: matchedProduct ? matchedProduct.title : r.product_name || "",
            product_handle: matchedProduct ? matchedProduct.handle : "",
            ...r,
          });
        }
      }
    }
  }

  // Store result for Python to retrieve
  console.log("[sqspc] Done: " + products.length + " products, " + allReviews.length + " reviews");
  window.__SQSPC_RESULT = {
    products: products,
    reviews: allReviews,
  };
})();
