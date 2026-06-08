# Error Handling

Classifies failures, applies retry policy, provides hook points for user overrides.
The goal is zero silent failures — every request ends in `done`, `dead_letter`,
or a logged exception.

---

## Error Taxonomy

| Class | Inherits | Triggers | Default action |
|---|---|---|---|
| `NetworkError` | `CrawlError` | Timeout, DNS failure, TCP reset during navigation | Retry with backoff |
| `BotBlockError` | `CrawlError` | CAPTCHA page, 403 with bot signals, Cloudflare challenge | Dead-letter, warn |
| `StructureError` | `CrawlError` | `querySelector` returns None, assertion in handler | Dead-letter, error log |
| `SiteDownError` | `CrawlError` | HTTP 5xx, repeated connection refused | Retry with long backoff |
| `BrowserCrashError` | `CrawlError` | CDP disconnect, Chrome process exit during handler | Replace browser, retry |
| `HandlerError` | `CrawlError` | Unhandled exception from user code | Dead-letter, error log with traceback |
| `NavigationError` | `NetworkError` | Page load failed (net::ERR_*, HTTP 4xx except 403) | Retry (configurable) |
| `TimeoutError` | `NetworkError` | Page didn't load within `page_load_timeout` | Retry with backoff |

All library exceptions are subclasses of `CrawlError` (which inherits `Exception`).
User code may raise its own exceptions — they are caught and wrapped in `HandlerError`.

---

## Error Hierarchy

```
Exception
└── CrawlError
    ├── NetworkError
    │   ├── NavigationError
    │   └── TimeoutError
    ├── BotBlockError
    ├── StructureError
    ├── SiteDownError
    ├── BrowserCrashError
    └── HandlerError
        └── (wraps original exception as __cause__)
```

---

## Exception Fields

```python
@dataclass
class CrawlError(Exception):
    message:   str
    request:   Request
    cause:     Exception | None = None     # original exception, if any

class BotBlockError(CrawlError):
    signal:    str          # which heuristic triggered ("captcha", "403+body", etc.)
    page_title: str | None  # page <title> at time of detection

class StructureError(CrawlError):
    selector:  str | None   # CSS selector that returned None, if applicable
    context:   str | None   # user-supplied description, e.g. "expected price element"

class NavigationError(NetworkError):
    status_code: int | None  # HTTP status if available
    chrome_error: str | None # CDP error string (e.g. "net::ERR_NAME_NOT_RESOLVED")
```

`StructureError` can be raised explicitly in handler code:
```python
price = await ctx.page.select_one(".price")
if price is None:
    raise StructureError("expected price element", selector=".price")
```

---

## Bot-Block Detection

Applied automatically after each page navigation. User may also trigger it manually.

### Detection heuristics (in order)

```
1. HTTP status 403 AND response body matches bot_signals pattern list
2. Page title matches bot_title_signals list (case-insensitive contains)
3. Page body contains CAPTCHA iframe src patterns
4. Response header "cf-mitigated" = "challenge" (Cloudflare)
5. URL redirected to a known bot-check path (configurable: bot_url_patterns)
```

Default signal lists:
```python
bot_signals = [
    "access denied", "blocked", "unusual traffic",
    "automated", "bot detected", "verify you are human",
    "sorry, you have been blocked", "enable javascript",
]

bot_title_signals = [
    "captcha", "access denied", "attention required",
    "just a moment", "security check",
]

bot_url_patterns = [
    "/cdn-cgi/challenge-platform/",
    "/captcha",
]
```

All lists are configurable:
```python
Crawler(
    bot_signals=["my-custom-signal"],     # replaces defaults
    extra_bot_signals=["extra-signal"],   # appends to defaults
)
```

Bot-block detection runs **automatically** after every successful page navigation,
before the matched handler is called. The heuristics above are checked in order;
on the first match, `BotBlockError` is raised and routes through error handling.
No user code is needed to trigger it.

### Manual detection in handlers

For custom signals not covered by the built-in heuristics:

```python
@router.on("https://example.com/**")
async def handler(ctx):
    if "verify" in ctx.page.title.lower():
        raise BotBlockError("manual detection", signal="title-check",
                            page_title=ctx.page.title)
```

---

## Retry Policy

Defined in [`request-queue.md`](request-queue.md). Error handling interacts with
it through the `retry` parameter of `queue.nack()`:

```
NetworkError       → nack(retry=True)   uses RetryPolicy.backoff
BotBlockError      → nack(retry=False)  → dead_letter immediately
StructureError     → nack(retry=False)  → dead_letter immediately
SiteDownError      → nack(retry=True)   uses RetryPolicy.backoff (2× longer base)
BrowserCrashError  → nack(retry=True)   retry_count += 1, no additional backoff
HandlerError       → nack(retry=False)  → dead_letter, full traceback logged
```

---

## Error Hook API

Register handlers for specific error types on the `Router`:

```python
@router.on_error(BotBlockError)
async def handle_bot_block(ctx: CrawlContext, error: BotBlockError) -> ErrorAction:
    await ctx.store.set_json(
        f"blocked/{ctx.request.url}",
        {"signal": error.signal, "title": error.page_title},
    )
    return ErrorAction.DEAD_LETTER

@router.on_error(StructureError)
async def handle_structure_change(ctx: CrawlContext, error: StructureError) -> ErrorAction:
    path = await ctx.page.save_screenshot(f"debug_{hash(ctx.request.url)}.jpg")
    await ctx.store.set_json(f"debug/{ctx.request.url}", {"screenshot": path})
    return ErrorAction.DEAD_LETTER

@router.on_error(SiteDownError)
async def handle_site_down(ctx: CrawlContext, error: SiteDownError) -> ErrorAction:
    ctx.log.warning("Site appears down, backing off: %s", ctx.request.url)
    return ErrorAction.RETRY    # use default retry policy
```

### `ErrorAction` enum

```python
class ErrorAction(Enum):
    RETRY       = "retry"        # re-queue with backoff (respects RetryPolicy)
    SKIP        = "skip"         # ack the request (count as done, no retry)
    DEAD_LETTER = "dead_letter"  # nack(retry=False) → dead letter
    RAISE       = "raise"        # re-raise the original exception (crashes the crawl)
```

### Hook matching

Error hooks match by exception type (including subclasses). More specific types
are checked first:

```python
@router.on_error(NavigationError)   # checked before NetworkError for NavigationError
async def handle_nav(ctx, err): ...

@router.on_error(NetworkError)      # fallback for NetworkError and other subclasses
async def handle_net(ctx, err): ...
```

If no hook matches, the default action from the taxonomy table is applied.

If the hook itself raises, the exception is logged and the default action applies.
Hooks must not raise — use `ctx.log.error(...)` for problems inside hooks.

### Global error handler

A catch-all hook:
```python
@router.on_error(Exception)
async def global_error_handler(ctx: CrawlContext, error: Exception) -> ErrorAction:
    ctx.log.error("Unexpected error on %s: %s", ctx.request.url, error, exc_info=True)
    return ErrorAction.DEAD_LETTER
```

---

## SkipRequest

`SkipRequest` is a special exception that marks a request as done (acked) without
processing it. It does not consume a retry — the request simply disappears from
the queue cleanly.

```python
from zencrawler import SkipRequest

@router.before_request
async def skip_pdfs(hctx):
    if hctx.request.url.endswith(".pdf"):
        raise SkipRequest("PDF skipped")

@router.on("https://example.com/**")
async def handler(ctx):
    if ctx.request.depth > 3:
        raise SkipRequest("depth limit reached")
```

`SkipRequest` vs `ErrorAction.SKIP`:
- `SkipRequest` is raised from handlers or hooks — imperative style
- `ErrorAction.SKIP` is returned from `on_error` hooks — in response to a failure
Both result in `ack()` being called (request marked done, no dead-letter).

---

## zendriver Exception Mapping

zendriver raises its own exceptions during navigation and CDP calls. ZenCrawler
maps them to library error types automatically:

| zendriver exception | ZenCrawler exception |
|---|---|
| `zendriver.TimeoutError` | `TimeoutError` (subclass of `NetworkError`) |
| CDP `net::ERR_NAME_NOT_RESOLVED` | `NavigationError` |
| CDP `net::ERR_CONNECTION_REFUSED` | `NavigationError` |
| CDP `net::ERR_CONNECTION_TIMED_OUT` | `TimeoutError` |
| HTTP 403 (with bot signal) | `BotBlockError` |
| HTTP 4xx (other) | `NavigationError` with `status_code` set |
| HTTP 5xx | `SiteDownError` |
| CDP disconnect | `BrowserCrashError` |

Unmapped zendriver exceptions bubble up as `HandlerError` with the original
exception as `__cause__`.

---

## Page Load Timeout

```python
Crawler(page_load_timeout=30.0)   # seconds; default 30
```

If a page does not fire the `load` event within this time, `TimeoutError` is
raised and the request is retried. The browser context is closed — a hung page
should not occupy a browser slot indefinitely.

---

## Handler Isolation

Each handler runs in a `try/except` block. An unhandled exception in one handler
does not affect other in-flight handlers.

```python
async def _run_handler(self, request: Request) -> None:
    handle = None
    try:
        handle = await self._pool.acquire(request)
        ctx    = self._build_context(handle, request)
        await self._router.dispatch(ctx)
        await self._queue.ack(request)
    except CrawlError as e:
        action = await self._router.dispatch_error(ctx, e)
        await self._apply_action(action, request, e)
    except Exception as e:
        wrapped = HandlerError(str(e), request=request, cause=e)
        await self._queue.nack(request, wrapped, retry=False)
        self._log.error("Unhandled exception in handler", exc_info=True)
    finally:
        if handle:
            crashed = isinstance(e, BrowserCrashError) if 'e' in dir() else False
            await self._pool.release(handle, crashed=crashed)
```
