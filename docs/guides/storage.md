# Storing Data

ZenCrawler provides two storage abstractions: **Dataset** for append-only item collections and **Store** for key/value state.

Both are available on `ctx` inside handlers, and on the `Crawler` instance itself.

---

## Dataset

A `Dataset` is a write-once, append-only collection of JSON-serialisable dicts. Use it for scraped items.

### Pushing items

```python
@router.default
async def handle(ctx):
    title = await ctx.page.query_selector("h1")
    price = await ctx.page.query_selector(".price_color")

    if title:
        await ctx.dataset.push({
            "title": title.text.strip(),
            "price": price.text.strip() if price else None,
            "url":   ctx.request.url,
        })
```

### Named datasets

Separate datasets help when scraping multiple item types:

```python
@router.on(label="product")
async def handle_product(ctx):
    ...
    await ctx.get_dataset("products").push(item)

@router.on(label="review")
async def handle_review(ctx):
    ...
    await ctx.get_dataset("reviews").push(item)
```

### Reading items after a crawl

```python
async with Crawler(router=router) as crawler:
    result = await crawler.run(seeds)
    items = result.items_pushed  # total items pushed to default dataset

    # Iterate the default dataset
    async for item in crawler.dataset.iter():
        print(item)

    # Or a named dataset
    async for item in crawler.get_dataset("products").iter():
        print(item)
```

!!! warning "Access dataset inside the `async with` block"
    Storage is closed when the `async with` block exits. Iterating a dataset or calling `count()` must happen **before** the block exits. `result.items_pushed` is always safe to read after the block.

### Batch push

```python
await ctx.dataset.push_many([
    {"title": "Item A", "price": "£9.99"},
    {"title": "Item B", "price": "£14.99"},
])
```

---

## Store

A `Store` is a key/value map backed by the same SQLite database. Use it for crawl state: auth tokens, pagination cursors, per-domain counters.

### JSON helpers

```python
# Save structured state
await ctx.store.set_json("config", {"max_depth": 5, "currency": "GBP"})

# Read it back (returns None if the key doesn't exist)
config = await ctx.store.get_json("config")
```

### Raw bytes

```python
await ctx.store.set("screenshot", image_bytes)
data = await ctx.store.get("screenshot")  # bytes | None
```

### Listing and deleting

```python
keys = await ctx.store.keys()            # all keys
keys = await ctx.store.keys("product/") # keys with this prefix

await ctx.store.delete("old-key")
exists = await ctx.store.exists("old-key")  # False
```

### Named stores

```python
rate_store = ctx.get_store("rate-limits")
await rate_store.set_json("shop.example.com", {"last_seen": 1718000000})
```

---

## Storage backends

### Default: SQLite

When `storage="sqlite"` (the default), all datasets and stores are written to a single SQLite file at `storage_path` (default: `./crawl_data`).

The SQLite backend batches dataset writes — up to 100 rows or 1 second, whichever comes first — to keep write amplification low without sacrificing freshness.

### In-memory (testing)

For tests, use `MemoryDataset` and `MemoryStore` directly, or use the `build_context` helper:

```python
from zencrawler.testing.fakes import build_context, FakePage

html = "<h1>Test Product</h1><p class='price'>£9.99</p>"
ctx = build_context(url="https://example.com/product/1", page=FakePage(html))

await handle_product(ctx)

items = list(ctx.dataset._items)  # MemoryDataset stores items in ._items
assert items[0]["title"] == "Test Product"
```

---

## Accessing results after the crawl

```python
async with Crawler(router=router, max_requests=100) as crawler:
    result = await crawler.run(seeds)

    # Safe to call here (inside the block)
    count = await crawler.dataset.count()
    async for item in crawler.dataset.iter():
        process(item)

# result is always available after the block
print(f"Total items: {result.items_pushed}")
print(f"Pages done:  {result.requests_done}")
```
