from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

from .types import QueueStats, Request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tracking parameters stripped during URL normalisation
# ---------------------------------------------------------------------------

TRACKING_PARAMS: list[str] = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "source",
]

# Default ports that should be removed from URLs
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
}


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Controls how failed requests are retried with exponential back-off."""

    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 300.0
    backoff_jitter: float = 0.1

    # Exception types that should trigger a retry
    retry_on: tuple[type[Exception], ...] = field(default_factory=tuple)

    # Exception types that should NOT be retried (wins over retry_on)
    no_retry_on: tuple[type[Exception], ...] = field(default_factory=tuple)

    def should_retry(self, error: Exception) -> bool:
        """Return True if this error warrants a retry attempt."""
        # no_retry_on wins if the exception matches both lists
        if self.no_retry_on and isinstance(error, self.no_retry_on):
            return False
        if self.retry_on and isinstance(error, self.retry_on):
            return True
        # Default: retry when neither list explicitly matches
        return True

    def backoff_delay(self, retry_count: int) -> float:
        """Return the number of seconds to wait before the next attempt."""
        delay = min(self.backoff_base ** retry_count, self.backoff_max)
        lo = 1.0 - self.backoff_jitter
        hi = 1.0 + self.backoff_jitter
        delay *= random.uniform(lo, hi)
        return delay


# ---------------------------------------------------------------------------
# URL normalisation and hashing
# ---------------------------------------------------------------------------

def normalise_url(url: str, strip_params: list[str] | None = None) -> str:
    """Return a canonicalised version of *url* suitable for deduplication.

    Steps applied (per spec):
    1.  Parse into components
    2.  Lowercase scheme and host
    3.  Remove default port (80/http, 443/https)
    4.  Decode percent-encoded characters that don't need encoding
    5.  Remove trailing slash from path (unless path is exactly "/")
    6.  Sort query parameters alphabetically by key
    7.  Remove known tracking parameters
    8.  Remove empty query parameters
    9.  Strip fragment
    """
    if strip_params is None:
        strip_params = TRACKING_PARAMS

    strip_set = set(strip_params)

    parsed = urllib.parse.urlparse(url)

    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    port = parsed.port

    # Step 3 — remove default port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None

    # Reconstruct netloc
    if port is not None:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    # Preserve userinfo if present
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    # Step 4 — decode unnecessarily percent-encoded characters
    # urllib.parse.unquote handles this; we use quote to re-encode only what's necessary
    path = urllib.parse.unquote(parsed.path)
    # Re-encode only characters that must be encoded in paths
    path = urllib.parse.quote(path, safe="/:@!$&'()*+,;=~-._")

    # Step 5 — strip trailing slash unless path is root
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Step 6–8 — process query string
    raw_qs = parsed.query
    if raw_qs:
        params = urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
        # Remove tracking and empty params
        params = [
            (k, v)
            for k, v in params
            if k not in strip_set and (k.strip() or v.strip())
        ]
        # Sort alphabetically by key then value for determinism
        params.sort(key=lambda kv: (kv[0], kv[1]))
        qs = urllib.parse.urlencode(params)
    else:
        qs = ""

    # Step 9 — strip fragment
    fragment = ""

    normalised = urllib.parse.urlunparse((scheme, netloc, path, parsed.params, qs, fragment))
    return normalised


def url_hash(url: str, strip_params: list[str] | None = None, hash_bytes: int = 16) -> str:
    """Return a hex string SHA-256 hash of the normalised *url*.

    Args:
        url: The URL to hash.
        strip_params: Tracking parameter names to remove before hashing.
        hash_bytes: Number of raw bytes to use (default 16 → 32 hex chars).

    Returns:
        A lowercase hex string of length ``hash_bytes * 2``.
    """
    normalised = normalise_url(url, strip_params=strip_params)
    digest = hashlib.sha256(normalised.encode()).digest()
    return digest[:hash_bytes].hex()


# ---------------------------------------------------------------------------
# MemoryQueue
# ---------------------------------------------------------------------------

class MemoryQueue:
    """In-process queue backed by asyncio.PriorityQueue.

    Zero I/O, lowest latency. State is lost on process exit.
    """

    def __init__(
        self,
        retry_policy: RetryPolicy | None = None,
        strip_params: list[str] | None = None,
        hash_bytes: int = 16,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy()
        self._strip_params = strip_params if strip_params is not None else TRACKING_PARAMS
        self._hash_bytes = hash_bytes

        # (negated_priority, enqueued_timestamp, Request)
        self._pending: asyncio.PriorityQueue[tuple[int, float, Request]] = asyncio.PriorityQueue()
        self._processing: dict[str, Request] = {}
        self._done: set[str] = set()
        self._dead_letter: list[Request] = []
        self._seen: set[str] = set()  # all-time: includes done + dead_letter

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _hash(self, url: str) -> str:
        return url_hash(url, strip_params=self._strip_params, hash_bytes=self._hash_bytes)

    # ------------------------------------------------------------------
    # QueueBackend protocol
    # ------------------------------------------------------------------

    async def push(self, request: Request) -> bool:
        """Enqueue *request*.

        Returns False if the URL was deduplicated (already seen).
        Returns True if accepted into pending.
        """
        h = self._hash(request.url)

        if not request.no_dedupe and h in self._seen:
            logger.debug("Deduplicated %s", request.url)
            return False

        self._seen.add(h)
        # Higher priority value = popped first, so negate for min-heap
        await self._pending.put((-request.priority, time.monotonic(), request))
        return True

    async def push_many(self, requests: Iterable[Request]) -> int:
        count = 0
        for req in requests:
            if await self.push(req):
                count += 1
        return count

    async def pop(self) -> Request | None:
        """Return the next pending request or None if the queue is empty.

        Moves the request atomically to *processing* state.
        """
        try:
            _priority, _ts, request = self._pending.get_nowait()
        except asyncio.QueueEmpty:
            return None

        h = self._hash(request.url)
        self._processing[h] = request
        return request

    async def ack(self, request: Request) -> None:
        """Mark *request* as successfully completed."""
        h = self._hash(request.url)
        self._processing.pop(h, None)
        self._done.add(h)

    async def nack(
        self,
        request: Request,
        error: Exception,
        *,
        retry: bool = True,
    ) -> None:
        """Mark *request* as failed.

        If *retry* is True and the policy permits it, re-enqueue with backoff.
        Otherwise move to dead_letter.
        """
        h = self._hash(request.url)
        self._processing.pop(h, None)

        can_retry = (
            retry
            and self._retry_policy.should_retry(error)
            and request.retry_count < self._retry_policy.max_retries
        )

        if can_retry:
            delay = self._retry_policy.backoff_delay(request.retry_count + 1)
            logger.debug(
                "Scheduling retry %d/%d for %s in %.1fs",
                request.retry_count + 1,
                self._retry_policy.max_retries,
                request.url,
                delay,
            )
            retried = request.with_retry()
            # Fire-and-forget delayed re-enqueue
            asyncio.get_event_loop().call_later(delay, self._requeue, retried)
        else:
            logger.debug("Dead-lettering %s (retry_count=%d)", request.url, request.retry_count)
            self._dead_letter.append(request)

    def _requeue(self, request: Request) -> None:
        """Re-insert a retried request into the pending queue (called via call_later)."""
        self._pending.put_nowait((-request.priority, time.monotonic(), request))

    async def peek_dead_letters(self) -> AsyncIterator[Request]:  # type: ignore[override]
        for req in list(self._dead_letter):
            yield req

    async def stats(self) -> QueueStats:
        return QueueStats(
            pending=self._pending.qsize(),
            processing=len(self._processing),
            done=len(self._done),
            failed=0,  # MemoryQueue has no persisted failed state; retries are pending
            dead_letter=len(self._dead_letter),
            total_seen=len(self._seen),
        )

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# SqliteQueue
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS requests (
    url_hash      TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    state         TEXT NOT NULL,
    method        TEXT NOT NULL,
    headers       TEXT,
    payload       BLOB,
    metadata      TEXT,
    label         TEXT,
    depth         INTEGER NOT NULL DEFAULT 0,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 0,
    enqueued_at   REAL NOT NULL,
    updated_at    REAL NOT NULL,
    next_retry_at REAL
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_state_priority
    ON requests(state, priority DESC, enqueued_at ASC);

CREATE INDEX IF NOT EXISTS idx_next_retry
    ON requests(next_retry_at) WHERE state = 'failed';
"""

_RESET_STALE_PROCESSING = """
UPDATE requests
SET state = 'pending',
    retry_count = retry_count + 1,
    updated_at = :now
WHERE state = 'processing'
  AND updated_at < :cutoff;
"""

_POP_PENDING = """
SELECT url_hash, url, method, headers, payload, metadata,
       label, depth, retry_count, priority, enqueued_at
FROM requests
WHERE state = 'pending'
ORDER BY priority DESC, enqueued_at ASC
LIMIT 1;
"""


class SqliteQueue:
    """Persistent queue backed by an aiosqlite database.

    Supports crash recovery: any *processing* rows older than
    *processing_timeout* seconds are reset to *pending* on startup.
    """

    def __init__(
        self,
        db_path: str,
        retry_policy: RetryPolicy | None = None,
        strip_params: list[str] | None = None,
        hash_bytes: int = 16,
        processing_timeout: float = 300.0,
    ) -> None:
        if not HAS_AIOSQLITE:
            raise RuntimeError(
                "SqliteQueue requires aiosqlite. Install it with: pip install aiosqlite"
            )
        self._db_path = db_path
        self._retry_policy = retry_policy or RetryPolicy()
        self._strip_params = strip_params if strip_params is not None else TRACKING_PARAMS
        self._hash_bytes = hash_bytes
        self._processing_timeout = processing_timeout
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the database connection, create schema, and run crash recovery."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.execute(_CREATE_TABLE)
        for stmt in _CREATE_INDEXES.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._db.execute(stmt)
        await self._db.commit()

        await self._recover()

    async def _recover(self) -> None:
        """Reset stale processing rows to pending (crash recovery)."""
        now = time.time()
        cutoff = now - self._processing_timeout
        async with self._db.execute(
            _RESET_STALE_PROCESSING, {"now": now, "cutoff": cutoff}
        ) as cur:
            rows_reset = cur.rowcount
        await self._db.commit()
        if rows_reset:
            logger.info("Crash recovery: reset %d stale processing rows to pending", rows_reset)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _hash(self, url: str) -> str:
        return url_hash(url, strip_params=self._strip_params, hash_bytes=self._hash_bytes)

    def _assert_open(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SqliteQueue is not open. Call await queue.open() first.")
        return self._db

    @staticmethod
    def _serialise_request(h: str, request: Request, state: str, now: float, next_retry_at: float | None = None) -> dict[str, Any]:
        return {
            "url_hash": h,
            "url": request.url,
            "state": state,
            "method": request.method,
            "headers": json.dumps(request.headers) if request.headers else None,
            "payload": request.payload,
            "metadata": json.dumps(request.metadata) if request.metadata else None,
            "label": request.label,
            "depth": request.depth,
            "retry_count": request.retry_count,
            "priority": request.priority,
            "enqueued_at": now,
            "updated_at": now,
            "next_retry_at": next_retry_at,
        }

    @staticmethod
    def _row_to_request(row: aiosqlite.Row) -> Request:
        headers: dict[str, str] = json.loads(row["headers"]) if row["headers"] else {}
        metadata: dict[str, Any] = json.loads(row["metadata"]) if row["metadata"] else {}
        return Request(
            url=row["url"],
            method=row["method"],
            headers=headers,
            payload=row["payload"],
            metadata=metadata,
            label=row["label"],
            depth=row["depth"],
            retry_count=row["retry_count"],
            priority=row["priority"],
        )

    # ------------------------------------------------------------------
    # QueueBackend protocol
    # ------------------------------------------------------------------

    async def push(self, request: Request) -> bool:
        """Enqueue *request*.

        Returns False if the URL was deduplicated (already seen).
        Returns True if accepted into pending.
        """
        db = self._assert_open()
        h = self._hash(request.url)

        if not request.no_dedupe:
            async with db.execute(
                "SELECT 1 FROM requests WHERE url_hash = ?", (h,)
            ) as cur:
                if await cur.fetchone() is not None:
                    logger.debug("Deduplicated %s", request.url)
                    return False

        now = time.time()
        row = self._serialise_request(h, request, "pending", now)
        await db.execute(
            """
            INSERT INTO requests
                (url_hash, url, state, method, headers, payload, metadata,
                 label, depth, retry_count, priority, enqueued_at, updated_at, next_retry_at)
            VALUES
                (:url_hash, :url, :state, :method, :headers, :payload, :metadata,
                 :label, :depth, :retry_count, :priority, :enqueued_at, :updated_at, :next_retry_at)
            ON CONFLICT(url_hash) DO NOTHING
            """,
            row,
        )
        await db.commit()
        return True

    async def push_many(self, requests: Iterable[Request]) -> int:
        count = 0
        for req in requests:
            if await self.push(req):
                count += 1
        return count

    async def pop(self) -> Request | None:
        """Return the next pending request or None if the queue is empty.

        Atomically moves the request to *processing* state.
        """
        db = self._assert_open()
        now = time.time()

        async with db.execute(_POP_PENDING) as cur:
            row = await cur.fetchone()

        if row is None:
            # Check for failed rows whose retry delay has elapsed
            async with db.execute(
                """
                SELECT url_hash, url, method, headers, payload, metadata,
                       label, depth, retry_count, priority, enqueued_at
                FROM requests
                WHERE state = 'failed'
                  AND next_retry_at <= :now
                ORDER BY priority DESC, enqueued_at ASC
                LIMIT 1
                """,
                {"now": now},
            ) as cur2:
                row = await cur2.fetchone()

        if row is None:
            return None

        url_hash_val: str = row["url_hash"]
        await db.execute(
            "UPDATE requests SET state = 'processing', updated_at = ? WHERE url_hash = ?",
            (now, url_hash_val),
        )
        await db.commit()

        return self._row_to_request(row)

    async def ack(self, request: Request) -> None:
        """Mark *request* as successfully completed (state → done)."""
        db = self._assert_open()
        h = self._hash(request.url)
        now = time.time()
        await db.execute(
            "UPDATE requests SET state = 'done', updated_at = ? WHERE url_hash = ?",
            (now, h),
        )
        await db.commit()

    async def nack(
        self,
        request: Request,
        error: Exception,
        *,
        retry: bool = True,
    ) -> None:
        """Mark *request* as failed.

        Moves to *failed* (with a next_retry_at timestamp) when a retry should
        be attempted, or to *dead_letter* when retries are exhausted.
        """
        db = self._assert_open()
        h = self._hash(request.url)
        now = time.time()

        can_retry = (
            retry
            and self._retry_policy.should_retry(error)
            and request.retry_count < self._retry_policy.max_retries
        )

        if can_retry:
            delay = self._retry_policy.backoff_delay(request.retry_count + 1)
            next_retry_at = now + delay
            logger.debug(
                "Scheduling retry %d/%d for %s in %.1fs",
                request.retry_count + 1,
                self._retry_policy.max_retries,
                request.url,
                delay,
            )
            await db.execute(
                """
                UPDATE requests
                SET state = 'failed',
                    retry_count = retry_count + 1,
                    updated_at = ?,
                    next_retry_at = ?
                WHERE url_hash = ?
                """,
                (now, next_retry_at, h),
            )
        else:
            logger.debug(
                "Dead-lettering %s (retry_count=%d)", request.url, request.retry_count
            )
            await db.execute(
                """
                UPDATE requests
                SET state = 'dead_letter',
                    updated_at = ?,
                    next_retry_at = NULL
                WHERE url_hash = ?
                """,
                (now, h),
            )
        await db.commit()

    async def peek_dead_letters(self) -> AsyncIterator[Request]:  # type: ignore[override]
        db = self._assert_open()
        async with db.execute(
            "SELECT url_hash, url, method, headers, payload, metadata, "
            "label, depth, retry_count, priority, enqueued_at "
            "FROM requests WHERE state = 'dead_letter' ORDER BY updated_at ASC"
        ) as cur:
            async for row in cur:
                yield self._row_to_request(row)

    async def stats(self) -> QueueStats:
        db = self._assert_open()
        counts: dict[str, int] = {}
        async with db.execute(
            "SELECT state, COUNT(*) as cnt FROM requests GROUP BY state"
        ) as cur:
            async for row in cur:
                counts[row["state"]] = row["cnt"]

        total_seen = sum(counts.values())
        return QueueStats(
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            done=counts.get("done", 0),
            failed=counts.get("failed", 0),
            dead_letter=counts.get("dead_letter", 0),
            total_seen=total_seen,
        )
