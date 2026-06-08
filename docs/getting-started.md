# Getting Started

## Installation

```bash
pip install zencrawler
```

For persistent queues and storage backed by SQLite (recommended for production):

```bash
pip install "zencrawler[sqlite]"
```

**Requirements:** Python 3.11 or later, Chrome or Chromium installed and on your `$PATH`.

---

## Your first crawler

The following example crawls [books.toscrape.com](https://books.toscrape.com), extracts book titles and prices, and saves them to a dataset.

```python title="first_crawl.py"
import asyncio
from zencrawler import Crawler, Router, Request

router = Router()


@router.default
async def handle_page(ctx):
    page = ctx.page

    # Extract title and price from the current page
    title_el = await page.query_selector("h1")
    price_el  = await page.query_selector(".price_color")

    if title_el and price_el:
        await ctx.dataset.push({
            "title": title_el.text.strip(),
            "price": price_el.text.strip(),
            "url":   ctx.request.url,
            "depth": ctx.request.depth,
        })

    # Follow links to other pages on the same domain
    import urllib.parse
    base = urllib.parse.urlparse(ctx.request.url)
    for a in await page.select_all("a[href]"):
        href = a.get("href")
        if not href:
            continue
        abs_url = urllib.parse.urljoin(ctx.request.url, href)
        parsed  = urllib.parse.urlparse(abs_url)
        if parsed.netloc == base.netloc:
            await ctx.enqueue(abs_url)


async def main():
    async with Crawler(
        router=router,
        max_requests=50,   # stop after 50 pages
        max_concurrency=3,
    ) as crawler:
        result = await crawler.run(["https://books.toscrape.com/"])

    print(f"Pages:  {result.requests_done}")
    print(f"Items:  {result.items_pushed}")
    print(f"Errors: {result.requests_dead_letter}")
    print(f"Time:   {result.elapsed_seconds:.1f}s")


asyncio.run(main())
```

Run it:

```bash
python first_crawl.py
```

```
Pages:  50
Items:  47
Errors: 0
Time:   18.3s
```

---

## What just happened?

**`Crawler`** is the orchestrator. It manages a pool of Chrome browsers, pulls requests from the queue, dispatches them to your router, and writes results to storage.

**`Router`** maps requests to handler functions. `@router.default` registers a catch-all — it runs for every request that doesn't match a more specific pattern.

**`CrawlContext`** (`ctx`) is passed to every handler. It gives you:

| Attribute | What it is |
|---|---|
| `ctx.page` | A live `zendriver.Tab` — use it to query the rendered DOM |
| `ctx.request` | The `Request` being processed (url, depth, metadata, …) |
| `ctx.dataset` | The default dataset — call `await ctx.dataset.push(dict)` |
| `ctx.store` | A key/value store — call `await ctx.store.set_json("key", value)` |
| `ctx.enqueue(url)` | Queue a new URL for crawling |
| `ctx.log` | A `logging.Logger` scoped to this handler |

**`page`** is a live Chrome tab. The key methods:

```python
el  = await page.query_selector("css-selector")   # returns Element | None
els = await page.select_all("css-selector")        # returns list[Element]
txt = await page.get_content()                     # full HTML as string
val = await page.evaluate("document.title")        # run arbitrary JS
```

On an `Element`:

```python
el.text              # visible text (property)
el.attrs             # dict of HTML attributes (property)
el.get("href")       # single attribute value (returns str | None)
await el.get_html()  # outer HTML as string
```

!!! warning "Properties, not methods"
    `el.text`, `el.attrs`, `page.title`, and `page.url` are **properties** — do not call them as `el.text()`. They're read directly: `title = page.title`.

---

## Using labels

Labels let you route different page types to different handlers:

```python
router = Router()

@router.on(label="listing")
async def handle_listing(ctx):
    # find product links and enqueue them with "product" label
    for a in await ctx.page.select_all(".product-card a"):
        await ctx.enqueue(a.get("href"), label="product")

@router.on(label="product")
async def handle_product(ctx):
    title = await ctx.page.query_selector("h1")
    price = await ctx.page.query_selector(".price")
    if title:
        await ctx.dataset.push({
            "title": title.text,
            "price": price.text if price else None,
        })

# Seed with a label
async with Crawler(router=router, max_requests=200) as crawler:
    await crawler.run([
        Request(url="https://example.com/shop", label="listing")
    ])
```

---

## Switching to SQLite

The default in-memory queue loses state if the process crashes. For longer crawls, use SQLite:

```python
async with Crawler(
    router=router,
    queue="sqlite",
    storage_path="./my_crawl",
    max_requests=5000,
) as crawler:
    await crawler.run(seeds)
```

The SQLite queue records every URL seen and can resume a partially-completed crawl by replaying the `pending` state on startup.

---

## Next steps

- [Routing Requests](guides/routing.md) — URL globs, domain patterns, hooks, `SkipRequest`
- [Storing Data](guides/storage.md) — named datasets, key/value store, iterating results
- [Error Handling](guides/error-handling.md) — retries, bot blocks, custom error hooks
- [Concurrency & Rate Limiting](guides/concurrency.md) — tuning throughput
