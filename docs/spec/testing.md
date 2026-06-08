# Testing

Testing patterns for handlers and crawl configurations. ZenCrawler provides a
`zencrawler.testing` module with fakes that mirror the real zendriver API.

---

## Unit Testing with FakePage

`FakePage` is a drop-in replacement for `zendriver.Tab` that works against an
HTML string. It mirrors the real API: element properties are properties (not
coroutines), `select()` / `select_all()` wait semantics are simulated, and
`query_selector()` returns `None` when nothing matches.

```python
import pytest
from zencrawler.testing import FakePage, build_context

@pytest.mark.asyncio
async def test_book_page_handler():
    page = FakePage(html="""
        <h1>A Light in the Attic</h1>
        <p class="price_color">£51.77</p>
    """)
    ctx = build_context(
        url="https://books.toscrape.com/catalogue/a-light-in-the-attic.html",
        page=page,
    )

    await book_page(ctx)   # call your handler

    rows = [r async for r in ctx.dataset.iter()]
    assert len(rows) == 1
    assert rows[0]["title"] == "A Light in the Attic"
    assert rows[0]["price"] == "£51.77"
```

### FakePage API

```python
FakePage(
    html:  str,
    url:   str = "https://example.com/",
    title: str = "Fake Page",
)
```

Supported methods (mirrors `zendriver.Tab`):

| Method | Behaviour |
|---|---|
| `await page.select(selector)` | Parses HTML, returns `FakeElement`; raises `NoSuchElementError` if no match |
| `await page.select_all(selector)` | Returns `list[FakeElement]`; empty list if no match |
| `await page.query_selector(selector)` | Returns `FakeElement | None` |
| `await page.query_selector_all(selector)` | Returns `list[FakeElement]` |
| `await page.find(text)` | Returns first `FakeElement` whose `.text` contains `text` |
| `await page.evaluate(js)` | Always returns `None` unless configured via `page.set_evaluate_result(js, value)` |
| `await page.get_content()` | Returns the `html` string passed to constructor |
| `await page.save_screenshot(filename)` | Returns `filename` unchanged; no file is written |
| `page.title` | Property — returns `title` from constructor |
| `page.url` | Property — returns `url` from constructor |

`FakeElement` properties mirror `zendriver.Element`:

| Property / Method | Behaviour |
|---|---|
| `el.text` | Inner text (tags stripped) |
| `el.text_all` | Same as `.text` in FakeElement |
| `el.attrs` | Dict of HTML attributes |
| `el.get("name")` | `attrs.get("name")` |
| `el.get_html()` | Outer HTML string |
| `el.query_selector(sel)` | Scoped query within this element |
| `el.query_selector_all(sel)` | Scoped query, returns list |

**CSS selector support:** FakePage uses `html.parser` + a lightweight selector
engine. Supported: tag, class (`.foo`), id (`#bar`), attribute (`[href]`,
`[href="val"]`), descendant (space), direct child (`>`), comma union. Not
supported: pseudo-classes (`:first-child`, `:not()`), sibling selectors (`~`,
`+`). Keep test HTML simple to stay within supported selectors.

---

## build_context

Creates a `CrawlContext` with in-memory dataset/store and a recording enqueue:

```python
from zencrawler.testing import build_context, FakePage

ctx = build_context(
    url="https://example.com/product/42",
    page=FakePage(html="<h1>Widget</h1><a href='/next'>Next</a>"),
    metadata={"category": "widgets"},
    label="product",
    # dataset=MemoryDataset(),   # inject custom dataset if needed
    # store=MemoryStore(),       # inject custom store if needed
)
```

Returned `ctx` has:
- `ctx.dataset` — `MemoryDataset` (append-only, inspectable)
- `ctx.store` — `MemoryStore` (full CRUD, inspectable)
- `ctx.enqueue(...)` — records the call but does NOT execute it
- `ctx.enqueued` — `list[Request]` of all calls made to `ctx.enqueue`

```python
await my_handler(ctx)

# Inspect enqueued requests
assert len(ctx.enqueued) == 1
assert ctx.enqueued[0].url == "https://example.com/next"
assert ctx.enqueued[0].metadata["category"] == "widgets"  # inherited

# Inspect dataset
count = await ctx.dataset.count()
rows  = [r async for r in ctx.dataset.iter()]

# Inspect store
value = await ctx.store.get_json("some-key")
```

---

## MemoryDataset / MemoryStore

Used automatically by `build_context`. Can also be injected directly:

```python
from zencrawler.testing import MemoryDataset, MemoryStore

ds = MemoryDataset(name="products")
await ds.push({"title": "Widget", "price": 9.99})
await ds.push_many([{"title": "Gadget"}, {"title": "Doohickey"}])

assert await ds.count() == 3
rows = [r async for r in ds.iter()]

await ds.export_json(Path("test_output.json"))
```

`MemoryStore` has the full `Store` protocol:

```python
store = MemoryStore()
await store.set_json("key", {"value": 42})
assert await store.get_json("key") == {"value": 42}
assert await store.exists("key") is True
assert await store.keys("ke") == ["key"]
```

---

## Testing Error Hooks

Test error hook behaviour by constructing the error and calling the hook directly:

```python
from zencrawler import BotBlockError, ErrorAction
from zencrawler.testing import build_context, FakePage

async def test_bot_block_hook():
    ctx = build_context(url="https://example.com/", page=FakePage(html=""))
    error = BotBlockError("test", request=ctx.request, signal="captcha")

    action = await on_block(ctx, error)   # call your error hook

    assert action == ErrorAction.DEAD_LETTER
    assert await ctx.store.exists(f"blocked/{ctx.request.url}")
```

---

## Testing Router Dispatch

Test the full routing logic without a browser:

```python
from zencrawler.testing import FakeRouter

router = Router()

@router.on("https://example.com/products/**")
async def product_handler(ctx): ...

@router.default
async def fallback(ctx): ...

fake_router = FakeRouter(router)

# Check which handler would be dispatched
handler = fake_router.match(Request("https://example.com/products/42"))
assert handler is product_handler

handler = fake_router.match(Request("https://example.com/about"))
assert handler is fallback
```

---

## Integration Testing (Real Browser)

Integration tests hit a real browser and network. Mark them with a custom marker
to keep CI fast:

```python
# conftest.py
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: tests requiring a real browser")
```

```python
# test_integration.py
import pytest
from zencrawler import Crawler, Router

@pytest.mark.asyncio
@pytest.mark.integration
async def test_crawl_books_site():
    router = Router()

    @router.on("https://books.toscrape.com/catalogue/**.html")
    async def book(ctx):
        await ctx.dataset.push({"title": (await ctx.page.select("h1")).text})

    @router.default
    async def follow(ctx):
        links = await ctx.page.select_all("a")
        await ctx.enqueue_all([a.get("href") for a in links if a.get("href")])

    async with Crawler(router=router, max_concurrency=1, max_requests=5) as crawler:
        result = await crawler.run(["https://books.toscrape.com/"])

    assert result.requests_done >= 1
    assert result.requests_dead_letter == 0
```

Run only integration tests:
```bash
pytest -m integration
```

Skip integration tests:
```bash
pytest -m "not integration"
```

---

## CI/CD Pattern

```yaml
# .github/workflows/test.yml
- name: Run unit tests
  run: pytest -m "not integration" --tb=short

- name: Run integration tests
  if: github.ref == 'refs/heads/main'
  run: |
    pytest -m integration --tb=short
  env:
    ZENCRAWLER_HEADLESS: "true"
```

Chrome must be available in the CI environment. For GitHub Actions:

```yaml
- name: Install Chrome
  uses: browser-actions/setup-chrome@v1
```

---

## Pytest Fixtures

Reusable fixtures for common test patterns:

```python
# conftest.py
import pytest
from zencrawler.testing import MemoryDataset, MemoryStore, FakePage, build_context

@pytest.fixture
def fake_ctx(request):
    url  = getattr(request, "param", {}).get("url",  "https://example.com/")
    html = getattr(request, "param", {}).get("html", "<html><body></body></html>")
    return build_context(url=url, page=FakePage(html=html))

@pytest.fixture
async def live_crawler(router):
    async with Crawler(router=router, max_concurrency=1, max_requests=10) as c:
        yield c
```
