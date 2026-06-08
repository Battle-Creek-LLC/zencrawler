from __future__ import annotations
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Coroutine, Iterable, Protocol, runtime_checkable

# ── Request ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Request:
    url:         str
    method:      str                  = "GET"
    headers:     dict[str, str]       = field(default_factory=dict)
    payload:     bytes | None         = None
    metadata:    dict[str, Any]       = field(default_factory=dict)
    label:       str | None           = None
    depth:       int                  = 0
    retry_count: int                  = 0
    priority:    int                  = 0
    no_dedupe:   bool                 = False

    def with_retry(self) -> "Request":
        return dataclasses.replace(self, retry_count=self.retry_count + 1)

    def with_metadata(self, extra: dict[str, Any]) -> "Request":
        return dataclasses.replace(self, metadata={**self.metadata, **extra})


# ── Storage protocols ──────────────────────────────────────────────────────

@runtime_checkable
class Dataset(Protocol):
    name: str
    async def push(self, item: dict[str, Any]) -> None: ...
    async def push_many(self, items: Iterable[dict[str, Any]]) -> None: ...
    async def flush(self) -> None: ...
    def iter(self) -> AsyncIterator[dict[str, Any]]: ...
    async def count(self) -> int: ...
    async def clear(self) -> None: ...

@runtime_checkable
class Store(Protocol):
    name: str
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def keys(self, prefix: str = "") -> list[str]: ...
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any) -> None: ...
    async def clear(self) -> None: ...


# ── Queue stats ────────────────────────────────────────────────────────────

@dataclass
class QueueStats:
    pending:     int = 0
    processing:  int = 0
    done:        int = 0
    failed:      int = 0
    dead_letter: int = 0
    total_seen:  int = 0

    @property
    def depth(self) -> int:
        return self.pending + self.processing


# ── Crawl result ───────────────────────────────────────────────────────────

@dataclass
class CrawlResult:
    requests_done:        int   = 0
    requests_failed:      int   = 0
    requests_dead_letter: int   = 0
    elapsed_seconds:      float = 0.0
    items_pushed:         int   = 0


# ── ErrorAction ────────────────────────────────────────────────────────────

from enum import Enum

class ErrorAction(Enum):
    RETRY       = "retry"
    SKIP        = "skip"
    DEAD_LETTER = "dead_letter"
    RAISE       = "raise"


# ── Queue backend protocol ─────────────────────────────────────────────────

class QueueBackend(Protocol):
    async def push(self, request: Request) -> bool: ...
    async def push_many(self, requests: Iterable[Request]) -> int: ...
    async def pop(self) -> Request | None: ...
    async def ack(self, request: Request) -> None: ...
    async def nack(self, request: Request, error: Exception, *, retry: bool = True) -> None: ...
    def peek_dead_letters(self) -> AsyncIterator[Request]: ...
    async def stats(self) -> QueueStats: ...
    async def close(self) -> None: ...


# ── Storage backend protocol ───────────────────────────────────────────────

class StorageBackend(Protocol):
    def dataset(self, name: str) -> Dataset: ...
    def store(self, name: str) -> Store: ...
    async def close(self) -> None: ...


# ── Hook / handler context (forward refs — filled in by crawler.py) ────────

# These are defined as dataclasses in context.py to avoid circular imports.
# Imported here for re-export only.
