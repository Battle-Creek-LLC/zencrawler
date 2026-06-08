# Concurrency & Rate Limiting

## Concurrency

`max_concurrency` controls how many pages are loaded simultaneously. Each concurrent request occupies one browser from the pool.

```python
async with Crawler(
    router=router,
    max_concurrency=5,   # up to 5 Chrome tabs active at once
) as crawler:
    await crawler.run(seeds)
```

The default is `5`, or the value of the `ZENCRAWLER_MAX_CONCURRENCY` environment variable.

### Choosing a value

| Scenario | Suggested `max_concurrency` |
|---|---|
| Laptop, development | 2–3 |
| Server, polite crawling | 3–5 |
| Server, target permits it | 8–16 |
| Single-site with rate limiting | Match rate limit × avg page load time |

Each concurrent browser uses ~100–200 MB of RAM and a Chrome process. On a machine with 4 GB available, a concurrency of 10–15 is reasonable.

---

## Rate limiting

Rate limiting caps requests per second independently of concurrency. A token-bucket algorithm allows short bursts and then enforces the average rate.

### Via environment variables

```bash
export ZENCRAWLER_RATE_RPS=2.0    # 2 requests per second
export ZENCRAWLER_RATE_BURST=5    # allow bursts of up to 5
python crawl.py
```

`ZENCRAWLER_RATE_BURST` defaults to `max(1, int(rate_rps))` if not set.

### Via code

Rate limiting is configured through environment variables in the current version. Set them programmatically before instantiating the Crawler if needed:

```python
import os
os.environ["ZENCRAWLER_RATE_RPS"] = "1.5"

async with Crawler(router=router) as crawler:
    await crawler.run(seeds)
```

---

## Environment variable reference

All Crawler parameters have environment variable equivalents:

| Variable | Parameter | Default |
|---|---|---|
| `ZENCRAWLER_MAX_CONCURRENCY` | `max_concurrency` | `5` |
| `ZENCRAWLER_MAX_REQUESTS` | `max_requests` | `None` (unlimited) |
| `ZENCRAWLER_MAX_DEPTH` | `max_depth` | `None` (unlimited) |
| `ZENCRAWLER_QUEUE` | `queue` | `"memory"` |
| `ZENCRAWLER_STORAGE_PATH` | `storage_path` | `"./crawl_data"` |
| `ZENCRAWLER_PAGE_LOAD_TIMEOUT` | `page_load_timeout` | `30.0` |
| `ZENCRAWLER_SHUTDOWN_TIMEOUT` | `shutdown_timeout` | `30.0` |
| `ZENCRAWLER_HEADLESS` | (BrowserPoolConfig) | `true` |
| `ZENCRAWLER_RATE_RPS` | (token bucket) | None (no limit) |
| `ZENCRAWLER_RATE_BURST` | (token bucket) | `max(1, rate_rps)` |

Code arguments take precedence over environment variables.

---

## Capping the crawl

### By request count

```python
async with Crawler(router=router, max_requests=500) as crawler:
    await crawler.run(seeds)
```

The crawl stops once 500 requests have been completed (including retries and dead-letters).

### By depth

```python
async with Crawler(router=router, max_depth=3) as crawler:
    await crawler.run(seeds)
```

Requests with `depth > max_depth` are dropped before queuing. Depth starts at `0` for seed requests; `ctx.enqueue()` increments by `depth_offset` (default `1`).

---

## Graceful shutdown

Sending `SIGTERM` or `SIGINT` (Ctrl+C) triggers graceful shutdown:

1. The scheduler stops accepting new requests from the queue.
2. In-flight handlers are given `shutdown_timeout` seconds to complete.
3. All browsers are closed cleanly.
4. `run()` returns the partial `CrawlResult`.

```python
async with Crawler(
    router=router,
    shutdown_timeout=60.0,  # wait up to 60s for active pages to finish
) as crawler:
    result = await crawler.run(seeds)
```

---

## SQLite queue for long crawls

The default in-memory queue loses state on crash. For crawls over ~1,000 pages, switch to SQLite:

```python
# pip install "zencrawler[sqlite]"

async with Crawler(
    router=router,
    queue="sqlite",
    storage_path="./crawl_data",
    max_requests=10_000,
) as crawler:
    await crawler.run(seeds)
```

!!! tip
    ZenCrawler logs a warning if you crawl more than 1,000 pages with the memory queue. It's a reminder, not an error.

The SQLite queue automatically recovers stale `processing` rows on startup — any request that was mid-flight when the process died is re-queued on next run.
