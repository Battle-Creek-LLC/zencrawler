# Public API

The complete user-facing surface. Everything below this layer is internal.

---

## Crawler

The top-level orchestrator. Created once, used as an async context manager.

```python
class Crawler:
    def __init__(
        self,
        router:           Router,

        # Concurrency
        max_concurrency:  int                        = 5,
        max_requests:     int | None                 = None,
        max_depth:        int | None                 = None,

        # Rate limiting
        rate_limit:       RateLimitConfig | None     = None,
        rate_limits:      dict[str, RateLimitConfig] = {},  # per-domain overrides

        # Queue
        queue:            Literal["memory", "sqlite"] | QueueBackend = "memory",
        retry_policy:     RetryPolicy | None         = None,

        # Storage
        storage:          Literal["sqlite"] | StorageBackend = "sqlite",
        storage_path:     Path                       = Path("./crawl_data"),

        # Browser
        browser:          BrowserPoolConfig | None   = None,
        page_load_timeout: float                     = 30.0,

        # Bot detection
        bot_signals:      list[str] | None           = None,
        extra_bot_signals: list[str]                 = [],

        # Dedup
        dedupe_strip_params: list[str] | None        = None,
        dedupe_hash_bytes:   int                     = 16,

        # Backpressure
        backpressure:     BackpressureConfig | None  = None,

        # Shutdown
        shutdown_timeout: float                      = 30.0,
    ): ...

    # Context manager
    async def __aenter__(self) -> "Crawler": ...
    async def __aexit__(self, *args) -> None: ...

    # Run
    async def run(
        self,
        requests:    Iterable[str | Request],
        *,
        wait:        bool = True,    # False → returns immediately after seeding queue
    ) -> CrawlResult: ...

    # Enqueue from outside a handler (before or after run)
    async def enqueue(self, request: str | Request) -> bool: ...
    async def enqueue_all(self, requests: Iterable[str | Request]) -> int: ...

    # Storage access
    @property
    def dataset(self) -> Dataset: ...      # default dataset
    @property
    def store(self) -> Store: ...          # default store
    def get_dataset(self, name: str) -> Dataset: ...
    def get_store(self, name: str) -> Store: ...

    # Queue inspection
    async def queue_stats(self) -> QueueStats: ...
    async def dead_letter_count(self) -> int: ...
    def dead_letters(self) -> AsyncIterator[Request]: ...

    # Stats callback
    def on_stats(self, callback: Callable[[QueueStats, float], Coroutine]) -> None: ...
```

---

## CrawlResult

Returned by `crawler.run()`:

```python
@dataclass
class CrawlResult:
    requests_done:        int
    requests_failed:      int
    requests_dead_letter: int
    elapsed_seconds:      float
    items_pushed:         int   # sum across ALL datasets (default + named)
```

`items_pushed` counts every `dataset.push()` call across all named datasets. If
two datasets each receive 100 rows, `items_pushed == 200`.

---

## run() — wait parameter

```python
result = await crawler.run(seeds, wait=False)
# Returns immediately after seeding the queue.
# Crawl runs in background tasks.
# __aexit__ (context manager exit) waits for completion.
```

`wait=False` is useful for incremental or interleaved crawls:

```python
async with Crawler(router=router) as crawler:
    await crawler.run(batch_one, wait=False)
    await crawler.run(batch_two, wait=False)   # both batches run concurrently
    # __aexit__ drains both
```

When `wait=False`, `run()` returns a `CrawlResult` stub with zeroed counters —
final counts are only available after the crawl completes (on context manager exit
or by awaiting the returned task).

---

## Complete Examples

### Basic scraper

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
    await ctx.enqueue_all([a.get("href") for a in links if a.get("href")])
    next_btn = await ctx.page.query_selector("li.next a")
    if next_btn:
        await ctx.enqueue(next_btn.get("href"))

async def main():
    async with Crawler(router=router, max_concurrency=3) as crawler:
        result = await crawler.run(["https://books.toscrape.com/"])
    await crawler.dataset.export_json("books.json")
    print(f"Done: {result.requests_done} pages, {result.items_pushed} books")

asyncio.run(main())
```

### With persistent queue and polite rate limiting

```python
async with Crawler(
    router=router,
    max_concurrency=2,
    queue="sqlite",                          # survive crashes
    rate_limit=RateLimitConfig(
        requests_per_second=0.5,             # 1 request every 2 seconds
        burst=1,
    ),
    storage_path=Path("./my_crawl"),
) as crawler:
    result = await crawler.run(seeds)
```

### Multi-dataset output with lifecycle hooks

```python
router = Router()

@router.before_request
async def add_auth_header(hctx):
    token = await hctx.store.get_json("auth/token")
    return dataclasses.replace(
        hctx.request,
        headers={**hctx.request.headers, "Authorization": f"Bearer {token}"},
    )

@router.after_request
async def on_failure(hctx):
    if hctx.error:
        await hctx.store.set_json(
            f"failures/{hctx.request.url}",
            {"error": str(hctx.error), "elapsed": hctx.elapsed},
        )

@router.on("https://example.com/products/**", label="product")
async def product_handler(ctx):
    products = ctx.get_dataset("products")
    reviews  = ctx.get_dataset("reviews")

    title = (await ctx.page.select("h1")).text
    await products.push({"title": title, "url": ctx.request.url})

    for review_el in await ctx.page.select_all(".review"):
        await reviews.push({"product_url": ctx.request.url, "text": review_el.text})

@router.on_error(BotBlockError)
async def on_block(ctx, error):
    ctx.log.warning("Blocked on %s (signal: %s)", ctx.request.url, error.signal)
    path = await ctx.page.save_screenshot(f"blocked_{hash(ctx.request.url)}.jpg")
    await ctx.store.set_json(f"blocked/{ctx.request.url}", {"screenshot": path})
    return ErrorAction.DEAD_LETTER

async with Crawler(router=router, max_concurrency=4) as crawler:
    await crawler.run(seeds)

await crawler.get_dataset("products").export_json("products.json")
await crawler.get_dataset("reviews").export_csv("reviews.csv")
```

### Re-enqueueing dead letters after fixing a handler

```python
async with Crawler(router=router, queue="sqlite") as crawler:
    await crawler.run(seeds)

print(f"Dead letters: {await crawler.dead_letter_count()}")

# Fix your handler, then:
async with Crawler(router=fixed_router, queue="sqlite") as crawler:
    async for req in crawler.dead_letters():
        await crawler.enqueue(
            dataclasses.replace(req, no_dedupe=True, retry_count=0)
        )
    await crawler.run([])   # empty seeds — process re-enqueued dead letters
```

---

## Configuration Reference

### RateLimitConfig

```python
@dataclass
class RateLimitConfig:
    requests_per_second: float = 1.0
    burst:               int   = 3
    per: Literal["netloc", "domain"] = "netloc"
```

### RetryPolicy

```python
@dataclass
class RetryPolicy:
    max_retries:     int   = 3
    backoff_base:    float = 2.0
    backoff_max:     float = 300.0
    backoff_jitter:  float = 0.1
    retry_on:        tuple[type[Exception], ...] = (NetworkError, SiteDownError, BrowserCrashError)
    no_retry_on:     tuple[type[Exception], ...] = (BotBlockError, StructureError)
```

### BrowserPoolConfig

```python
@dataclass
class BrowserPoolConfig:
    min_size:         int   = 1
    max_size:         int   = 5
    idle_timeout:     float = 60.0
    launch_timeout:   float = 30.0
    crash_max_retries: int  = 3
    headless:         bool  = True
    launch_args:      list[str] = field(default_factory=list)
```

### BackpressureConfig

```python
@dataclass
class BackpressureConfig:
    enabled:              bool  = True
    threshold_multiplier: int   = 100
    yield_interval:       float = 0.0
```

### ErrorAction

```python
from zencrawler import ErrorAction

class ErrorAction(Enum):
    RETRY       = "retry"        # re-queue with backoff (respects RetryPolicy)
    SKIP        = "skip"         # ack as done, no retry, no dead-letter
    DEAD_LETTER = "dead_letter"  # nack(retry=False)
    RAISE       = "raise"        # re-raise — crashes the crawl
```

---

## Import Structure

```python
# Core API
from zencrawler import Crawler, Request, Router

# Config dataclasses
from zencrawler import (
    BrowserPoolConfig,
    RateLimitConfig,
    RetryPolicy,
    BackpressureConfig,
)

# Error classes
from zencrawler import (
    CrawlError,
    NetworkError,
    NavigationError,
    TimeoutError,
    BotBlockError,
    StructureError,
    SiteDownError,
    BrowserCrashError,
    HandlerError,
    SkipRequest,
    UnhandledRequestError,
)

# Control flow
from zencrawler import ErrorAction

# Result types
from zencrawler import CrawlResult, QueueStats

# Storage protocols (for type hints)
from zencrawler import Dataset, Store

# Backend protocols (for custom implementations)
from zencrawler import QueueBackend, StorageBackend

# Testing utilities
from zencrawler.testing import FakePage, build_context, MemoryDataset, MemoryStore
```

---

## Environment Variable Configuration

All `Crawler` constructor parameters can be set via environment variables. The
`ZENCRAWLER_` prefix is used. Types are coerced from strings.

```bash
export ZENCRAWLER_MAX_CONCURRENCY=8
export ZENCRAWLER_QUEUE=sqlite
export ZENCRAWLER_STORAGE_PATH=/data/crawl
export ZENCRAWLER_PAGE_LOAD_TIMEOUT=45.0
export ZENCRAWLER_SHUTDOWN_TIMEOUT=60.0
export ZENCRAWLER_HEADLESS=true
```

Environment variables are applied as defaults — explicit constructor kwargs always
take precedence:

```python
# Uses env var max_concurrency=8 unless overridden
crawler = Crawler(router=router)

# Overrides env var
crawler = Crawler(router=router, max_concurrency=2)
```

Full env var → parameter mapping:

| Environment variable | Parameter | Type |
|---|---|---|
| `ZENCRAWLER_MAX_CONCURRENCY` | `max_concurrency` | `int` |
| `ZENCRAWLER_MAX_REQUESTS` | `max_requests` | `int` |
| `ZENCRAWLER_MAX_DEPTH` | `max_depth` | `int` |
| `ZENCRAWLER_QUEUE` | `queue` | `"memory"` or `"sqlite"` |
| `ZENCRAWLER_STORAGE_PATH` | `storage_path` | `Path` |
| `ZENCRAWLER_PAGE_LOAD_TIMEOUT` | `page_load_timeout` | `float` |
| `ZENCRAWLER_SHUTDOWN_TIMEOUT` | `shutdown_timeout` | `float` |
| `ZENCRAWLER_HEADLESS` | `browser.headless` | `bool` |
| `ZENCRAWLER_RATE_RPS` | `rate_limit.requests_per_second` | `float` |
| `ZENCRAWLER_RATE_BURST` | `rate_limit.burst` | `int` |

---

## Testing Utilities

See [`testing.md`](testing.md) for full documentation. Quick reference:

```python
from zencrawler.testing import FakePage, build_context

fake_page = FakePage(html="<h1>My Book</h1><p class='price_color'>£12.99</p>")
ctx = build_context(url="https://books.toscrape.com/catalogue/my-book.html", page=fake_page)

await book_page(ctx)

assert await ctx.dataset.count() == 1
rows = [r async for r in ctx.dataset.iter()]
assert rows[0]["title"] == "My Book"
```

---

## Logging

ZenCrawler uses the stdlib `logging` module. All library messages are under the
`zencrawler` namespace.

```
zencrawler              INFO — crawl start/stop, 30s stats summary
zencrawler.pool         DEBUG — browser checkout/return/crash
zencrawler.queue        DEBUG — push/pop/ack/nack transitions
zencrawler.router       DEBUG — handler dispatch
zencrawler.handler.<label>  INFO/ERROR — handler-level log via ctx.log
zencrawler.storage      DEBUG — flush events
```

To enable all debug output:
```python
import logging
logging.getLogger("zencrawler").setLevel(logging.DEBUG)
```

To silence everything except errors:
```python
logging.getLogger("zencrawler").setLevel(logging.ERROR)
```
