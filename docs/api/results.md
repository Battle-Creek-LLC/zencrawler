# Results & Stats

## CrawlResult

Returned by `crawler.run()`. Summarises the outcome of the crawl.

```python
@dataclass
class CrawlResult:
    requests_done:        int   = 0
    requests_failed:      int   = 0
    requests_dead_letter: int   = 0
    elapsed_seconds:      float = 0.0
    items_pushed:         int   = 0
```

### Fields

`requests_done`
:   Number of requests completed successfully (handler ran without unhandled error).

`requests_failed`
:   Number of requests that failed and were retried (cumulative retry count).

`requests_dead_letter`
:   Number of requests that exhausted retries or were explicitly dead-lettered.

`elapsed_seconds`
:   Wall-clock time from `crawler.run()` call to completion.

`items_pushed`
:   Total items pushed to the **default** dataset during the crawl.
    Safe to read after the `async with` block exits.

!!! warning "Use `result.items_pushed`, not `await crawler.dataset.count()`"
    `items_pushed` is tracked during the crawl and is always readable after `run()` returns,
    even after the `async with` block exits and storage is closed.
    Calling `await crawler.dataset.count()` outside the `async with` block raises `RuntimeError`.

### Example

```python
async with Crawler(router=router, max_requests=100) as crawler:
    result = await crawler.run(seeds)

# Safe to read after the block
print(f"Pages:      {result.requests_done}")
print(f"Failures:   {result.requests_failed}")
print(f"Dead-letter:{result.requests_dead_letter}")
print(f"Items:      {result.items_pushed}")
print(f"Time:       {result.elapsed_seconds:.1f}s")
```

---

## QueueStats

A snapshot of queue state, returned by `await crawler.queue_stats()`.

```python
@dataclass
class QueueStats:
    pending:       int = 0
    processing:    int = 0
    done:          int = 0
    failed:        int = 0
    dead_letter:   int = 0
    total_seen:    int = 0
```

### Fields

`pending`
:   Requests queued but not yet started.

`processing`
:   Requests currently being handled.

`done`
:   Requests completed successfully.

`failed`
:   Requests that failed (retried or dead-lettered).

`dead_letter`
:   Requests in the dead-letter queue.

`total_seen`
:   Total unique URLs ever queued (used for deduplication tracking).

### Properties

`depth` *(property)*
:   `pending + processing` — the number of requests still in flight or waiting.

### Example

```python
async with Crawler(router=router) as crawler:
    await crawler.enqueue("https://example.com/")
    stats = await crawler.queue_stats()
    print(f"Pending: {stats.pending}, Total seen: {stats.total_seen}")
```

---

## ErrorAction

Returned from `@router.on_error` hooks to control what happens after an error.

```python
from zencrawler import ErrorAction

class ErrorAction(Enum):
    RETRY       = "retry"
    SKIP        = "skip"
    DEAD_LETTER = "dead_letter"
    RAISE       = "raise"
```

| Value | Effect |
|---|---|
| `RETRY` | Re-queue the request with `retry_count + 1`. Subject to `RetryPolicy.max_retries`. |
| `SKIP` | Discard silently — not counted as failure. |
| `DEAD_LETTER` | Move to dead-letter queue — inspectable via `crawler.dead_letters()`. |
| `RAISE` | Re-raise the exception, stopping the crawl immediately. |
