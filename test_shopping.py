"""
ZenCrawler — 10 shopping site integration test.
Crawls up to 100 pages per site, extracts product data where possible.
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import NamedTuple

from zencrawler import Crawler, Request, Router
from zencrawler.errors import SkipRequest, StructureError

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("crawl.log"),
    ],
)
log = logging.getLogger("test_shopping")

# ── Sites ─────────────────────────────────────────────────────────────────────

SITES = [
    ("books.toscrape.com",      "https://books.toscrape.com/"),
    ("quotes.toscrape.com",     "https://quotes.toscrape.com/"),
    ("webscraper-allinone",     "https://webscraper.io/test-sites/e-commerce/allinone"),
    ("webscraper-scroll",       "https://webscraper.io/test-sites/e-commerce/scroll"),
    ("scrapingcourse-ecommerce","https://www.scrapingcourse.com/ecommerce/"),
    ("toscrape-products",       "https://www.toscrape.com/"),
    ("httpbin-anything",        "https://httpbin.org/"),
    ("example-store-demo",      "https://demo.opencart.com/"),
    ("prestashop-demo",         "https://demo.prestashop.com/#/en"),
    ("magento-demo",            "https://magento.softwaretestingboard.com/"),
]

MAX_PAGES = 100
CONCURRENCY = 3

# ── Shared router ─────────────────────────────────────────────────────────────

router = Router()

# Generic product-page handler — tries common selectors
@router.default
async def generic_handler(ctx):
    url = ctx.request.url
    page = ctx.page

    # Try to find a product title
    title = None
    for sel in ["h1", "[itemprop='name']", ".product-title", ".product-name",
                "#productTitle", ".pdp-title"]:
        el = await page.query_selector(sel)
        if el:
            title = el.text.strip()
            break

    # Try to find a price
    price = None
    for sel in ["[itemprop='price']", ".price", ".product-price", "#priceblock_ourprice",
                ".price_color", ".a-price-whole", "[data-price]"]:
        el = await page.query_selector(sel)
        if el:
            price = el.text.strip() or el.attrs.get("content", "")
            if price:
                break

    # If we found structured data, save it
    if title:
        await ctx.dataset.push({
            "site":  ctx.request.metadata.get("site", "unknown"),
            "url":   url,
            "title": title,
            "price": price,
            "depth": ctx.request.depth,
        })

    # Follow links on the same domain
    import urllib.parse
    base = urllib.parse.urlparse(url)
    links = await page.select_all("a[href]")
    to_enqueue = []
    for a in links:
        href = a.get("href")
        if not href:
            continue
        abs_url = urllib.parse.urljoin(url, href)
        parsed  = urllib.parse.urlparse(abs_url)
        # Only follow same-domain links
        if parsed.netloc == base.netloc and parsed.scheme in ("http", "https"):
            to_enqueue.append(abs_url)

    if to_enqueue:
        await ctx.enqueue_all(to_enqueue[:20])  # cap per-page link fanout


# ── Crawl runner ──────────────────────────────────────────────────────────────

class SiteResult(NamedTuple):
    site:         str
    pages_done:   int
    items_found:  int
    dead_letters: int
    elapsed_s:    float
    error:        str | None


async def crawl_site(name: str, seed_url: str) -> SiteResult:
    log.info("▶ Starting crawl: %s  seed=%s", name, seed_url)
    t0 = time.monotonic()

    # Fresh router per site so handlers don't bleed state
    site_router = Router()

    @site_router.default
    async def handler(ctx):
        await generic_handler(ctx)

    try:
        async with Crawler(
            router=site_router,
            max_concurrency=CONCURRENCY,
            max_requests=MAX_PAGES,
            page_load_timeout=20.0,
            shutdown_timeout=30.0,
            storage_path=Path(f"./crawl_data/{name.replace('/', '_')}"),
        ) as crawler:
            seed = Request(
                url=seed_url,
                metadata={"site": name},
            )
            result = await crawler.run([seed])
            items = result.items_pushed

        elapsed = time.monotonic() - t0
        log.info(
            "✓ %s — pages=%d items=%d dead=%d elapsed=%.1fs",
            name, result.requests_done, items, result.requests_dead_letter, elapsed,
        )
        return SiteResult(
            site=name,
            pages_done=result.requests_done,
            items_found=items,
            dead_letters=result.requests_dead_letter,
            elapsed_s=elapsed,
            error=None,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.error("✗ %s — FAILED in %.1fs: %s", name, elapsed, exc, exc_info=True)
        return SiteResult(
            site=name,
            pages_done=0,
            items_found=0,
            dead_letters=0,
            elapsed_s=elapsed,
            error=str(exc),
        )


async def main():
    log.info("=" * 60)
    log.info("ZenCrawler — 10-site shopping test")
    log.info("max_pages=%d  concurrency=%d", MAX_PAGES, CONCURRENCY)
    log.info("=" * 60)

    results = []
    for name, url in SITES:
        r = await crawl_site(name, url)
        results.append(r)
        # Brief pause between sites to be polite
        await asyncio.sleep(2)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"{'Site':<30} {'Pages':>6} {'Items':>6} {'Dead':>6} {'Time':>7}  Status")
    print("-" * 70)
    for r in results:
        status = "✓" if r.error is None else f"✗ {r.error[:25]}"
        print(f"{r.site:<30} {r.pages_done:>6} {r.items_found:>6} {r.dead_letters:>6} {r.elapsed_s:>6.1f}s  {status}")
    print("=" * 70)

    total_pages = sum(r.pages_done for r in results)
    total_items = sum(r.items_found for r in results)
    ok_sites    = sum(1 for r in results if r.error is None)
    print(f"\nSites OK: {ok_sites}/{len(results)}  Total pages: {total_pages}  Total items: {total_items}")

    # Write JSON summary
    summary = [
        {
            "site": r.site, "pages": r.pages_done, "items": r.items_found,
            "dead_letters": r.dead_letters, "elapsed_s": round(r.elapsed_s, 2),
            "error": r.error,
        }
        for r in results
    ]
    Path("crawl_summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Summary written to crawl_summary.json")


if __name__ == "__main__":
    asyncio.run(main())
