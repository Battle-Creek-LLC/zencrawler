# Concurrency & Rate Limiting

Controls how many requests run simultaneously and how fast requests are sent to
any given domain. All control happens in the `Crawler` scheduling loop.

---

## Global Concurrency

A single `asyncio.Semaphore(max_concurrency)` gates browser checkout. One semaphore
slot = one in-flight browser context = one in-flight handler.

```
max_concurrency = 5   →   at most 5 Chrome contexts open simultaneously
```

The semaphore is acquired before checkout and released after the handler returns
(and the context is closed). It is never held across `await` gaps outside of the
handler itself — no deadlock risk.

**Choosing `max_concurrency`:**

| Environment | Suggested value | Notes |
|---|---|---|
| Development laptop | 2–3 | Keeps machine responsive |
| CI container (2 CPU) | 2–4 | Watch RAM: ~200MB per Chrome process |
| Production container (8 CPU) | 6–12 | RAM is usually the bottleneck |
| Polite crawl of public sites | 1–2 + rate limit | Respect robots.txt spirit |

Rule of thumb: RAM budget ÷ 250MB ≈ safe `max_concurrency`.

---

## Per-Domain Rate Limiting

Token bucket algorithm. One bucket per domain key (see bucketing below).

```python
@dataclass
class RateLimitConfig:
    requests_per_second: float = 1.0
    burst:               int   = 3      # tokens available immediately at start
    per:  Literal["netloc", "domain"] = "netloc"
    # "netloc"  → blog.example.com and shop.example.com have separate buckets
    # "domain"  → both share one bucket (eTLD+1, stdlib-only approximation)
```

**Token bucket behaviour:**

```
bucket capacity = burst
initial tokens  = burst
refill rate     = requests_per_second tokens/second

acquire():
  wait until tokens >= 1
  tokens -= 1
```

The bucket is non-blocking to the event loop — `acquire()` is:
```python
async def acquire(self):
    while self._tokens < 1.0:
        await asyncio.sleep(self._next_refill_in())
    self._tokens -= 1.0
```

**Domain bucketing — `per="domain"` (eTLD+1):**

Accurate eTLD+1 (e.g. `co.uk`, `com.au`) requires a Public Suffix List. Without
an external dep, ZenCrawler uses a heuristic:

```python
def _domain_key(netloc: str) -> str:
    parts = netloc.split(".")
    if len(parts) <= 2:
        return netloc          # example.com → example.com
    # Heuristic: last two parts are TLD+1 unless second-to-last is 2 chars
    # (catches .co.uk, .com.au, .org.nz, etc.)
    if len(parts[-2]) <= 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
```

This covers ~95% of real-world cases. For full PSL accuracy, install the optional
`tldextract` package — the library detects and uses it automatically if present.

**Rate limiting is applied after the semaphore is acquired**, so a slow domain
does not block other domains from using their browser slots. The wait happens
with the semaphore held — this is intentional: it back-pressures the scheduler
without blocking the event loop.

Rate limiting can be disabled per-handler:
```python
@router.on("https://example.com/api/**", rate_limit=None)
async def api_handler(ctx): ...
```

Or configured per-domain:
```python
Crawler(
    rate_limits={
        "example.com":       RateLimitConfig(requests_per_second=2.0),
        "slow-site.com":     RateLimitConfig(requests_per_second=0.2),
        "*":                 RateLimitConfig(requests_per_second=1.0),  # default
    }
)
```

---

## Scheduling Loop

```python
async def _run_loop(self):
    while not self._should_stop():
        request = await self._queue.pop()

        if request is None:
            if self._active_count == 0:
                break                          # queue empty, no in-flight → done
            await asyncio.sleep(0.1)           # yield, check again shortly
            continue

        await self._semaphore.acquire()        # blocks if at max_concurrency
        await self._rate_limiter.acquire(request.url)  # blocks for domain rate

        task = asyncio.create_task(
            self._run_handler(request),
            name=f"handler:{request.url}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
```

`_should_stop()` returns `True` when:
- Shutdown was requested (SIGTERM / context manager exit), OR
- `max_requests` limit reached (optional config)

`_run_handler(request)` handles browser checkout, handler dispatch, ack/nack,
and browser return. It never raises — all exceptions are caught and routed.

---

## Backpressure

When handlers enqueue URLs faster than they drain, the queue grows unboundedly.
Left unchecked on large sites this causes OOM.

```python
@dataclass
class BackpressureConfig:
    enabled:              bool  = True
    threshold_multiplier: int   = 100   # engage at queue.depth > max_concurrency * N
    yield_interval:       float = 0.0   # asyncio.sleep() seconds per enqueue call
    # yield_interval=0.0 means asyncio.sleep(0) — cooperative yield only
    # yield_interval=0.1 means 10 enqueues/second max per handler
```

When `queue.depth > max_concurrency * threshold_multiplier`:
- `ctx.enqueue()` calls `await asyncio.sleep(yield_interval)` before returning
- This gives the scheduler a chance to drain the queue before more work is added
- It does NOT block — other handlers continue running

Backpressure is cooperative. A handler doing a tight `for url in 10_000_urls`
loop will still enqueue all of them, just more slowly. This is by design — hard
blocking would risk deadlock (handler holds semaphore slot, waits on queue drain,
but queue can't drain because no free slots).

Disable with `BackpressureConfig(enabled=False)` for crawls with known, finite
URL sets.

---

## Concurrency Guards

### `max_requests` limit

```python
Crawler(max_requests=1000)
```

After 1000 requests complete (acked or dead-lettered), the scheduler stops
dequeuing new requests and waits for in-flight handlers to finish. Queue state
is preserved (SQLite backend) or discarded (memory backend).

Useful for: test runs, budget-constrained crawls, incremental crawls.

### `max_depth` limit

```python
Crawler(max_depth=3)
```

Requests with `depth > max_depth` are silently dropped at enqueue time.
`Request.depth` is automatically incremented by 1 on each `ctx.enqueue()` call
(relative to the parent request's depth).

---

## SIGTERM and Graceful Shutdown

ZenCrawler registers handlers for `SIGTERM` and `SIGINT` (Ctrl-C) when the
`Crawler` context manager is entered:

```python
async with Crawler(router=router, shutdown_timeout=30.0) as crawler:
    await crawler.run(seeds)
# On SIGTERM/SIGINT: complete in-flight handlers (up to shutdown_timeout),
# flush storage, close browsers, exit cleanly.
```

Shutdown sequence on signal:
```
1. Signal received → set _shutting_down = True
2. Stop dequeuing new requests
3. Wait up to shutdown_timeout for in-flight handlers to complete
4. If timeout exceeded: cancel remaining handler tasks, log count
5. Flush all dataset write buffers
6. Close all browser contexts and processes
7. Close queue and storage backends
8. Re-raise KeyboardInterrupt (SIGINT) or exit(0) (SIGTERM)
```

**SQLite queue recovery after abrupt exit:** If the process is killed hard
(`kill -9`) or crashes before shutdown completes, in-flight requests remain in
`state='processing'`. On the next Crawler startup with `queue="sqlite"`, requests
stuck in `processing` for longer than `processing_timeout` (default 5 minutes)
are automatically reset to `pending`. See [`request-queue.md`](request-queue.md).

**Docker:** Ensure `STOPSIGNAL SIGTERM` in your Dockerfile (the default). Allow
sufficient stop grace period:

```dockerfile
STOPSIGNAL SIGTERM
```

```yaml
# docker-compose.yml
services:
  crawler:
    stop_grace_period: 45s   # must exceed shutdown_timeout
```

---

## Resource Sizing Guide

| Available RAM | Suggested `max_concurrency` | Notes |
|---|---|---|
| 1 GB | 2–3 | Tight — use `--disable-gpu` in launch_args |
| 2 GB | 3–5 | Comfortable laptop config |
| 4 GB | 6–10 | Standard container |
| 8 GB | 12–20 | Memory is not the bottleneck; watch CPU |
| 16 GB+ | 20–40 | CPU and rate limits become the bottleneck |

Rule of thumb: `max_concurrency ≈ available_RAM_GB × 3`.

Each Chrome process uses approximately 150–300 MB depending on page complexity.
Pages with heavy JavaScript or large DOM trees (news sites, SPAs) push toward the
upper bound.

**CPU:** Each Chrome process runs JavaScript on a single thread. At high
concurrency on a low-CPU machine, processes compete for CPU time, causing
navigation timeouts. If you see frequent `TimeoutError`, reduce concurrency
before increasing `page_load_timeout`.

**Monitoring during a run:**

```bash
# Watch Chrome memory usage
watch -n 2 "ps aux | grep chrome | awk '{sum += \$6} END {print sum/1024 \" MB\"}'"

# ZenCrawler log stats line every 30s (stdout if logging configured)
tail -f crawl.log | grep "zencrawler"
```

---

## Observability

The scheduler emits `INFO`-level summaries every 30 seconds:

```
[INFO] zencrawler: pending=142 processing=5 done=893 dead_letter=2 elapsed=45s rate=19.8/min
```

`DEBUG`-level logs every state transition (high volume — use only for debugging).

**Programmatic stats callback** (fires every 30 seconds):

```python
async def print_stats(stats: QueueStats, elapsed: float) -> None:
    print(f"[{elapsed:.0f}s] done={stats.done} pending={stats.pending}")

crawler.on_stats(print_stats)
```

The callback is `async def`. `elapsed` is wall-clock seconds since `run()` was
called.

[SCOPE RISK]: Prometheus / OpenTelemetry integration is out of scope for v1.
Use `on_stats` as the extension point for custom metric emission.
