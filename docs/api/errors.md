# Errors

All ZenCrawler exceptions are importable from the top-level package:

```python
from zencrawler.errors import (
    BotBlockError,
    NetworkError,
    NavigationError,
    TimeoutError,
    StructureError,
    SkipRequest,
    # ...
)
```

---

## Hierarchy

```
CrawlError
├── NetworkError
│   ├── NavigationError
│   └── TimeoutError
├── BotBlockError
├── StructureError
├── SiteDownError
├── BrowserCrashError
├── HandlerError
└── LaunchTimeoutError

SkipRequest              (not a CrawlError — flow control only)
UnhandledRequestError    (raised when no handler matches)
```

---

## CrawlError

Base class for all crawl-related errors.

```python
class CrawlError(Exception):
    request: Request | None   # the request being processed
    cause: Exception | None   # the underlying exception, if any
```

All subclasses accept `request=` and `cause=` keyword arguments.

---

## NetworkError

Raised for network-level failures that are not otherwise classified.

Subclasses:

### NavigationError

Raised when Chrome fails to navigate to the URL — HTTP error responses, DNS failures, or Chrome-level navigation errors.

```python
class NavigationError(NetworkError):
    status_code: int | None    # HTTP status code, if applicable
    chrome_error: str | None   # Chrome DevTools error string, if applicable
```

### TimeoutError

Raised when the page does not finish loading within `page_load_timeout` seconds.

---

## BotBlockError

Raised when ZenCrawler detects a bot-block page. Detection is automatic — the page title and body are checked against known signals after every navigation.

```python
class BotBlockError(CrawlError):
    signal: str           # the text string that triggered detection
    page_title: str | None
```

Default behaviour: dead-letter (no retry). Override with `@router.on_error(BotBlockError)`.

---

## StructureError

Raise this yourself when a required DOM element is missing from a page that should have it. Useful for distinguishing "site changed its HTML" from "network glitch".

```python
class StructureError(CrawlError):
    selector: str | None   # the CSS selector that was expected
    context: str | None    # human-readable context string
```

Usage:

```python
from zencrawler.errors import StructureError

price_el = await ctx.page.query_selector(".price")
if price_el is None:
    raise StructureError("price missing", selector=".price", context=ctx.request.url)
```

---

## SiteDownError

Raised when the target server is completely unreachable (connection refused, DNS failure). Retried by default.

---

## BrowserCrashError

Raised when the Chrome process terminates unexpectedly while processing a request. The pool automatically launches a replacement browser; the request is retried by default.

---

## HandlerError

Wraps an unhandled exception raised inside a user handler. The `cause` attribute holds the original exception.

---

## LaunchTimeoutError

Raised when Chrome fails to start within `BrowserPoolConfig.launch_timeout` seconds.

---

## SkipRequest

Not an error — raise it inside a handler to abandon the current request silently without marking it as failed or triggering retries.

```python
class SkipRequest(Exception):
    reason: str   # optional description logged at DEBUG level
```

```python
from zencrawler.errors import SkipRequest

@router.default
async def handle(ctx):
    if "login required" in ctx.page.title.lower():
        raise SkipRequest("login wall — skipping")
    ...
```

---

## UnhandledRequestError

Raised when no router handler matches a request and no default handler is registered.

```python
class UnhandledRequestError(Exception):
    request: Request
```
