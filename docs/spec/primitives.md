# Core Primitives

The five first-class types. Users interact with all of them; the library owns their
lifecycles.

---

## Overview

| Primitive | Role | Mutable? | Lifetime |
|---|---|---|---|
| `Request` | Crawl unit — URL + metadata envelope | No | Enqueue → ack/nack |
| `CrawlContext` | Live handler scope — page + injected deps | Yes (deps only) | Handler call |
| `HookContext` | Narrower scope for lifecycle hooks (no page) | No | Hook call |
| `Router` | Dispatch table + handler registry | Yes (setup phase) | Application |
| `Dataset` | Append-only structured output | Append only | Application |
| `Store` | Key-value blob storage | Full CRUD | Application |

---

## Request

Immutable value object. Created by users and by handlers (via `ctx.enqueue`).
Identity is determined by a hash of the normalised URL — see
[`request-queue.md`](request-queue.md) for normalisation details.

```python
@dataclass(frozen=True)
class Request:
    url:          str
    method:       str                    = "GET"
    headers:      dict[str, str]         = field(default_factory=dict)
    payload:      bytes | None           = None          # POST body
    metadata:     dict[str, Any]         = field(default_factory=dict)
    label:        str | None             = None          # router hint
    depth:        int                    = 0
    retry_count:  int                    = 0
    priority:     int                    = 0             # higher = sooner (future)
    no_dedupe:    bool                   = False         # force re-crawl
```

**`metadata`** passes arbitrary user data through the queue into the handler.
It is not inspected by the library. Common uses: parent URL, category tag,
pagination state.

When a handler calls `ctx.enqueue(url, inherit_metadata=True)` (the default),
the parent request's metadata dict is shallow-merged with any explicitly passed
`metadata` kwarg. The child request is then immutable. Keys from the explicit
`metadata` kwarg override parent keys on conflict.

```python
# Parent request has metadata={"site": "example", "category": "books"}
await ctx.enqueue("/next", metadata={"page": 2})
# Child metadata → {"site": "example", "category": "books", "page": 2}
```

**`label`** is a plain string. The router can match on it directly, which avoids
encoding routing intent into the URL itself. Common pattern: use a small set of
labels (`"listing"`, `"product"`, `"api"`) as an explicit routing table rather
than relying on URL shape, which can be fragile.

**`no_dedupe`** bypasses the URL hash check. Useful for periodic re-crawls of the
same URL with different expected content. Note: if `no_dedupe=True` is passed,
every call to `enqueue` will add the URL to the queue regardless of whether it has
been visited before. Use sparingly to avoid duplicate processing.

**`Request.retry_count`** is managed by the queue backend, not by user code.
It is incremented automatically by `nack(retry=True)`. When constructing a
`Request` manually (e.g. from dead letters), reset it to `0` to give the request
a fresh retry budget.

**What `Request` does NOT own:**
- The browser page (that lives in `BrowserPool`)
- Queue position or state (managed by `RequestQueue`)
- Storage references

---

## CrawlContext

Created by the `Crawler` for each request. Injected into the matched handler.
Discarded after the handler returns (or raises).

```python
@dataclass
class CrawlContext:
    page:      zendriver.Tab
    request:   Request
    crawler:   Crawler                  # narrow interface — enqueue only
    dataset:   Dataset                  # default dataset
    store:     Store                    # default store
    log:       logging.Logger           # namespaced: "zencrawler.handler.<label>"

    # Convenience wrappers around crawler.enqueue()
    async def enqueue(
        self,
        url: str | Request,
        *,
        label:    str | None       = None,
        metadata: dict | None      = None,
        depth_offset: int          = 1,
    ) -> bool: ...                      # False if deduplicated

    async def enqueue_all(
        self,
        urls: Iterable[str | Request],
        **kwargs,
    ) -> int: ...                       # count actually enqueued

    # Named storage access
    def get_dataset(self, name: str) -> Dataset: ...
    def get_store(self, name: str) -> Store: ...
```

`CrawlContext.crawler` exposes **only** the enqueue interface — not `run()`,
`shutdown()`, or internal queue access. This prevents handlers from taking
actions with global consequences.

**`page`** is a live `zendriver.Tab`. Handlers interact with it directly using
zendriver's native API. ZenCrawler does not wrap or proxy it. Key methods:

```python
# Navigation
await ctx.page.get("https://example.com")          # navigate to URL

# Element selection (waits up to 10s, raises if not found)
el  = await ctx.page.select("h1")
els = await ctx.page.select_all(".item")

# Element selection (immediate, no wait, returns None if not found)
el  = await ctx.page.query_selector(".optional")
els = await ctx.page.query_selector_all(".optional")

# Text-based search
el  = await ctx.page.find("Sign in")

# Element data (properties — no await)
el.text           # visible text content
el.text_all       # all text including hidden
el.attrs          # dict of all HTML attributes
el.get("href")    # single attribute, returns None if missing
el.get_html()     # outer HTML string

# Page-level (properties — no await)
ctx.page.title    # page <title>
ctx.page.url      # current URL (may differ from request.url after redirects)

# JavaScript
result = await ctx.page.evaluate("document.querySelectorAll('a').length")
html   = await ctx.page.get_content()              # full page HTML

# Screenshot (saves to file, returns filename path)
path = await ctx.page.save_screenshot("out.jpg")
```

See [zendriver documentation](https://github.com/stephanlensky/zendriver) for the
complete API reference.

**What `CrawlContext` does NOT own:**
- The browser process (returned to pool after handler exits)
- Queue state (read-only view via `enqueue`)
- Storage backend connections (borrowed references)

---

## Router

Holds the handler registry. Configured at startup; treated as read-only during
a crawl run.

```python
class Router:
    # Handler registration — see router.md for full matching rules
    def on(
        self,
        pattern: str | None          = None,
        *,
        domain:    str | None        = None,
        predicate: Callable | None   = None,
        label:     str | None        = None,
    ) -> Callable: ...               # decorator factory

    @property
    def default(self) -> Callable: ...  # decorator for fallback handler

    # Error hooks — see error-handling.md
    def on_error(self, *error_types: type[Exception]) -> Callable: ...

    # Lifecycle hooks — see router.md
    def before_request(self) -> Callable: ...
    def after_request(self) -> Callable: ...
```

**What `Router` does NOT own:**
- Queue state
- Browser pool
- Storage backends
- Concurrency control

---

## HookContext

A narrower version of `CrawlContext` passed to lifecycle hooks. Deliberately
excludes `page` (not yet acquired in `before_request`) and the enqueue interface
(hooks should not add work to the queue — that is a handler responsibility).

```python
@dataclass(frozen=True)
class HookContext:
    request:  Request
    log:      logging.Logger        # namespaced: "zencrawler.hook"
    store:    Store                 # default store — for cross-request state
```

The `after_request` hook receives an extended version:

```python
@dataclass(frozen=True)
class AfterHookContext(HookContext):
    page:    zendriver.Tab          # still live — context not yet closed
    error:   Exception | None       # None if handler succeeded
    elapsed: float                  # wall-clock seconds for handler + navigation
```

`HookContext` is immutable. If `before_request` needs to modify the request (e.g.
add headers), it returns a new `Request` object — the library swaps it in for
the current crawl cycle without touching the queue entry.

See [`router.md`](router.md) for full hook registration and behaviour.

---

## Dataset

Append-only. Rows are arbitrary dicts. No schema enforcement at the library level.

```python
class Dataset(Protocol):
    name: str

    async def push(self, item: dict[str, Any]) -> None
    async def push_many(self, items: Iterable[dict[str, Any]]) -> None

    def iter(self) -> AsyncIterator[dict[str, Any]]
    async def count(self) -> int

    async def export_json(self, path: Path, *, lines: bool = False) -> None
    async def export_csv(self, path: Path) -> None

    async def clear(self) -> None     # destructive — use with care
```

The default dataset is named `"default"`. Named datasets are accessed via
`ctx.get_dataset("name")` and are created on first use.

Rows are flushed to the backend in batches (see [`storage.md`](storage.md)).
`push()` is safe to call concurrently from multiple handlers.

---

## Store

Key-value blob storage. Values are raw bytes; JSON helpers are provided.

```python
class Store(Protocol):
    name: str

    async def get(self, key: str) -> bytes | None
    async def set(self, key: str, value: bytes) -> None
    async def delete(self, key: str) -> None
    async def exists(self, key: str) -> bool
    async def keys(self, prefix: str = "") -> list[str]

    # JSON convenience (serialize/deserialize with stdlib json)
    async def get_json(self, key: str) -> Any | None
    async def set_json(self, key: str, value: Any) -> None

    async def clear(self) -> None
```

Common uses:
- Cache already-scraped URLs beyond dedup (e.g. "did we get a clean result?")
- Persist pagination cursors across a crawl
- Store intermediate state between handler calls
- Record bot-block events for post-run analysis

---

## Relationship Diagram

```
User code
  │
  ├── creates ──► Request(url, metadata, label)
  │                    │
  │                    ▼
  │              RequestQueue
  │                    │  pops pending
  │                    ▼
  │              Crawler.run() loop
  │                    │  checks out browser
  │                    ▼
  │              BrowserPool ──► zendriver.Tab (page)
  │                    │
  │                    ▼
  │              CrawlContext(page, request, dataset, store, log)
  │                    │
  │                    ▼
  ├── receives ──► Router.dispatch(context)
  │                    │  matches handler
  │                    ▼
  └── writes ◄── user handler fn
                       │
                       ├── ctx.dataset.push(...)
                       ├── ctx.store.set(...)
                       └── ctx.enqueue(url)
```
