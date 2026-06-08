# Request Queue

Tracks what to crawl, ensures each URL is visited once, and manages retry state.
Users never interact with the queue directly — they call `ctx.enqueue()`.

---

## Backend Interface

```python
class QueueBackend(Protocol):
    async def push(self, request: Request) -> bool
    # Returns False if the URL was deduplicated (already seen).
    # Returns True if the request was accepted into pending.

    async def push_many(self, requests: Iterable[Request]) -> int
    # Returns count of requests actually enqueued (not deduplicated).

    async def pop(self) -> Request | None
    # Returns the next pending request, or None if the queue is empty.
    # Atomically moves the request to `processing` state.

    async def ack(self, request: Request) -> None
    # Mark a request as successfully completed. Moves to `done`.

    async def nack(
        self,
        request: Request,
        error: Exception,
        *,
        retry: bool = True,
    ) -> None
    # Mark a request as failed.
    # If retry=True and retry_count < max_retries: re-enqueue with backoff.
    # If retry=False or retry_count >= max_retries: move to `dead_letter`.

    async def peek_dead_letters(self) -> AsyncIterator[Request]
    # Iterate dead-lettered requests without removing them.

    async def stats(self) -> QueueStats
    # Returns current counts per state.

    async def close(self) -> None
    # Flush and release resources.
```

---

## Backends

### MemoryQueue (default)

```
Storage:
  pending:      asyncio.PriorityQueue[(priority, Request)]
  processing:   dict[str, Request]           # url_hash -> Request
  done:         set[str]                     # url_hashes
  dead_letter:  list[Request]
  seen:         set[str]                     # dedup: all ever accepted

Properties:
  - Zero I/O, lowest latency
  - Lost on process exit — no crash recovery
  - Memory grows with corpus size (mitigated: only hashes stored in `seen`, not full URLs)
  - Thread-safe within asyncio event loop (no locks needed)
```

### SqliteQueue (opt-in)

```
Storage: single file — crawl_state.db (in storage_path)

Tables:
  requests(
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    state       TEXT NOT NULL,          -- pending|processing|done|failed|dead_letter
    method      TEXT NOT NULL,
    headers     TEXT,                   -- JSON
    payload     BLOB,
    metadata    TEXT,                   -- JSON
    label       TEXT,
    depth       INTEGER,
    retry_count INTEGER,
    priority    INTEGER,
    enqueued_at REAL,                   -- Unix timestamp
    updated_at  REAL,
    next_retry_at REAL                  -- NULL unless state=failed awaiting retry
  )

Indexes:
  idx_state_priority ON requests(state, priority DESC, enqueued_at ASC)
  idx_next_retry     ON requests(next_retry_at) WHERE state = 'failed'

Journal mode: WAL (allows concurrent readers while writer is active)
```

**Crash recovery**: on startup, any `processing` rows older than `processing_timeout`
(default 5 minutes) are reset to `pending`. This handles the case where the process
died mid-crawl.

```python
UPDATE requests
SET state = 'pending', retry_count = retry_count + 1
WHERE state = 'processing'
  AND updated_at < (strftime('%s','now') - :processing_timeout)
```

---

## Deduplication

Applied at `push()` time. A request is silently dropped if its URL hash is
already in `seen` (regardless of current state — even `failed` or `dead_letter`).

Override with `Request(no_dedupe=True)` to bypass.

### Normalisation Algorithm

Applied to every URL before hashing:

```
1.  Parse URL into components (scheme, host, path, query, fragment)
2.  Lowercase scheme and host
3.  Remove default port (80 for http, 443 for https)
4.  Decode percent-encoded characters that don't need encoding
        (e.g. %41 → A, but %20 stays as + or %20 per context)
5.  Remove trailing slash from path
        UNLESS path is "/" (root) — keep that
6.  Sort query parameters alphabetically by key
7.  Remove known tracking parameters:
        utm_source, utm_medium, utm_campaign, utm_term, utm_content,
        fbclid, gclid, ref, source (configurable list)
8.  Remove empty query parameters
9.  Strip fragment entirely (#anchor)
10. SHA256(normalised_url)[:16 bytes] → hex string (32 chars)
```

The tracking parameter strip list is configurable:
```python
Crawler(dedupe_strip_params=["ref", "source", "sid"])
```

Set to `[]` to disable stripping entirely.

### Collision probability

16-byte (128-bit) prefix of SHA256. With 10 million URLs the collision probability
is ~1.47 × 10⁻²³ — negligible. If correctness is critical at extreme scale, set
`dedupe_hash_bytes=32` in config.

---

## State Machine

```
                    push()
                      │
              ┌───────▼──────┐
              │   pending    │◄──────────────────────────┐
              └───────┬──────┘                           │
                      │ pop()                            │
              ┌───────▼──────┐                           │
              │  processing  │                           │
              └───┬──────┬───┘                           │
                  │      │                               │
               ack()   nack(retry=True)                  │
                  │      │                               │
         ┌────────▼┐   ┌─▼──────────┐                   │
         │  done   │   │   failed   │──backoff delay──►  │  retry_count < max_retries
         └─────────┘   └─────┬──────┘                   │
                             │                           │
                    retry_count >= max_retries            │
                             │        or nack(retry=False)
                     ┌───────▼───────┐
                     │  dead_letter  │
                     └───────────────┘
```

Transitions are atomic in the SQLite backend (single UPDATE statement per transition).

---

## Retry Policy

```python
@dataclass
class RetryPolicy:
    max_retries:     int   = 3
    backoff_base:    float = 2.0     # delay = backoff_base ^ retry_count (seconds)
    backoff_max:     float = 300.0   # cap at 5 minutes
    backoff_jitter:  float = 0.1     # multiply delay by uniform(1-j, 1+j) to avoid thundering herd

    retry_on: tuple[type[Exception], ...] = (
        NetworkError,
        SiteDownError,
        BrowserCrashError,
    )
    no_retry_on: tuple[type[Exception], ...] = (
        BotBlockError,
        StructureError,
    )
```

Delay formula:
```python
delay = min(backoff_base ** retry_count, backoff_max)
delay *= uniform(1 - backoff_jitter, 1 + backoff_jitter)
```

Retries 1–3 for default config: ~2s, ~4s, ~8s (with jitter).

The `retry_on` / `no_retry_on` lists are consulted in order. If the exception
matches both, `no_retry_on` wins. If neither, the default is `retry=True`.

---

## Dead-Letter Handling

Dead-lettered requests are never silently discarded.

- **MemoryQueue**: stored in `dead_letter: list[Request]`
- **SqliteQueue**: stored as `state = 'dead_letter'` rows

After a run:
```python
async with Crawler(...) as crawler:
    await crawler.run(seeds)

async for req in crawler.dead_letters():
    print(req.url, req.metadata)

print(f"Dead letters: {await crawler.dead_letter_count()}")
```

Users can re-enqueue dead letters with `no_dedupe=True` after fixing a handler:
```python
async for req in crawler.dead_letters():
    await crawler.enqueue(dataclasses.replace(req, no_dedupe=True, retry_count=0))
```

---

## QueueStats

```python
@dataclass
class QueueStats:
    pending:     int
    processing:  int
    done:        int
    failed:      int      # awaiting retry
    dead_letter: int
    total_seen:  int      # unique URLs ever accepted (includes done + dead_letter)

    @property
    def depth(self) -> int:
        return self.pending + self.processing
```

Accessible via `crawler.queue_stats()` at any time during or after a run.
Emitted to the log at `INFO` level every 30 seconds during active crawls.
