# Request

An immutable dataclass representing a URL to crawl, plus routing and metadata fields.

```python
from zencrawler import Request

req = Request(url="https://example.com/product/42", label="product")
```

---

## Fields

`url` *(str, required)*
:   The URL to fetch. Must be an absolute URL with scheme.

`method` *(str)*
:   HTTP method. Default: `"GET"`.
    zendriver always navigates via Chrome; this field is stored for routing and logging purposes.

`headers` *(dict[str, str])*
:   Custom request headers. Default: `{}`.

`payload` *(bytes | None)*
:   Request body for POST/PUT requests. Default: `None`.

`metadata` *(dict[str, Any])*
:   Arbitrary user data attached to the request. Accessible in handlers via `ctx.request.metadata`.
    Default: `{}`.

    ```python
    req = Request(url="https://example.com/", metadata={"site": "example", "region": "uk"})

    # In handler:
    site = ctx.request.metadata.get("site")
    ```

`label` *(str | None)*
:   Routing label. Used by `@router.on(label=...)` for exact matching.
    Default: `None`.

`depth` *(int)*
:   How many hops from a seed this request is. Seeds are depth `0`; `ctx.enqueue()` increments by 1.
    Default: `0`.

`retry_count` *(int)*
:   Number of times this request has been retried. Set automatically by the queue.
    Default: `0`.

`priority` *(int)*
:   Queue priority. Lower values are processed first.
    Default: `0`.

`no_dedupe` *(bool)*
:   If `True`, bypasses URL deduplication — the request is always queued even if the URL has been seen before.
    Default: `False`.

---

## Methods

### `with_retry()`

```python
def with_retry(self) -> Request
```

Returns a copy of this request with `retry_count` incremented by 1. Used internally by the queue.

### `with_metadata(extra)`

```python
def with_metadata(self, extra: dict[str, Any]) -> Request
```

Returns a copy of this request with `extra` merged into `metadata`.

```python
new_req = ctx.request.with_metadata({"page_num": 2})
```

---

## Creating requests

Strings passed to `crawler.run()`, `crawler.enqueue()`, or `ctx.enqueue()` are automatically converted to `Request(url=string)`.

```python
# All equivalent ways to seed a crawl:
await crawler.run(["https://example.com/"])
await crawler.run([Request(url="https://example.com/")])
await crawler.run([Request(url="https://example.com/", label="seed", priority=-1)])
```

---

## Metadata inheritance

When `ctx.enqueue()` is called with `inherit_metadata=True` (the default), the new request inherits the current request's metadata dict. This lets you propagate context (e.g. the originating site name) through multi-level crawls automatically:

```python
# Seed
Request(url="https://shop.example.com/", metadata={"site": "example"})

# Handler at depth 0 — enqueues depth-1 URLs
await ctx.enqueue("/products/42", label="product")
# → Request(url="https://shop.example.com/products/42", metadata={"site": "example"}, depth=1)
```
