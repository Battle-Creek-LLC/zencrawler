# ZenCrawler — Specification

A lean, production-quality Python web crawling library built natively on
[zendriver](https://github.com/stephanlensky/zendriver) (CDP-based Chrome automation).

---

## Premise

zendriver drives real Chrome via the Chrome DevTools Protocol with no WebDriver
fingerprints. Detection evasion is solved at the protocol level. ZenCrawler's only
job is **orchestration**: what to crawl, when, with how many browsers, and where
results go.

This is not a Crawlee port. It does not try to replicate Playwright/Puppeteer
abstractions. The API is idiomatic Python — async-native, protocol-typed, minimal.

---

## Design Constraints

| Constraint | Detail |
|---|---|
| Language | Python 3.11+ |
| Concurrency | asyncio throughout — no threads, no multiprocessing |
| Dependencies | Zero mandatory beyond zendriver and stdlib |
| Optional extras | `aiohttp` for non-browser requests; SQLite via stdlib `sqlite3` |
| Deployment targets | Laptop dev environment AND Docker container at scale |
| API style | Idiomatic Python — not a JS port |

---

## Installation

```
pip install zencrawler
```

Requires Python 3.11+. zendriver is the only mandatory dependency; Chrome must
be installed on the system (zendriver locates it automatically).

Optional extras:
```
pip install zencrawler[aiohttp]   # non-browser HTTP requests in handlers
```

### Compatibility

| ZenCrawler | Python | zendriver |
|---|---|---|
| 0.1.x | 3.11, 3.12, 3.13 | ≥ 0.15 |

---

## Quick Example

```python
import asyncio
from zencrawler import Crawler, Request, Router

router = Router()

@router.on("https://books.toscrape.com/catalogue/**.html")
async def book_page(ctx):
    title = await ctx.page.select("h1")
    price = await ctx.page.select(".price_color")
    await ctx.dataset.push({
        "title": title.text,
        "price": price.text,
        "url":   ctx.request.url,
    })

@router.on("https://books.toscrape.com/**")
async def listing_page(ctx):
    links = await ctx.page.select_all("article.product_pod h3 a")
    await ctx.enqueue_all([a.get("href") for a in links])
    next_btn = await ctx.page.query_selector("li.next a")
    if next_btn:
        await ctx.enqueue(next_btn.get("href"))

async def main():
    async with Crawler(router=router, max_concurrency=3) as crawler:
        await crawler.run([Request("https://books.toscrape.com/")])
    await crawler.dataset.export_json("books.json")

asyncio.run(main())
```

### zendriver page API quick reference

Handlers work with a live `zendriver.Tab` via `ctx.page`. Key methods:

| Method | Returns | Notes |
|---|---|---|
| `await ctx.page.select(selector)` | `Element` | Waits up to 10s; raises if not found |
| `await ctx.page.select_all(selector)` | `list[Element]` | Returns empty list if none found |
| `await ctx.page.query_selector(selector)` | `Element \| None` | Immediate, no wait |
| `await ctx.page.query_selector_all(selector)` | `list[Element]` | Immediate, no wait |
| `await ctx.page.find(text)` | `Element` | Text-based search |
| `await ctx.page.evaluate(js)` | `Any` | Execute JavaScript |
| `await ctx.page.get_content()` | `str` | Full page HTML |
| `ctx.page.title` | `str` | Property — no await |
| `ctx.page.url` | `str` | Property — no await |
| `await ctx.page.save_screenshot(filename)` | `str` | Saves file, returns path |

Key `Element` attributes (properties — no await needed):

| Attribute | Type | Notes |
|---|---|---|
| `element.text` | `str` | Visible text content |
| `element.text_all` | `str` | All text including hidden |
| `element.attrs` | `dict` | All HTML attributes |
| `element.get("href")` | `str \| None` | Single attribute by name |
| `element.get_html()` | `str` | Outer HTML |

---

## Document Index

| File | Contents |
|---|---|
| [`primitives.md`](primitives.md) | Core types: Request, CrawlContext, Router, HookContext, Dataset, Store |
| [`browser-pool.md`](browser-pool.md) | Chrome process lifecycle, checkout/return, crash recovery |
| [`request-queue.md`](request-queue.md) | Queue backends, state machine, deduplication, retry policy |
| [`router.md`](router.md) | Handler registration, URL matching, lifecycle hooks |
| [`concurrency.md`](concurrency.md) | Rate limiting, semaphore, backpressure, scheduling loop, SIGTERM |
| [`storage.md`](storage.md) | Dataset and Store protocols, SQLite backend, export formats |
| [`error-handling.md`](error-handling.md) | Error taxonomy, detection heuristics, hook API |
| [`api.md`](api.md) | Full Crawler API, configuration reference, imports, env vars |
| [`testing.md`](testing.md) | FakePage, build_context, unit and integration test patterns |
| [`decisions.md`](decisions.md) | Non-goals, open questions, design decision log |

---

## Architecture Overview

```
                        ┌─────────────────────────────────────┐
                        │              Crawler                 │
                        │  (orchestrator — owns nothing else)  │
                        └──────┬──────────────┬───────────────┘
                               │              │
                 ┌─────────────▼──┐    ┌──────▼──────────┐
                 │  RequestQueue  │    │   BrowserPool    │
                 │  pending/done  │    │  Chrome procs    │
                 └─────────────┬──┘    └──────┬──────────┘
                               │              │
                               └──────┬───────┘
                                      │ per request
                               ┌──────▼──────────┐
                               │   CrawlContext   │
                               │  page + request  │
                               │  + storage refs  │
                               └──────┬──────────┘
                                      │
                               ┌──────▼──────────┐
                               │     Router       │
                               │  matches handler │
                               └──────┬──────────┘
                                      │
                          ┌───────────▼───────────┐
                          │    User handler fn     │
                          │  reads page, pushes   │
                          │  data, enqueues URLs  │
                          └───────────────────────┘
```

All communication between layers flows through narrow interfaces (Protocols).
No layer holds a reference to a layer above it.
