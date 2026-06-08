# Routing Requests

The `Router` maps incoming requests to handler functions. Handlers are matched in priority order; the first match wins.

## Match priority

1. **Label** — exact string match on `request.label`
2. **Exact URL** — literal URL string (no glob characters)
3. **URL glob** — `*` and `**` patterns, longest match first
4. **Domain glob** — `*.example.com` style, longest match first
5. **Predicate** — custom callable, in registration order
6. **Default** — catch-all registered with `@router.default`

If no handler matches, `UnhandledRequestError` is raised.

---

## Registration patterns

### Label

```python
@router.on(label="product")
async def handle_product(ctx):
    ...
```

Seed requests with a label:

```python
await crawler.run([
    Request(url="https://shop.example.com/", label="listing"),
])
```

Enqueue with a label from a handler:

```python
await ctx.enqueue(url, label="product")
```

### URL glob

```python
@router.on("https://shop.example.com/products/**")
async def handle_product(ctx):
    ...

@router.on("https://shop.example.com/categories/*")
async def handle_category(ctx):
    ...
```

`*` matches any characters except `/`. `**` matches across path segments.

### Domain

```python
@router.on(domain="shop.example.com")
async def handle_shop(ctx):
    ...

@router.on(domain="*.example.com")
async def handle_all_subdomains(ctx):
    ...
```

### Predicate

```python
@router.on(lambda req: req.depth == 0)
async def handle_seed(ctx):
    # only runs for seed requests (depth 0)
    ...
```

The predicate receives the `Request` object and must return a truthy value to match.

### Default

```python
@router.default
async def handle_any(ctx):
    ...
```

---

## Lifecycle hooks

### Before request

Runs before every request, regardless of which handler matches. Receives a `HookContext`.

```python
@router.before_request
async def log_url(hctx):
    hctx.log.info("Crawling %s (depth=%d)", hctx.request.url, hctx.request.depth)
```

You can also modify the request by returning a new one:

```python
@router.before_request
async def add_auth_header(hctx):
    token = await hctx.store.get_json("auth_token")
    return hctx.request.with_metadata({"token": token})
```

### After request

Runs after every request completes (or fails). Receives an `AfterHookContext` that includes the page, any error that occurred, and elapsed time.

```python
@router.after_request
async def record_timing(hctx):
    hctx.log.info(
        "%.2fs — %s%s",
        hctx.elapsed,
        hctx.request.url,
        f" [error: {hctx.error}]" if hctx.error else "",
    )
```

!!! note
    Exceptions raised inside an `after_request` hook are logged and swallowed — they do not affect the crawl.

---

## Error hooks

Register handlers for specific exception types:

```python
from zencrawler import ErrorAction
from zencrawler.errors import BotBlockError, NetworkError

@router.on_error(BotBlockError)
async def on_bot_block(hctx, error):
    hctx.log.warning("Bot block on %s — signal: %s", hctx.request.url, error.signal)
    return ErrorAction.DEAD_LETTER

@router.on_error(NetworkError)
async def on_network_error(hctx, error):
    return ErrorAction.RETRY
```

`ErrorAction` values:

| Value | Behaviour |
|---|---|
| `RETRY` | Re-queue the request (subject to `RetryPolicy.max_retries`) |
| `SKIP` | Discard the request silently |
| `DEAD_LETTER` | Move to dead-letter queue — visible via `crawler.dead_letters()` |
| `RAISE` | Re-raise the exception, halting the crawl |

---

## Skipping a request from a handler

Raise `SkipRequest` anywhere in a handler to abandon the current page without marking it failed:

```python
from zencrawler.errors import SkipRequest

@router.default
async def handle(ctx):
    if "out-of-stock" in await ctx.page.get_content():
        raise SkipRequest("out of stock — skipping")

    title = await ctx.page.query_selector("h1")
    ...
```

---

## Enqueuing from handlers

```python
# Single URL — inherits current depth + 1 and metadata by default
await ctx.enqueue("https://example.com/page/2")

# With label and custom metadata
await ctx.enqueue(
    "https://example.com/product/42",
    label="product",
    metadata={"category": "books"},
)

# Batch enqueue — returns count of newly queued (deduplicated) URLs
count = await ctx.enqueue_all(
    ["https://example.com/a", "https://example.com/b"],
    label="listing",
)
```

`enqueue` and `enqueue_all` are idempotent — URLs already seen by the crawler are silently skipped. Pass `no_dedupe=True` on a `Request` to bypass deduplication for that specific URL.

---

## Multiple routers

Each `Crawler` instance takes one `Router`. For multi-site crawls, create a `Crawler` per site and run them sequentially:

```python
for name, seed in SITES:
    site_router = Router()

    @site_router.default
    async def handler(ctx):
        ...

    async with Crawler(router=site_router, max_requests=100) as crawler:
        await crawler.run([seed])
```

This avoids handler state bleeding between sites and keeps each crawl's storage isolated.
