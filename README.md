# ZenCrawler

Async Python web crawler built on [zendriver](https://github.com/stephanlensky/zendriver) and Chrome DevTools Protocol.

**[Documentation →](https://battle-creek-llc.github.io/zencrawler/)**

---

## Install

```bash
pip install zencrawler

# With SQLite persistent queue and storage
pip install "zencrawler[sqlite]"
```

**Requires:** Python 3.11+, Chrome or Chromium

---

## Quick start

```python
import asyncio
from zencrawler import Crawler, Router, Request

router = Router()

@router.default
async def handle(ctx):
    title = await ctx.page.query_selector("h1")
    if title:
        await ctx.dataset.push({"title": title.text, "url": ctx.request.url})

    for a in await ctx.page.select_all("a[href]"):
        await ctx.enqueue(a.get("href"))

async def main():
    async with Crawler(router=router, max_requests=50) as crawler:
        result = await crawler.run(["https://books.toscrape.com/"])

    print(f"{result.requests_done} pages, {result.items_pushed} items")

asyncio.run(main())
```

---

## Features

- **Real browser rendering** — Chrome via CDP; JavaScript, lazy-loading, and dynamic content work out of the box
- **Router** — match requests by label, URL glob, domain, or custom predicate
- **Deduplication** — URLs normalised and hashed; tracking params (`utm_*`, `fbclid`, …) stripped automatically
- **Concurrency** — semaphore-based, configurable; token-bucket rate limiting
- **Storage** — append-only Dataset and key/value Store, SQLite-backed
- **Error handling** — typed exception hierarchy, automatic bot-block detection, exponential back-off retry
- **Crash recovery** — browser crashes detected and recovered; SQLite queue survives process restarts
- **Testing** — `FakePage` / `FakeElement` / `build_context` helpers — no Chrome needed in unit tests

---

## Documentation

Full guides and API reference at **[battle-creek-llc.github.io/zencrawler](https://battle-creek-llc.github.io/zencrawler/)**:

- [Getting Started](https://battle-creek-llc.github.io/zencrawler/getting-started/)
- [Routing Requests](https://battle-creek-llc.github.io/zencrawler/guides/routing/)
- [Storing Data](https://battle-creek-llc.github.io/zencrawler/guides/storage/)
- [Error Handling](https://battle-creek-llc.github.io/zencrawler/guides/error-handling/)
- [API Reference — Crawler](https://battle-creek-llc.github.io/zencrawler/api/crawler/)

---

## License

MIT — see [LICENSE](LICENSE)
