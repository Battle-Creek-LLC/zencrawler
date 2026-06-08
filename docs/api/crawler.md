# Crawler

The main orchestrator. Manages browser pool, request queue, storage, concurrency, and signal handling.

## Constructor

```python
class Crawler:
    def __init__(
        self,
        router: Router,
        max_concurrency: int | None = None,
        max_requests: int | None = None,
        max_depth: int | None = None,
        queue: Literal["memory", "sqlite"] | QueueBackend | None = None,
        retry_policy: RetryPolicy | None = None,
        storage: Literal["sqlite"] | StorageBackend = "sqlite",
        storage_path: Path | None = None,
        page_load_timeout: float | None = None,
        bot_signals: list[str] | None = None,
        extra_bot_signals: list[str] = [],
        dedupe_strip_params: list[str] | None = None,
        shutdown_timeout: float | None = None,
    ) -> None
```

### Parameters

`router` *(required)*
:   The `Router` instance that dispatches requests to handler functions.

`max_concurrency`
:   Maximum number of pages loaded simultaneously. Each slot is one Chrome browser.
    Default: `5` or `$ZENCRAWLER_MAX_CONCURRENCY`.

`max_requests`
:   Stop the crawl after this many requests complete (including retries). `None` means unlimited.
    Default: `None` or `$ZENCRAWLER_MAX_REQUESTS`.

`max_depth`
:   Drop requests whose `depth` exceeds this value before queuing. `None` means unlimited.
    Default: `None` or `$ZENCRAWLER_MAX_DEPTH`.

`queue`
:   Queue backend. `"memory"` (default) is fast but loses state on crash. `"sqlite"` persists to disk and recovers on restart. Pass a custom `QueueBackend` implementation to use a different backend.
    Default: `"memory"` or `$ZENCRAWLER_QUEUE`.

`retry_policy`
:   A `RetryPolicy` controlling retries and back-off. See [Configuration](config.md#retrypolicy).
    Default: `RetryPolicy()` (3 retries, base 2s, max 300s).

`storage`
:   Storage backend. `"sqlite"` (default) writes datasets and stores to a SQLite file.
    Pass a custom `StorageBackend` implementation for other backends.

`storage_path`
:   Directory for SQLite files. Created if it doesn't exist.
    Default: `./crawl_data` or `$ZENCRAWLER_STORAGE_PATH`.

`page_load_timeout`
:   Seconds to wait for a page to load before raising `TimeoutError`.
    Default: `30.0` or `$ZENCRAWLER_PAGE_LOAD_TIMEOUT`.

`bot_signals`
:   Replace the built-in list of bot-block body text signals entirely.
    Omit to keep the defaults; use `extra_bot_signals` to add to them.

`extra_bot_signals`
:   Additional body text strings that indicate a bot block. Added to the built-in list.
    Default: `[]`.

`dedupe_strip_params`
:   Query parameters stripped before URL hashing for deduplication.
    Default: `["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "source"]`.

`shutdown_timeout`
:   Seconds to wait for in-flight requests to finish during graceful shutdown (SIGTERM/SIGINT).
    Default: `30.0` or `$ZENCRAWLER_SHUTDOWN_TIMEOUT`.

---

## Context manager

`Crawler` is an async context manager. Use it with `async with` to ensure browsers and storage are always closed cleanly:

```python
async with Crawler(router=router, max_requests=100) as crawler:
    result = await crawler.run(seeds)
    # access crawler.dataset here, before the block exits
```

Storage is closed when the block exits. Accessing datasets or stores after the block raises `RuntimeError`.

---

## Methods

### `run(requests, *, wait=True)`

```python
async def run(
    self,
    requests: Iterable[Request | str],
    *,
    wait: bool = True,
) -> CrawlResult
```

Seed the queue with `requests` and start crawling.

- `requests` — an iterable of `Request` objects or URL strings. Strings are converted to `Request(url=url)`.
- `wait` — if `False`, returns immediately after seeding (the crawl continues in the background). Use the `Crawler` context manager to wait for completion.

Returns a [`CrawlResult`](results.md#crawlresult) when the crawl finishes or is stopped.

### `enqueue(request)`

```python
async def enqueue(self, request: Request | str) -> bool
```

Add a single request to the queue. Returns `True` if the URL was newly queued, `False` if it was already seen (deduplicated).

Can be called before or during a crawl.

### `enqueue_all(requests)`

```python
async def enqueue_all(self, requests: Iterable[Request | str]) -> int
```

Batch-enqueue multiple requests. Returns the count of newly queued URLs.

### `queue_stats()`

```python
async def queue_stats(self) -> QueueStats
```

Returns current queue depth and counters. See [`QueueStats`](results.md#queuestats).

### `dead_letters()`

```python
async def dead_letters(self) -> AsyncIterator[Request]
```

Async iterator over all dead-lettered requests.

```python
async for req in crawler.dead_letters():
    print(req.url, req.retry_count)
```

### `on_stats(callback)`

```python
def on_stats(self, callback: Callable[[CrawlResult], None]) -> None
```

Register a callback that receives a `CrawlResult` snapshot periodically during the crawl. The callback may be sync or async.

```python
crawler.on_stats(lambda r: print(f"done={r.requests_done} items={r.items_pushed}"))
```

---

## Properties

`crawler.dataset`
:   The default `Dataset`. Must be accessed inside the `async with` block.

`crawler.store`
:   The default `Store`. Must be accessed inside the `async with` block.

`crawler.get_dataset(name)`
:   Returns the named `Dataset`.

`crawler.get_store(name)`
:   Returns the named `Store`.
