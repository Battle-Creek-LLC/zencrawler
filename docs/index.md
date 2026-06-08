# ZenCrawler

**Async Python web crawler built on [zendriver](https://github.com/stephanlensky/zendriver) and Chrome DevTools Protocol.**

ZenCrawler handles browser lifecycle, request deduplication, concurrency control, storage, and error recovery — so your handlers stay focused on extracting data.

```python
from zencrawler import Crawler, Router, Request

router = Router()

@router.default
async def handler(ctx):
    title = await ctx.page.query_selector("h1")
    if title:
        await ctx.dataset.push({"title": title.text, "url": ctx.request.url})

    links = await ctx.page.select_all("a[href]")
    await ctx.enqueue_all([a.get("href") for a in links if a.get("href")])

async def main():
    async with Crawler(router=router, max_requests=50) as crawler:
        result = await crawler.run(["https://books.toscrape.com/"])

        # Dataset is available here, inside the block
        async for item in crawler.dataset.iter():
            print(item["title"])

    print(f"Done — {result.requests_done} pages, {result.items_pushed} items")
```

---

## Why ZenCrawler?

**Real browser rendering.** Powered by Chrome via CDP — JavaScript, lazy-loading, and dynamic content work out of the box.

**Built-in deduplication.** URLs are normalised and hashed before queuing. Tracking parameters (`utm_*`, `fbclid`, etc.) are stripped automatically.

**Crash-resistant.** Browser crashes are detected and recovered without losing queued work. Switch to `queue="sqlite"` for full crash recovery across process restarts.

**Zero mandatory external dependencies.** Only `zendriver` is required. SQLite support (for persistent queues and storage) uses the standard library's `sqlite3` via the optional `aiosqlite` extra.

**Handler-first design.** Route requests by URL pattern, domain, label, or custom predicate. Each handler receives a single `CrawlContext` — page, request, dataset, store, and enqueue helpers in one place.

---

## Features at a glance

| Feature | Detail |
|---|---|
| Concurrency | Semaphore-based, configurable `max_concurrency` |
| Rate limiting | Token-bucket, configurable via env vars or code |
| Deduplication | SHA-256 of normalised URL, configurable strip params |
| Queue backends | In-memory (default) or SQLite (persistent, crash-recoverable) |
| Storage | Dataset (append-only items) + Store (key/value) — SQLite or in-memory |
| Error handling | Typed exception hierarchy, per-error-type hooks, automatic retry with backoff |
| Bot detection | Automatic — checks page title and body for known block signals |
| Signal handling | SIGTERM / SIGINT → graceful shutdown with configurable drain timeout |
| Testing | `FakePage`, `FakeElement`, `build_context` — no Chrome needed in tests |

---

## Installation

```bash
pip install zencrawler
```

With SQLite persistent queue and storage:

```bash
pip install "zencrawler[sqlite]"
```

**Requirements:** Python 3.11+, Chrome or Chromium installed.

---

## Next steps

- [Getting Started](getting-started.md) — install, write your first crawler, run it
- [Routing Requests](guides/routing.md) — patterns, labels, hooks
- [Storing Data](guides/storage.md) — datasets and key/value store
- [API Reference](api/crawler.md) — full class and method docs
