# Router

The primary user-facing API. Holds the handler registry and dispatches each
`CrawlContext` to the correct handler. Also owns lifecycle hooks.

---

## Handler Registration

Four registration mechanisms, combinable:

### 1. URL glob pattern

```python
@router.on("https://example.com/products/**")
async def product_handler(ctx: CrawlContext) -> None: ...
```

Glob syntax:
- `*` — matches any characters except `/`
- `**` — matches any characters including `/`
- `?` — matches exactly one character

Examples:
```
"https://example.com/blog/**"         matches /blog/2024/post-title
"https://example.com/page/*/edit"     matches /page/42/edit, not /page/42/sub/edit
"https://*.example.com/**"            matches any subdomain
```

### 2. Domain glob

```python
@router.on(domain="books.toscrape.com")
async def site_handler(ctx: CrawlContext) -> None: ...

@router.on(domain="*.example.com")
async def subdomain_handler(ctx: CrawlContext) -> None: ...
```

Matches against the URL's `netloc`. Does not inspect path or scheme.

### 3. Label match

```python
@router.on(label="product")
async def labeled_handler(ctx: CrawlContext) -> None: ...
```

Matches `Request.label` exactly. Useful when multiple different URL shapes should
go to the same handler, or when URL structure is not a reliable signal.

```python
# Enqueueing with a label
await ctx.enqueue(
    "https://example.com/p/some-product",
    label="product",
    metadata={"category": "books"},
)
```

### 4. Custom predicate

```python
@router.on(lambda req: req.metadata.get("depth") == 0 and "shop" in req.url)
async def entry_handler(ctx: CrawlContext) -> None: ...
```

The predicate receives the `Request` (not the full context) and must return `bool`.
Predicates are synchronous — no `await`.

### 5. Default handler

```python
@router.default
async def catch_all(ctx: CrawlContext) -> None:
    ctx.log.warning("Unhandled URL: %s", ctx.request.url)
```

Required if the crawl may encounter URLs that don't match any registered pattern.
If absent and an unmatched URL is encountered, `UnhandledRequestError` is raised
and the request is dead-lettered.

---

## Match Priority

First match wins. Within the same tier, more-specific patterns win.

```
Priority order:
  1. Label match            (exact string equality — fastest)
  2. Exact URL match        (full URL string equality)
  3. URL glob               (longest pattern first — most specific wins)
  4. Domain glob            (longest domain pattern first)
  5. Custom predicate       (registration order — first registered, first checked)
  6. Default handler
  7. UnhandledRequestError  (dead-letter)
```

Tie-breaking within URL globs: longer pattern string wins. If two patterns have
the same length, registration order wins.

---

## Lifecycle Hooks

Hooks run around every request, regardless of which handler is matched. They
receive a narrower context than handlers do.

```python
@dataclass
class HookContext:
    request:  Request
    log:      logging.Logger
    store:    Store           # for cross-request state
    # page is NOT available in before_request — browser not yet acquired
```

### before_request

Runs after a request is dequeued but before the browser is acquired.

```python
@router.before_request
async def log_request(hctx: HookContext) -> None | Request:
    hctx.log.info("Starting: %s", hctx.request.url)
    # Return None → proceed with original request unchanged
    # Return a Request → use it for this cycle (queue entry unchanged)
```

**Return value semantics:**
- `None` — proceed with `hctx.request` unchanged
- `Request(...)` — use this request instead, for this cycle only. The original
  queue entry is not modified. Routing is **not** re-evaluated — the new request
  is handed directly to the already-matched handler. If you change the URL and
  want routing to re-evaluate, raise `SkipRequest` and `ctx.enqueue` the new URL.
- Raising any exception — request is nacked (retried or dead-lettered per retry
  policy). Raise `SkipRequest` specifically to dead-letter without retry.

```python
# Add auth header
@router.before_request
async def inject_token(hctx: HookContext) -> Request:
    token = await hctx.store.get_json("auth/token") or ""
    return dataclasses.replace(
        hctx.request,
        headers={**hctx.request.headers, "Authorization": f"Bearer {token}"},
    )

# Skip certain URLs
@router.before_request
async def skip_pdfs(hctx: HookContext) -> None:
    if hctx.request.url.endswith(".pdf"):
        raise SkipRequest("PDF URLs skipped")
```

Use cases:
- Logging / tracing
- Adding auth headers dynamically
- Conditional skip without retry (`SkipRequest`)
- Pre-flight validation

### after_request

Runs after the handler returns (or raises), before the browser context is closed.

```python
@dataclass
class AfterHookContext(HookContext):
    page:    zendriver.Tab      # still live at this point
    error:   Exception | None   # None if handler succeeded
    elapsed: float              # seconds
```

```python
@router.after_request
async def record_timing(hctx: AfterHookContext) -> None:
    await hctx.store.set_json(
        f"timing/{hctx.request.url}",
        {"elapsed": hctx.elapsed, "ok": hctx.error is None},
    )
```

Use cases:
- Timing metrics
- Screenshot on failure (`await hctx.page.save_screenshot(...)`)
- Global success/failure counters
- Cleanup of shared state

### Hook ordering

Multiple hooks of the same type run in registration order. All `before_request`
hooks complete before the browser is acquired; all `after_request` hooks complete
before the browser context is closed.

If a `before_request` hook raises `SkipRequest`, the request is dead-lettered
immediately (no retry). If it raises any other exception, the request is nacked
and retried per the retry policy. If one hook in a chain raises, subsequent hooks
in that chain do not run.

If an `after_request` hook raises, the exception is logged at `ERROR` level and
swallowed — it does not affect the request's ack/nack status, and subsequent
`after_request` hooks still run.

---

## Handler Contract

```python
async def my_handler(ctx: CrawlContext) -> None:
    ...
```

- Must be `async def`
- Must accept exactly one argument (`CrawlContext`)
- Return value is ignored
- Raising any exception triggers error handling (see [`error-handling.md`](error-handling.md))

Handlers should not catch and swallow all exceptions — let the error router
classify and handle them. Catch only specific, recoverable exceptions you intend
to handle locally.

---

## Enqueueing from Handlers

### Single URL

```python
# Shorthand — inherits parent domain, depth += 1
await ctx.enqueue("/next-page")              # relative URL resolved against request URL
await ctx.enqueue("https://example.com/next")

# With options
await ctx.enqueue(
    "https://example.com/product/42",
    label="product",
    metadata={"category": ctx.request.metadata.get("category")},
)
```

Relative URLs are resolved against `ctx.request.url` using `urllib.parse.urljoin`.

### Bulk

```python
links = await ctx.page.select_all("a")
hrefs = [a.get("href") for a in links if a.get("href")]
count = await ctx.enqueue_all(hrefs)
# count = number actually enqueued (not deduplicated)
```

`enqueue_all` accepts a mix of strings and `Request` objects. Strings are
converted to `Request` with parent metadata merged in (configurable):

```python
await ctx.enqueue_all(
    hrefs,
    label="product",
    metadata={"source": "listing"},
    inherit_metadata=False,    # default True — merges parent metadata
)
```

### Full Request object

```python
await ctx.enqueue(Request(
    url="https://api.example.com/data",
    method="POST",
    payload=b'{"key": "value"}',
    headers={"Content-Type": "application/json"},
    label="api-call",
    no_dedupe=True,
))
```

---

## Multiple Routers

Not supported in v1. A single `Crawler` takes a single `Router`. Handlers are
scoped by URL pattern, not by router instance.

[SCOPE RISK]: Multiple routers could model "crawl phases" (seed → product →
review), but the complexity of coordinating between them is a v2 concern. Use
`label` to achieve phase-like routing within a single router.
