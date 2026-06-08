# Router

Matches requests to handler functions and manages lifecycle hooks.

```python
from zencrawler import Router

router = Router()
```

---

## Handler registration

### `@router.on(...)`

```python
def on(
    self,
    pattern: str | Callable | None = None,
    *,
    domain: str | None = None,
    label: str | None = None,
) -> Callable
```

Registers a handler. Exactly one of `pattern`, `domain`, or `label` should be provided. Alternatively, pass a callable as `pattern` to use it as a predicate.

**By label:**
```python
@router.on(label="product")
async def handle_product(ctx):
    ...
```

**By URL glob:**
```python
@router.on("https://shop.example.com/products/**")
async def handle_product(ctx):
    ...
```

`*` matches any characters except `/`. `**` matches across path segments.

**By domain:**
```python
@router.on(domain="shop.example.com")
async def handle_shop(ctx):
    ...
```

**By predicate:**
```python
@router.on(lambda req: req.depth == 0)
async def handle_seed(ctx):
    ...
```

The predicate receives the `Request` and returns a truthy value to match.

---

### `@router.default`

```python
@property
def default(self) -> Callable
```

Registers the catch-all handler — matches any request not caught by a more specific pattern.

```python
@router.default
async def handle_any(ctx):
    ...
```

---

## Lifecycle hooks

### `@router.before_request`

```python
@property
def before_request(self) -> Callable
```

Runs before every request, in registration order. Receives `HookContext`.

```python
@router.before_request
async def log_request(hctx):
    hctx.log.info("→ %s", hctx.request.url)
```

To modify the request, return a new `Request`:

```python
@router.before_request
async def inject_header(hctx):
    return hctx.request.with_metadata({"auth": "bearer xyz"})
```

---

### `@router.after_request`

```python
@property
def after_request(self) -> Callable
```

Runs after every request (success or failure). Receives `AfterHookContext`.

```python
@router.after_request
async def log_timing(hctx):
    status = "ok" if hctx.error is None else f"err:{type(hctx.error).__name__}"
    hctx.log.info("← %s  %.2fs  %s", hctx.request.url, hctx.elapsed, status)
```

Exceptions raised in `after_request` hooks are logged and swallowed.

---

### `@router.on_error(*types)`

```python
def on_error(self, *error_types: type[BaseException]) -> Callable
```

Registers an error hook for the given exception types. Called when a handler or the crawler itself raises one of those exceptions.

The hook receives `(hctx: HookContext, error: Exception)` and must return an `ErrorAction`.

```python
from zencrawler import ErrorAction
from zencrawler.errors import BotBlockError

@router.on_error(BotBlockError)
async def on_bot_block(hctx, error):
    hctx.log.warning("Bot block — %s", error.signal)
    return ErrorAction.DEAD_LETTER
```

---

## Match priority

When multiple registrations could match a request, the first match in this order wins:

1. Label (exact string match)
2. Exact URL (no glob characters)
3. URL glob (longest pattern first)
4. Domain glob (longest pattern first)
5. Predicate (registration order)
6. Default

If nothing matches and no default is registered, `UnhandledRequestError` is raised.

---

## Handler signature

Every handler is an `async` function that receives a single `CrawlContext`:

```python
async def my_handler(ctx: CrawlContext) -> None:
    ...
```

Return value is ignored. Raise `SkipRequest` to abandon the request silently, or any other exception to trigger the error-hook / retry logic.
