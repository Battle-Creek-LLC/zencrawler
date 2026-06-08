# Error Handling

ZenCrawler uses a typed exception hierarchy so you can handle specific failure modes precisely. Most errors are caught automatically and either retried or dead-lettered — you only need to intervene when the default behaviour isn't right for your site.

---

## Exception hierarchy

```
CrawlError
├── NetworkError
│   ├── NavigationError   — HTTP errors, Chrome navigation failures
│   └── TimeoutError      — page load exceeded page_load_timeout
├── BotBlockError         — bot detection triggered
├── StructureError        — expected element not found in DOM
├── SiteDownError         — site unreachable (connection refused, DNS failure)
├── BrowserCrashError     — Chrome process terminated unexpectedly
├── HandlerError          — unhandled exception inside a handler
└── LaunchTimeoutError    — Chrome failed to start within launch_timeout

SkipRequest               — not an error; raised to silently abandon a request
UnhandledRequestError     — no router handler matched
```

Every `CrawlError` carries:

- `.request` — the `Request` being processed when the error occurred
- `.cause` — the underlying exception that triggered it, if any

---

## Default retry behaviour

The default `RetryPolicy` retries up to **3 times** with exponential back-off (base 2s, max 300s, ±10% jitter):

| Error | Default action |
|---|---|
| `NetworkError` | Retry |
| `TimeoutError` | Retry |
| `BotBlockError` | Dead-letter |
| `StructureError` | Retry |
| `SiteDownError` | Retry |
| `BrowserCrashError` | Retry (new browser launched automatically) |
| `HandlerError` | Retry |

Once `max_retries` is exhausted the request moves to the dead-letter queue.

---

## Bot block detection

ZenCrawler checks every loaded page for known bot-block signals in the page title and body text. When a signal is detected, `BotBlockError` is raised automatically — you don't need to check manually.

Default signals checked in the page body:

- `"unusual traffic"`
- `"automated queries"`
- `"bot detected"`
- `"verify you are human"`
- `"sorry, you have been blocked"`
- `"your ip has been blocked"`
- `"enable javascript and cookies to continue"`

Default signals checked in the page title:

- `"captcha"`, `"access denied"`, `"attention required"`, `"just a moment"`, `"security check"`

Add site-specific signals with `extra_bot_signals`:

```python
async with Crawler(
    router=router,
    extra_bot_signals=["please complete the security check", "ddos protection"],
) as crawler:
    await crawler.run(seeds)
```

---

## Custom error hooks

Use `@router.on_error` to override the default action for specific error types:

```python
from zencrawler import ErrorAction
from zencrawler.errors import BotBlockError, NetworkError, TimeoutError

@router.on_error(BotBlockError)
async def on_bot_block(hctx, error):
    hctx.log.warning(
        "Bot block on %s — signal: %r  title: %r",
        hctx.request.url, error.signal, error.page_title,
    )
    return ErrorAction.DEAD_LETTER  # don't retry, move to dead-letter

@router.on_error(TimeoutError)
async def on_timeout(hctx, error):
    if hctx.request.retry_count >= 1:
        return ErrorAction.SKIP  # already retried once, give up
    return ErrorAction.RETRY

@router.on_error(NetworkError)
async def on_network(hctx, error):
    return ErrorAction.RETRY
```

`ErrorAction` values:

| Value | Behaviour |
|---|---|
| `RETRY` | Re-queue with `retry_count + 1` (subject to `RetryPolicy.max_retries`) |
| `SKIP` | Discard silently |
| `DEAD_LETTER` | Move to dead-letter queue |
| `RAISE` | Re-raise the exception, stopping the crawl |

---

## Skipping from a handler

Raise `SkipRequest` to abandon the current page without counting it as failed:

```python
from zencrawler.errors import SkipRequest

@router.default
async def handle(ctx):
    content = await ctx.page.get_content()

    if "this item is no longer available" in content.lower():
        raise SkipRequest("item unavailable")

    title = await ctx.page.query_selector("h1")
    ...
```

`SkipRequest` is never retried and never counted as a failure.

---

## Custom retry policy

```python
from zencrawler import RetryPolicy
from zencrawler.errors import BotBlockError

async with Crawler(
    router=router,
    retry_policy=RetryPolicy(
        max_retries=5,
        backoff_base=3.0,
        backoff_max=120.0,
        backoff_jitter=0.2,
        no_retry_on=(BotBlockError,),  # never retry bot blocks
    ),
) as crawler:
    await crawler.run(seeds)
```

---

## Inspecting dead letters

```python
async with Crawler(router=router) as crawler:
    result = await crawler.run(seeds)

    print(f"Dead-lettered: {result.requests_dead_letter}")

    async for req in crawler.dead_letters():
        print(f"  {req.url}  (retries: {req.retry_count})")
```

---

## Handling `StructureError`

`StructureError` is raised when your handler calls `ctx.page.select(selector)` and the element is not found (note: `query_selector` returns `None` instead of raising). Raise it manually when a required element is missing:

```python
from zencrawler.errors import StructureError

@router.on(label="product")
async def handle_product(ctx):
    price_el = await ctx.page.query_selector(".price")
    if price_el is None:
        raise StructureError(
            "price element not found",
            selector=".price",
            context=ctx.request.url,
        )

    await ctx.dataset.push({"price": price_el.text})
```

This lets you distinguish "site structure changed" from "network error" in your error hooks and dead-letter analysis.
