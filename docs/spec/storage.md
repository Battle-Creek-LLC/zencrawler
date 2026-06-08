# Storage

Two primitives: `Dataset` (append-only rows) and `Store` (key-value blobs).
Both are backed by SQLite by default; backends are swappable via Protocol.

---

## Design Decisions

| Decision | Chosen | Alternative considered | Reason |
|---|---|---|---|
| Default backend | SQLite | In-memory dict | SQLite survives process death; zero extra deps |
| Single file | Yes (`crawl_data.db`) | Per-dataset files | Fewer file handles; atomic cross-dataset queries |
| Schema enforcement | None | Pydantic models | Out of scope; user's responsibility |
| Write strategy | Batched (100 rows / 1s) | Per-row commit | SQLite write amplification at high concurrency |
| Protocol typing | `typing.Protocol` | ABC | Structural subtyping — no import coupling |

---

## Dataset Protocol

```python
class Dataset(Protocol):
    name: str

    async def push(self, item: dict[str, Any]) -> None
    # Append one row. Safe to call concurrently from multiple handlers.
    # May buffer — not guaranteed flushed until flush() or close().

    async def push_many(self, items: Iterable[dict[str, Any]]) -> None
    # Append multiple rows in a single backend operation.
    # Preferred over looping push() for bulk inserts.

    async def flush(self) -> None
    # Force-flush any buffered writes to the backend.
    # Called automatically on Crawler exit.

    def iter(self) -> AsyncIterator[dict[str, Any]]
    # Async iteration over all rows, in insertion order.
    # Rows inserted after iter() starts may or may not appear.

    async def count(self) -> int

    async def export_json(
        self,
        path: Path,
        *,
        lines: bool = False,    # True → JSON Lines (NDJSON); False → JSON array
        indent: int | None = 2,
    ) -> None

    async def export_csv(
        self,
        path: Path,
        *,
        fieldnames: list[str] | None = None,  # None → inferred from first row
        extrasaction: str = "ignore",
    ) -> None

    async def clear(self) -> None
    # Deletes all rows. Use with care — irreversible in SQLite.
```

### Named datasets

The default dataset is `"default"`. Handlers access it via `ctx.dataset`.

Named datasets are created on first use:
```python
# In a handler:
products = ctx.get_dataset("products")
reviews  = ctx.get_dataset("reviews")
await products.push({"title": ..., "price": ...})
await reviews.push({"product_id": ..., "rating": ...})
```

After the run:
```python
await crawler.get_dataset("products").export_json("products.json")
await crawler.get_dataset("reviews").export_csv("reviews.csv")
```

---

## Store Protocol

```python
class Store(Protocol):
    name: str

    async def get(self, key: str) -> bytes | None
    async def set(self, key: str, value: bytes) -> None
    async def delete(self, key: str) -> None
    async def exists(self, key: str) -> bool

    async def keys(self, prefix: str = "") -> list[str]
    # Returns all keys with the given prefix. Empty prefix → all keys.

    async def get_json(self, key: str) -> Any | None
    # Equivalent to: json.loads(await self.get(key)) if key exists else None

    async def set_json(self, key: str, value: Any) -> None
    # Equivalent to: await self.set(key, json.dumps(value).encode())

    async def get_many(self, keys: Iterable[str]) -> dict[str, bytes | None]
    # Batch get — more efficient than individual get() calls.

    async def set_many(self, items: dict[str, bytes]) -> None
    # Batch set.

    async def clear(self) -> None
```

The default store is `"default"`. Named stores via `ctx.get_store("name")`.

Common patterns:
```python
# Cache a result to avoid re-processing
if not await ctx.store.exists(f"processed/{ctx.request.url}"):
    data = await extract(ctx.page)
    await ctx.dataset.push(data)
    await ctx.store.set(f"processed/{ctx.request.url}", b"1")

# Persist pagination state
cursor = await ctx.store.get_json("pagination/cursor") or 1
await ctx.enqueue(f"/page/{cursor + 1}", metadata={"cursor": cursor + 1})
await ctx.store.set_json("pagination/cursor", cursor + 1)

# Count by category
count = (await ctx.store.get_json("count/books") or 0) + 1
await ctx.store.set_json("count/books", count)
```

---

## SQLite Backend

### Schema

```sql
-- datasets table
CREATE TABLE datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset     TEXT    NOT NULL,
    item        TEXT    NOT NULL,   -- JSON
    created_at  REAL    NOT NULL    -- Unix timestamp
);
CREATE INDEX idx_datasets_name ON datasets(dataset, id);

-- store table
CREATE TABLE store (
    store_name  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       BLOB    NOT NULL,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (store_name, key)
);
CREATE INDEX idx_store_name ON store(store_name, key);
```

### Write batching

`push()` adds items to an in-memory buffer. A background task flushes when:
- Buffer reaches 100 items, OR
- 1 second has elapsed since last flush, OR
- `flush()` is called explicitly

Flush is a single `INSERT INTO datasets VALUES (?,?,?), ...` statement — SQLite
handles large multi-row inserts efficiently.

`Store.set()` is not batched — writes go directly. Key-value writes are infrequent
enough that batching adds complexity without benefit.

### Connection management

One `aiosqlite` connection per backend instance, opened on first use, closed on
`Crawler.__aexit__`. WAL journal mode is set at connection time:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;    -- durable enough; FULL is too slow
PRAGMA cache_size = -64000;     -- 64MB page cache
```

`aiosqlite` runs SQLite in a thread pool — all `await` calls yield to the event
loop. No connection pool is needed (SQLite WAL allows one writer, many readers;
the single writer is sufficient).

---

## Swappable Backends

Implement both protocols to replace the default:

```python
class MyS3Backend:
    # Dataset methods
    async def push(self, item: dict) -> None:
        self._buffer.append(item)
        if len(self._buffer) >= 100:
            await self._flush_to_s3()

    # Store methods
    async def get(self, key: str) -> bytes | None:
        return await self._s3.get_object(Bucket=self._bucket, Key=key)

    # ... implement full Protocol surface

crawler = Crawler(storage_backend=MyS3Backend(bucket="my-crawl-results"))
```

The backend class must implement both `Dataset` and `Store` protocols on the same
object (factory pattern: the `Crawler` calls `backend.dataset(name)` and
`backend.store(name)` to get named instances).

Backend protocol:
```python
class StorageBackend(Protocol):
    def dataset(self, name: str) -> Dataset: ...
    def store(self, name: str) -> Store: ...
    async def close(self) -> None: ...
```

---

## Export Formats

### JSON array (default)
```json
[
  {"title": "Book A", "price": "£12.99"},
  {"title": "Book B", "price": "£8.50"}
]
```

### JSON Lines (`lines=True`)
```
{"title": "Book A", "price": "£12.99"}
{"title": "Book B", "price": "£8.50"}
```

JSON Lines is preferred for large datasets — it can be streamed and processed
line-by-line without loading the entire file into memory.

### CSV

Columns are inferred from the keys of the first row. Rows with missing keys
produce empty cells; extra keys are ignored (`extrasaction="ignore"`).

For consistent column ordering across heterogeneous rows:
```python
await dataset.export_csv("out.csv", fieldnames=["title", "price", "url"])
```
