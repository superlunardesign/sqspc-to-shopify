/**
 * Squarespace → Shopify Product Extractor
 *
 * HOW TO USE:
 * 1. Open your Squarespace site in Chrome/Firefox/Edge
 * 2. Navigate to your shop page (e.g. /shop-skincare)
 * 3. Open DevTools: Cmd+Option+J (Mac) or Ctrl+Shift+J (Windows)
 * 4. Paste this entire script into the Console tab and press Enter
 * 5. Wait for it to finish — CSV files will download automatically
 *
 * Works from the browser with same-origin access to Squarespace's
 * internal JSON API. No server, no proxy, no CORS issues.
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

  function csvEscape(val) {
    const s = String(val ?? "");
    if (s.includes('"') || s.includes(",") || s.includes("\n")) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function downloadCsv(filename, headers, rows) {
    const lines = [headers.map(csvEscape).join(",")];
    for (const row of rows) {
      lines.push(headers.map((h) => csvEscape(row[h] ?? "")).join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ── Squarespace JSON API fetcher ─────────────────────────────────

  async function fetchJson(url) {
    const resp = await fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!resp.ok) return null;
    return resp.json();
  }

  async function discoverShopPath() {
    for (const path of SHOP_PATHS_TO_TRY) {
      console.log(`[sqspc] Trying ${path}...`);
      const data = await fetchJson(path + "?format=json&page=1");
      if (data && data.items && data.items.length > 0) {
        console.log(`[sqspc] ✓ Found products at ${path}`);
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
      const url = `${shopPath}?format=json&page=${page}`;
      console.log(`[sqspc] Fetching page ${page}...`);
      const data = await fetchJson(url);
      if (!data || !data.items || data.items.length === 0) break;
      allItems.push(...data.items);
      console.log(
        `[sqspc]   Got ${data.items.length} items (total: ${allItems.length})`
      );
      if (!data.pagination || data.pagination.nextPage === false) break;
      page++;
      await sleep(DELAY_MS);
    }
    return allItems;
  }

  // ── Fetch individual product detail pages ──────────────────────

  async function fetchProductDetail(item) {
    const slug = item.urlId || item.fullUrl || "";
    if (!slug) return {};

    const detailUrl = item.fullUrl
      ? item.fullUrl + "?format=json"
      : `${slug}?format=json`;
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
          // Grab sibling content after the heading
          const content = [];
          let sibling = heading.nextElementSibling || heading.parentElement?.nextElementSibling;
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

  // ── Extract reviews from product page DOM ───────────────────────

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
          const filled = starsEl.querySelectorAll(".filled, .star--filled, [data-active]");
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
                    author: r.authorName || r.author?.displayName || r.reviewer || "",
                    body: r.body || r.content || r.reviewBody || r.comment || "",
                    rating: String(r.rating || r.value || r.starRating || ""),
                    title: r.title || "",
                    date: r.addedOn || r.createdOn || r.date || "",
                    email: r.email || r.authorEmail || "",
                  });
                }
              }
            } catch {}
          }
        }
      }
    } catch (e) {
      console.warn(`[sqspc] Error extracting reviews: ${e.message}`);
    }
    return reviews;
  }

  // ── Process a raw Squarespace item into our product format ──────

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

    // Handle
    const handle = slugify(merged.urlId || title);

    // URL
    const fullUrl = merged.fullUrl
      ? new URL(merged.fullUrl, location.origin).href
      : "";

    // Description and body HTML
    const bodyHtml = merged.body || merged.excerpt || "";
    const description = stripHtml(bodyHtml);

    // Structured sections
    const sections = extractSections(bodyHtml);

    // Also check structuredContent or additionalInfoSections
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
    const sqVariants = merged.variants || merged.structuredContent?.variants || [];
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

        // Price: Squarespace stores price in cents in the JSON API
        let price = "";
        let comparePrice = "";
        const rawPrice = v.priceMoney?.value ?? v.price ?? v.retailPrice ?? merged.basicPrice;
        if (rawPrice != null) {
          const cents = Number(rawPrice);
          price = cents >= 100 ? (cents / 100).toFixed(2) : cents.toFixed(2);
        }
        const rawSale = v.salePriceMoney?.value ?? v.salePrice;
        if (rawSale != null && rawPrice != null) {
          comparePrice = price;
          const saleCents = Number(rawSale);
          price = saleCents >= 100 ? (saleCents / 100).toFixed(2) : saleCents.toFixed(2);
        }
        const rawOnSale = v.onSale ?? merged.onSale;
        if (rawOnSale && !comparePrice && rawSale != null) {
          comparePrice = price;
          const saleCents = Number(rawSale);
          price = saleCents >= 100 ? (saleCents / 100).toFixed(2) : saleCents.toFixed(2);
        }

        const qty = v.qtyInStock ?? v.stock ?? "";
        const soldOut = v.soldOut ?? (qty === 0);

        variants.push({
          sku: v.sku || "",
          price,
          compare_at_price: comparePrice,
          weight: String(v.weight ?? ""),
          weight_unit: v.weightUnit || "lb",
          inventory_qty: soldOut ? "0" : String(qty),
          option_values: optValues,
          image: cleanImageUrl(v.imageUrl || v.mainImageUrl || ""),
        });
      }
    }

    // Fallback: single default variant from item-level pricing
    if (variants.length === 0) {
      let price = "";
      const rawPrice = merged.basicPrice ?? merged.priceMoney?.value;
      if (rawPrice != null) {
        const cents = Number(rawPrice);
        price = cents >= 100 ? (cents / 100).toFixed(2) : cents.toFixed(2);
      }
      variants.push({
        sku: merged.sku || "",
        price,
        compare_at_price: "",
        weight: "",
        weight_unit: "lb",
        inventory_qty: "",
        option_values: [],
        image: "",
      });
    }

    // Tags and categories
    const tags = (merged.tags || []).map(String);
    const categories = (merged.categories || []).map(String);

    // Sold out?
    const soldOut =
      merged.isAvailable === false ||
      merged.isSoldOut === true ||
      variants.every((v) => v.inventory_qty === "0");

    return {
      id: merged.id || "",
      title,
      handle,
      url: fullUrl,
      body_html: bodyHtml,
      description,
      images,
      variants,
      tags,
      categories,
      vendor,
      product_type: categories[0] || "",
      sold_out: soldOut,
      ...sections,
    };
  }

  // ── Build Shopify product CSV rows ──────────────────────────────

  const PRODUCT_HEADERS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type",
    "Tags", "Published", "Option1 Name", "Option1 Value", "Option2 Name",
    "Option2 Value", "Option3 Name", "Option3 Value", "Variant SKU",
    "Variant Grams", "Variant Inventory Tracker", "Variant Inventory Qty",
    "Variant Inventory Policy", "Variant Fulfillment Service", "Variant Price",
    "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
    "Variant Barcode", "Image Src", "Image Position", "Image Alt Text",
    "Gift Card", "SEO Title", "SEO Description", "Variant Image",
    "Variant Weight Unit", "Variant Tax Code", "Cost per item", "Status",
  ];

  const REVIEW_HEADERS = [
    "product_handle", "state", "rating", "title", "author", "email",
    "body", "created_at",
  ];

  function buildProductRows(products) {
    const rows = [];
    for (const p of products) {
      const bodyHtml = buildBodyHtml(p);
      const tags = [...p.tags, ...p.categories].join(", ");
      const status = p.sold_out ? "draft" : "active";
      const optionNames =
        p.variants[0]?.option_values?.map((ov) => ov[0]) || [];

      for (let vi = 0; vi < p.variants.length; vi++) {
        const v = p.variants[vi];
        const isFirst = vi === 0;
        const row = {
          Handle: p.handle,
          Title: isFirst ? p.title : "",
          "Body (HTML)": isFirst ? bodyHtml : "",
          Vendor: isFirst ? p.vendor : "",
          "Product Category": "",
          Type: isFirst ? p.product_type : "",
          Tags: isFirst ? tags : "",
          Published: p.sold_out ? "FALSE" : "TRUE",
          "Option1 Name": "",
          "Option1 Value": "",
          "Option2 Name": "",
          "Option2 Value": "",
          "Option3 Name": "",
          "Option3 Value": "",
          "Variant SKU": v.sku,
          "Variant Grams": "",
          "Variant Inventory Tracker": "shopify",
          "Variant Inventory Qty": v.inventory_qty,
          "Variant Inventory Policy": "deny",
          "Variant Fulfillment Service": "manual",
          "Variant Price": v.price,
          "Variant Compare At Price": v.compare_at_price,
          "Variant Requires Shipping": "TRUE",
          "Variant Taxable": "TRUE",
          "Variant Barcode": "",
          "Image Src": isFirst && p.images[0] ? p.images[0] : "",
          "Image Position": isFirst && p.images[0] ? "1" : "",
          "Image Alt Text": isFirst && p.images[0] ? p.title : "",
          "Gift Card": "FALSE",
          "SEO Title": isFirst ? p.title : "",
          "SEO Description": isFirst ? p.description.slice(0, 320) : "",
          "Variant Image": v.image,
          "Variant Weight Unit": (v.weight_unit || "lb")
            .toLowerCase()
            .replace("pounds", "lb")
            .replace("ounces", "oz")
            .replace("kilograms", "kg")
            .replace("grams", "g"),
          "Variant Tax Code": "",
          "Cost per item": "",
          Status: status,
        };

        // Option values
        const opts = v.option_values || [];
        for (let i = 0; i < 3; i++) {
          if (i < opts.length) {
            row[`Option${i + 1} Name`] = isFirst ? opts[i][0] : (i < optionNames.length ? "" : "");
            row[`Option${i + 1} Value`] = opts[i][1];
          }
        }

        rows.push(row);
      }

      // Additional image rows
      for (let ii = 1; ii < p.images.length; ii++) {
        const imgRow = {};
        for (const h of PRODUCT_HEADERS) imgRow[h] = "";
        imgRow.Handle = p.handle;
        imgRow["Image Src"] = p.images[ii];
        imgRow["Image Position"] = String(ii + 1);
        imgRow["Image Alt Text"] = p.title;
        rows.push(imgRow);
      }
    }
    return rows;
  }

  function buildBodyHtml(product) {
    const parts = [];
    if (product.body_html) parts.push(product.body_html);
    const sectionMap = [
      ["What It Is", "what_it_is"],
      ["Benefits", "benefits"],
      ["Who It's For", "who_its_for"],
      ["How to Use", "how_to_use"],
      ["Ingredients", "ingredients"],
    ];
    for (const [label, key] of sectionMap) {
      const val = (product[key] || "").trim();
      if (val) {
        const safe = val.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>");
        parts.push(`<h3>${label}</h3>\n<p>${safe}</p>`);
      }
    }
    return parts.join("\n");
  }

  // ── MAIN ────────────────────────────────────────────────────────

  console.log("[sqspc] === Squarespace → Shopify Extractor ===");
  console.log("[sqspc] Discovering shop path...");

  // Allow user to set path before running: window.__SQSPC_SHOP_PATH = "/shop-skincare"
  const shopPath = await discoverShopPath();
  if (!shopPath) {
    console.error(
      "[sqspc] ✗ Could not find products. Set window.__SQSPC_SHOP_PATH = '/your-shop-path' and re-run."
    );
    return;
  }

  // Phase 1: Get product list
  console.log("[sqspc] Phase 1: Fetching product listings...");
  const rawItems = await scrapeProductList(shopPath);
  console.log(`[sqspc] Found ${rawItems.length} products in listings`);

  if (rawItems.length === 0) {
    console.error("[sqspc] ✗ No products found. Check the shop path.");
    return;
  }

  // Phase 2: Fetch individual product details
  console.log("[sqspc] Phase 2: Fetching product details...");
  const products = [];
  for (let i = 0; i < rawItems.length; i++) {
    const item = rawItems[i];
    console.log(
      `[sqspc]   (${i + 1}/${rawItems.length}) ${item.title || "untitled"}`
    );
    const detail = await fetchProductDetail(item);
    const product = processItem(item, detail);
    products.push(product);
    await sleep(DELAY_MS);
  }

  // Phase 3: Extract reviews
  console.log("[sqspc] Phase 3: Extracting reviews...");
  const allReviews = [];
  for (const product of products) {
    if (!product.url) continue;
    const reviews = await extractReviewsFromPage(product.url);
    for (const r of reviews) {
      allReviews.push({
        product_handle: product.handle,
        state: "published",
        rating: r.rating,
        title: r.title,
        author: r.author,
        email: r.email,
        body: r.body,
        created_at: r.date,
      });
    }
    if (reviews.length > 0) {
      console.log(
        `[sqspc]   ${product.title}: ${reviews.length} review(s)`
      );
    }
    await sleep(DELAY_MS / 2);
  }

  // Phase 4: Generate and download CSVs
  console.log("[sqspc] Phase 4: Generating CSVs...");

  const productRows = buildProductRows(products);
  downloadCsv("shopify_products.csv", PRODUCT_HEADERS, productRows);
  console.log(
    `[sqspc] ✓ Downloaded shopify_products.csv (${products.length} products, ${productRows.length} rows)`
  );

  if (allReviews.length > 0) {
    downloadCsv("shopify_reviews.csv", REVIEW_HEADERS, allReviews);
    console.log(
      `[sqspc] ✓ Downloaded shopify_reviews.csv (${allReviews.length} reviews)`
    );
  } else {
    console.log("[sqspc] No reviews found — skipping reviews CSV");
  }

  // Summary
  console.log("\n[sqspc] ═══════════════════════════════════════");
  console.log(`[sqspc]  Products: ${products.length}`);
  console.log(`[sqspc]  Reviews:  ${allReviews.length}`);
  console.log(`[sqspc]  Sold out: ${products.filter((p) => p.sold_out).length}`);
  console.log("[sqspc] ═══════════════════════════════════════");
  console.log("[sqspc] Done! CSVs downloaded to your browser.");
  console.log("[sqspc] Import shopify_products.csv via Shopify Admin → Products → Import");
  console.log("[sqspc] Import shopify_reviews.csv via your review app (Judge.me, Loox, etc.)");
})();
