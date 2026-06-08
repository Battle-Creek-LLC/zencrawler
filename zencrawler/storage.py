from __future__ import annotations
import asyncio
import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

from .types import Dataset, Store

# ── MemoryDataset ──────────────────────────────────────────────────────────────

class MemoryDataset:
    """In-memory dataset backed by a plain list. No flush needed. For testing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.enqueued: list[dict[str, Any]] = []

    async def push(self, item: dict[str, Any]) -> None:
        self.enqueued.append(item)

    async def push_many(self, items: Iterable[dict[str, Any]]) -> None:
        self.enqueued.extend(items)

    async def flush(self) -> None:
        pass  # nothing to flush

    async def iter(self) -> AsyncIterator[dict[str, Any]]:
        for item in list(self.enqueued):
            yield item

    async def count(self) -> int:
        return len(self.enqueued)

    async def export_json(
        self,
        path: Path,
        *,
        lines: bool = False,
        indent: int | None = 2,
    ) -> None:
        path = Path(path)
        if lines:
            path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in self.enqueued),
                encoding="utf-8",
            )
        else:
            path.write_text(
                json.dumps(self.enqueued, ensure_ascii=False, indent=indent),
                encoding="utf-8",
            )

    async def export_csv(
        self,
        path: Path,
        *,
        fieldnames: list[str] | None = None,
        extrasaction: str = "ignore",
    ) -> None:
        path = Path(path)
        rows = list(self.enqueued)
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        if fieldnames is None:
            fieldnames = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction=extrasaction)
            writer.writeheader()
            writer.writerows(rows)

    async def clear(self) -> None:
        self.enqueued.clear()


# ── MemoryStore ────────────────────────────────────────────────────────────────

class MemoryStore:
    """In-memory key-value store backed by a plain dict. For testing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def set(self, key: str, value: bytes) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def keys(self, prefix: str = "") -> list[str]:
        if prefix:
            return [k for k in self._data if k.startswith(prefix)]
        return list(self._data.keys())

    async def get_json(self, key: str) -> Any | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any) -> None:
        self._data[key] = json.dumps(value, ensure_ascii=False).encode()

    async def get_many(self, keys: Iterable[str]) -> dict[str, bytes | None]:
        return {k: self._data.get(k) for k in keys}

    async def set_many(self, items: dict[str, bytes]) -> None:
        self._data.update(items)

    async def clear(self) -> None:
        self._data.clear()


# ── SqliteDataset ──────────────────────────────────────────────────────────────

_FLUSH_BATCH_SIZE = 100
_FLUSH_INTERVAL = 1.0  # seconds


class SqliteDataset:
    """Append-only dataset backed by SQLite with batched writes.

    Writes are buffered and flushed when the buffer reaches 100 items OR
    1 second has elapsed since the last flush, whichever comes first.
    Call flush() to force an immediate write.
    """

    def __init__(self, name: str, backend: "SqliteStorageBackend") -> None:
        self.name = name
        self._backend = backend
        self._buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._last_flush: float = time.monotonic()

    # ── internal ────────────────────────────────────────────────────────────

    def _ensure_flush_task(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.get_event_loop().create_task(
                self._periodic_flush()
            )

    async def _periodic_flush(self) -> None:
        """Background task: flush once per interval while buffer is non-empty."""
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL)
            async with self._lock:
                if self._buffer:
                    await self._write_buffer()
                else:
                    # Nothing pending — let the task exit; it will be recreated on
                    # the next push.
                    return

    async def _write_buffer(self) -> None:
        """Write self._buffer to SQLite. Caller must hold self._lock."""
        if not self._buffer:
            return
        rows = self._buffer
        self._buffer = []
        self._last_flush = time.monotonic()
        db = await self._backend._connection()
        now = time.time()
        await db.executemany(
            "INSERT INTO datasets (dataset, item, created_at) VALUES (?, ?, ?)",
            [(self.name, json.dumps(row, ensure_ascii=False), now) for row in rows],
        )
        await db.commit()

    # ── Dataset protocol ─────────────────────────────────────────────────────

    async def push(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self._buffer.append(item)
            if len(self._buffer) >= _FLUSH_BATCH_SIZE:
                await self._write_buffer()
            else:
                self._ensure_flush_task()

    async def push_many(self, items: Iterable[dict[str, Any]]) -> None:
        rows = list(items)
        async with self._lock:
            self._buffer.extend(rows)
            if len(self._buffer) >= _FLUSH_BATCH_SIZE:
                await self._write_buffer()
            else:
                self._ensure_flush_task()

    async def flush(self) -> None:
        async with self._lock:
            await self._write_buffer()

    async def iter(self) -> AsyncIterator[dict[str, Any]]:
        # Flush first so that all buffered items are visible.
        await self.flush()
        db = await self._backend._connection()
        async with db.execute(
            "SELECT item FROM datasets WHERE dataset = ? ORDER BY id",
            (self.name,),
        ) as cursor:
            async for (item,) in cursor:
                yield json.loads(item)

    async def count(self) -> int:
        await self.flush()
        db = await self._backend._connection()
        async with db.execute(
            "SELECT COUNT(*) FROM datasets WHERE dataset = ?",
            (self.name,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def export_json(
        self,
        path: Path,
        *,
        lines: bool = False,
        indent: int | None = 2,
    ) -> None:
        path = Path(path)
        await self.flush()
        db = await self._backend._connection()
        async with db.execute(
            "SELECT item FROM datasets WHERE dataset = ? ORDER BY id",
            (self.name,),
        ) as cursor:
            if lines:
                with path.open("w", encoding="utf-8") as f:
                    async for (item,) in cursor:
                        f.write(item)
                        f.write("\n")
            else:
                rows: list[Any] = []
                async for (item,) in cursor:
                    rows.append(json.loads(item))
                path.write_text(
                    json.dumps(rows, ensure_ascii=False, indent=indent),
                    encoding="utf-8",
                )

    async def export_csv(
        self,
        path: Path,
        *,
        fieldnames: list[str] | None = None,
        extrasaction: str = "ignore",
    ) -> None:
        path = Path(path)
        await self.flush()
        db = await self._backend._connection()
        async with db.execute(
            "SELECT item FROM datasets WHERE dataset = ? ORDER BY id",
            (self.name,),
        ) as cursor:
            rows: list[dict[str, Any]] = []
            async for (item,) in cursor:
                rows.append(json.loads(item))

        if not rows:
            path.write_text("", encoding="utf-8")
            return
        if fieldnames is None:
            fieldnames = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction=extrasaction)
            writer.writeheader()
            writer.writerows(rows)

    async def clear(self) -> None:
        async with self._lock:
            self._buffer.clear()
        db = await self._backend._connection()
        await db.execute("DELETE FROM datasets WHERE dataset = ?", (self.name,))
        await db.commit()


# ── SqliteStore ────────────────────────────────────────────────────────────────

class SqliteStore:
    """Key-value store backed by SQLite with immediate (unbatched) writes."""

    def __init__(self, name: str, backend: "SqliteStorageBackend") -> None:
        self.name = name
        self._backend = backend

    async def get(self, key: str) -> bytes | None:
        db = await self._backend._connection()
        async with db.execute(
            "SELECT value FROM store WHERE store_name = ? AND key = ?",
            (self.name, key),
        ) as cursor:
            row = await cursor.fetchone()
            return bytes(row[0]) if row is not None else None

    async def set(self, key: str, value: bytes) -> None:
        db = await self._backend._connection()
        await db.execute(
            """
            INSERT INTO store (store_name, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (store_name, key) DO UPDATE
                SET value = excluded.value,
                    updated_at = excluded.updated_at
            """,
            (self.name, key, value, time.time()),
        )
        await db.commit()

    async def delete(self, key: str) -> None:
        db = await self._backend._connection()
        await db.execute(
            "DELETE FROM store WHERE store_name = ? AND key = ?",
            (self.name, key),
        )
        await db.commit()

    async def exists(self, key: str) -> bool:
        db = await self._backend._connection()
        async with db.execute(
            "SELECT 1 FROM store WHERE store_name = ? AND key = ?",
            (self.name, key),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def keys(self, prefix: str = "") -> list[str]:
        db = await self._backend._connection()
        if prefix:
            pattern = prefix + "%"
            async with db.execute(
                "SELECT key FROM store WHERE store_name = ? AND key LIKE ?",
                (self.name, pattern),
            ) as cursor:
                return [row[0] async for row in cursor]
        else:
            async with db.execute(
                "SELECT key FROM store WHERE store_name = ?",
                (self.name,),
            ) as cursor:
                return [row[0] async for row in cursor]

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any) -> None:
        await self.set(key, json.dumps(value, ensure_ascii=False).encode())

    async def get_many(self, keys: Iterable[str]) -> dict[str, bytes | None]:
        key_list = list(keys)
        if not key_list:
            return {}
        placeholders = ",".join("?" * len(key_list))
        db = await self._backend._connection()
        async with db.execute(
            f"SELECT key, value FROM store WHERE store_name = ? AND key IN ({placeholders})",
            (self.name, *key_list),
        ) as cursor:
            found = {row[0]: bytes(row[1]) async for row in cursor}
        return {k: found.get(k) for k in key_list}

    async def set_many(self, items: dict[str, bytes]) -> None:
        if not items:
            return
        db = await self._backend._connection()
        now = time.time()
        await db.executemany(
            """
            INSERT INTO store (store_name, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (store_name, key) DO UPDATE
                SET value = excluded.value,
                    updated_at = excluded.updated_at
            """,
            [(self.name, k, v, now) for k, v in items.items()],
        )
        await db.commit()

    async def clear(self) -> None:
        db = await self._backend._connection()
        await db.execute("DELETE FROM store WHERE store_name = ?", (self.name,))
        await db.commit()


# ── SqliteStorageBackend ───────────────────────────────────────────────────────

_INIT_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;

CREATE TABLE IF NOT EXISTS datasets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset    TEXT    NOT NULL,
    item       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets (dataset, id);

CREATE TABLE IF NOT EXISTS store (
    store_name TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (store_name, key)
);
"""


class SqliteStorageBackend:
    """Factory that returns named Dataset / Store instances backed by a single SQLite file."""

    def __init__(self, path: Path | str = "crawl_data.db") -> None:
        if not HAS_AIOSQLITE:
            raise RuntimeError(
                "aiosqlite is required for SqliteStorageBackend. "
                "Install it with: pip install aiosqlite"
            )
        self._path = Path(path)
        self._db: "aiosqlite.Connection | None" = None
        self._conn_lock = asyncio.Lock()
        self._datasets: dict[str, SqliteDataset] = {}
        self._stores: dict[str, SqliteStore] = {}

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _connection(self) -> "aiosqlite.Connection":
        async with self._conn_lock:
            if self._db is None:
                self._db = await aiosqlite.connect(self._path)
                await self._db.executescript(_INIT_SQL)
                await self._db.commit()
        return self._db

    # ── StorageBackend protocol ───────────────────────────────────────────────

    def dataset(self, name: str) -> SqliteDataset:
        if name not in self._datasets:
            self._datasets[name] = SqliteDataset(name, self)
        return self._datasets[name]

    def store(self, name: str) -> SqliteStore:
        if name not in self._stores:
            self._stores[name] = SqliteStore(name, self)
        return self._stores[name]

    async def close(self) -> None:
        # Flush all outstanding dataset buffers before closing.
        for ds in self._datasets.values():
            try:
                await ds.flush()
            except Exception:
                pass
            if ds._flush_task and not ds._flush_task.done():
                ds._flush_task.cancel()
                try:
                    await ds._flush_task
                except (asyncio.CancelledError, Exception):
                    pass

        async with self._conn_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None
