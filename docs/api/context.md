# CrawlContext

Passed to every handler function. Provides the rendered page, the current request, storage access, and helpers for queuing follow-up URLs.

```python
async def my_handler(ctx: CrawlContext) -> None:
    ...
```

---

## Properties

`ctx.page`
:   A live `zendriver.Tab` for the loaded page. See [Page API](#page-api) below.

`ctx.request`
:   The [`Request`](request.md) currently being processed.

`ctx.dataset`
:   The default `Dataset`. Call `await ctx.dataset.push(item)` to save scraped data.

`ctx.store`
:   The default `Store`. Call `await ctx.store.set_json(key, value)` for key/value state.

`ctx.log`
:   A `logging.Logger` scoped to this handler invocation.

`ctx.enqueued`
:   List of `Request` objects queued during this handler call (populated by `enqueue`/`enqueue_all`).

---

## Methods

### `enqueue(url, **kwargs)`

```python
async def enqueue(
    self,
    url: str | Request,
    *,
    label: str | None = None,
    metadata: dict | None = None,
    depth_offset: int = 1,
    inherit_metadata: bool = True,
) -> bool
```

Queue a follow-up URL. Returns `True` if newly queued, `False` if already seen.

- `depth_offset` — added to `ctx.request.depth` to set the new request's depth (default `1`).
- `inherit_metadata` — if `True`, the new request inherits the current request's metadata dict, merged with any `metadata` kwarg.

```python
await ctx.enqueue("https://example.com/page/2")
await ctx.enqueue("/product/42", label="product", metadata={"category": "books"})
```

### `enqueue_all(urls, **kwargs)`

```python
async def enqueue_all(
    self,
    urls: Iterable[str | Request],
    **kwargs,
) -> int
```

Batch-enqueue multiple URLs. All `kwargs` are forwarded to `enqueue`. Returns the count of newly queued URLs.

```python
links = await ctx.page.select_all("a[href]")
count = await ctx.enqueue_all(
    [a.get("href") for a in links if a.get("href")],
    label="listing",
)
```

### `get_dataset(name)`

```python
def get_dataset(self, name: str) -> Dataset
```

Returns the named dataset.

### `get_store(name)`

```python
def get_store(self, name: str) -> Store
```

Returns the named store.

---

## Page API

`ctx.page` is a `zendriver.Tab`. Key methods and properties:

### Querying elements

```python
el  = await ctx.page.query_selector("css-selector")   # Element | None
els = await ctx.page.select_all("css-selector")        # list[Element]
```

`query_selector` returns `None` immediately if the element is not present — no waiting, no exception.

`select_all` returns an empty list if nothing matches.

!!! warning "`select()` vs `query_selector()`"
    `ctx.page.select(selector)` **waits** for the element to appear (up to `timeout` seconds) and raises if it never does. `query_selector` is the non-blocking, non-raising alternative. Use `query_selector` for optional elements and `select` when the element must exist.

### Navigation

```python
await ctx.page.get("https://example.com/other-page")
```

Navigation happens inside the handler only when you explicitly call it. The Crawler navigates to `ctx.request.url` before calling your handler.

### Content and JavaScript

```python
html = await ctx.page.get_content()          # full HTML string
val  = await ctx.page.evaluate("expression") # run arbitrary JS, returns Any
```

### Screenshots

```python
path = await ctx.page.save_screenshot("output.png")  # saves file, returns path
```

### Properties

```python
ctx.page.url    # str — current URL (property, not method)
ctx.page.title  # str — current page title (property, not method)
```

!!! warning "Properties, not methods"
    `page.url` and `page.title` are plain properties. Read them directly — **do not** call `page.url()` or `page.title()`.

---

## Element API

Elements returned by `query_selector` and `select_all`:

```python
el.text       # visible text content (property)
el.text_all   # text from all descendant nodes (property)
el.attrs      # dict of all HTML attributes (property)

el.get("href")         # single attribute value → str | None
await el.get_html()    # outer HTML string
```

!!! warning "`el.get()`, not `el.get_attribute()`"
    The attribute accessor is `el.get("name")`, **not** `el.get_attribute("name")`. The latter does not exist.

---

## HookContext

Passed to `before_request` and `after_request` hooks (and `on_error` hooks).

```python
hctx.request   # Request
hctx.log       # logging.Logger
hctx.store     # default Store
```

### AfterHookContext

`after_request` hooks receive `AfterHookContext`, which extends `HookContext` with:

```python
hctx.page      # zendriver.Tab (the loaded page)
hctx.error     # Exception | None — the error that occurred, if any
hctx.elapsed   # float — seconds the request took
```
