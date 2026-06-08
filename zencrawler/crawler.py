from __future__ import annotations
import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, AsyncIterator, Callable, Coroutine, Iterable, Literal

from .context import AfterHookContext, CrawlContext, HookContext
from .errors import (
    BotBlockError, BrowserCrashError, CrawlError, HandlerError,
    LaunchTimeoutError, SkipRequest, UnhandledRequestError,
)
from .pool import BrowserPool, BrowserPoolConfig
from .queue import MemoryQueue, RetryPolicy, SqliteQueue, url_hash, TRACKING_PARAMS
from .router import Router
from .storage import MemoryDataset, MemoryStore, SqliteStorageBackend
from .types import (
    CrawlResult, Dataset, ErrorAction, QueueBackend,
    QueueStats, Request, Store, StorageBackend,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default bot-block signals
# ---------------------------------------------------------------------------

DEFAULT_BOT_SIGNALS = [
    "unusual traffic",
    "automated queries",
    "bot detected",
    "verify you are human",
    "sorry, you have been blocked",
    "your ip has been blocked",
    "enable javascript and cookies to continue",
]

DEFAULT_BOT_TITLE_SIGNALS = [
    "captcha",
    "access denied",
    "attention required",
    "just a moment",
    "security check",
]


# ---------------------------------------------------------------------------
# Env-var helper
# ---------------------------------------------------------------------------

def _env(name: str, default: Any, cast=str) -> Any:
    val = os.environ.get(f"ZENCRAWLER_{name}")
    if val is None:
        return default
    if cast == bool:
        return val.lower() in ("1", "true", "yes")
    return cast(val)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class Crawler:
    """
    Orchestrates the scheduling loop, browser pool, queue, router, and storage.

    Typical usage::

        router = Router()

        @router.on("https://example.com/**")
        async def scrape(ctx: CrawlContext) -> None:
            await ctx.dataset.push({"url": ctx.request.url})

        async with Crawler(router) as crawler:
            result = await crawler.run([Request(url="https://example.com/")])
    """

    def __init__(
        self,
        router: Router,
        max_concurrency: int = None,   # default from env or 5
        max_requests: int | None = None,
        max_depth: int | None = None,
        queue: Literal["memory", "sqlite"] | QueueBackend = None,  # default from env or "memory"
        retry_policy: RetryPolicy | None = None,
        storage: Literal["sqlite"] | StorageBackend = "sqlite",
        storage_path: Path = None,     # default from env or Path("./crawl_data")
        browser: BrowserPoolConfig | None = None,
        page_load_timeout: float = None,  # default from env or 30.0
        bot_signals: list[str] | None = None,
        extra_bot_signals: list[str] = [],
        dedupe_strip_params: list[str] | None = None,
        shutdown_timeout: float = None,  # default from env or 30.0
    ) -> None:
        # ── Apply env-var defaults ────────────────────────────────────────────
        if max_concurrency is None:
            max_concurrency = _env("MAX_CONCURRENCY", 5, int)
        if queue is None:
            queue = _env("QUEUE", "memory", str)
        if storage_path is None:
            storage_path = Path(_env("STORAGE_PATH", "./crawl_data", str))
        if page_load_timeout is None:
            page_load_timeout = _env("PAGE_LOAD_TIMEOUT", 30.0, float)
        if shutdown_timeout is None:
            shutdown_timeout = _env("SHUTDOWN_TIMEOUT", 30.0, float)
        if max_requests is None:
            env_val = os.environ.get("ZENCRAWLER_MAX_REQUESTS")
            max_requests = int(env_val) if env_val is not None else None
        if max_depth is None:
            env_val = os.environ.get("ZENCRAWLER_MAX_DEPTH")
            max_depth = int(env_val) if env_val is not None else None

        headless = _env("HEADLESS", True, bool)
        rate_rps = _env("RATE_RPS", None, float)
        rate_burst = _env("RATE_BURST", None, int)

        self._router = router
        self._max_concurrency = max_concurrency
        self._max_requests = max_requests
        self._max_depth = max_depth
        self._page_load_timeout = page_load_timeout
        self._shutdown_timeout = shutdown_timeout

        # ── Bot signals ───────────────────────────────────────────────────────
        if bot_signals is not None:
            self._bot_signals = [s.lower() for s in bot_signals]
        else:
            self._bot_signals = [s.lower() for s in DEFAULT_BOT_SIGNALS]
        # Merge extra signals
        self._bot_signals = self._bot_signals + [s.lower() for s in extra_bot_signals]
        self._bot_title_signals = [s.lower() for s in DEFAULT_BOT_TITLE_SIGNALS]

        # ── Deduplication params ──────────────────────────────────────────────
        self._dedupe_strip_params = dedupe_strip_params  # None means use default TRACKING_PARAMS

        # ── Rate limiting ─────────────────────────────────────────────────────
        self._rate_rps = rate_rps
        self._rate_burst = rate_burst
        self._rate_limiter: _TokenBucket | None = None
        if rate_rps is not None:
            burst = rate_burst if rate_burst is not None else max(1, int(rate_rps))
            self._rate_limiter = _TokenBucket(rate=rate_rps, burst=burst)

        # ── Queue ─────────────────────────────────────────────────────────────
        self._queue_spec = queue
        self._queue: QueueBackend | None = None
        self._retry_policy = retry_policy or RetryPolicy()

        # ── Storage ───────────────────────────────────────────────────────────
        self._storage_spec = storage
        self._storage_path = storage_path
        self._storage: StorageBackend | None = None

        # ── Browser pool ──────────────────────────────────────────────────────
        if browser is not None:
            self._pool_config = browser
        else:
            self._pool_config = BrowserPoolConfig(
                min_size=1,
                max_size=max_concurrency,
                headless=headless,
            )
        self._pool: BrowserPool | None = None

        # ── Runtime state ─────────────────────────────────────────────────────
        self._semaphore: asyncio.Semaphore | None = None
        self._shutting_down = False
        self._active_count = 0
        self._done_count = 0
        self._failed_count = 0
        self._items_pushed = 0
        self._large_crawl_warned = False

        # ── Stats callbacks ───────────────────────────────────────────────────
        self._stats_callbacks: list[Callable] = []

        # ── Context-manager guard ─────────────────────────────────────────────
        self._entered = False

    # =========================================================================
    # Context manager
    # =========================================================================

    async def __aenter__(self) -> "Crawler":
        if self._entered:
            raise RuntimeError("Crawler is not re-entrant; create a new instance")
        self._entered = True
        self._shutting_down = False
        self._done_count = 0
        self._failed_count = 0
        self._items_pushed = 0
        self._large_crawl_warned = False

        # Build the queue
        self._queue = self._build_queue()
        if hasattr(self._queue, "open"):
            await self._queue.open()  # type: ignore[attr-defined]

        # Build storage backend
        self._storage = self._build_storage()

        # Build the browser pool
        self._pool = BrowserPool(
            self._pool_config,
            on_crash=self._on_browser_crash,
        )
        await self._pool.start()

        # Semaphore controlling handler concurrency
        self._semaphore = asyncio.Semaphore(self._max_concurrency)

        # Register signal handlers (SIGTERM / SIGINT)
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except (NotImplementedError, OSError):
                # Windows / some event loops don't support add_signal_handler
                pass

        log.info(
            "Crawler started (concurrency=%d, queue=%s, storage=%s)",
            self._max_concurrency,
            type(self._queue).__name__,
            type(self._storage).__name__,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self._teardown()
        return False  # do not suppress exceptions

    # =========================================================================
    # Public API
    # =========================================================================

    async def run(
        self,
        requests: Iterable[Request | str],
        *,
        wait: bool = True,
    ) -> CrawlResult:
        """
        Enqueue *requests* and run the scheduling loop until the queue is
        drained (or a stop condition fires).

        Parameters
        ----------
        requests:
            Initial seeds.  Strings are converted to Request objects.
        wait:
            If True (the default) block until the crawl is complete.
            If False, start the loop and return immediately (the result will
            contain zeros).
        """
        if not self._entered:
            raise RuntimeError(
                "Crawler.run() must be called inside an `async with Crawler(...) as c:` block"
            )

        start = monotonic()

        # Seed the queue
        seed_requests = []
        for r in requests:
            if isinstance(r, str):
                seed_requests.append(Request(url=r))
            else:
                seed_requests.append(r)

        await self.enqueue_all(seed_requests)

        if not wait:
            return CrawlResult(elapsed_seconds=monotonic() - start)

        await self._run_loop()

        elapsed = monotonic() - start
        stats = await self._queue.stats()

        result = CrawlResult(
            requests_done=self._done_count,
            requests_failed=self._failed_count,
            requests_dead_letter=stats.dead_letter,
            elapsed_seconds=elapsed,
            items_pushed=self._items_pushed,
        )

        log.info(
            "Crawl complete — done=%d failed=%d dead_letter=%d items=%d elapsed=%.1fs",
            result.requests_done,
            result.requests_failed,
            result.requests_dead_letter,
            result.items_pushed,
            result.elapsed_seconds,
        )

        # Notify stats callbacks
        for cb in self._stats_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(result)
                else:
                    cb(result)
            except Exception:
                log.warning("Stats callback %r raised", cb, exc_info=True)

        return result

    async def enqueue(self, request: Request | str) -> bool:
        """
        Push a single request into the queue.

        Returns True if the request was accepted, False if it was
        deduplicated.
        """
        if isinstance(request, str):
            request = Request(url=request)

        if self._max_depth is not None and request.depth > self._max_depth:
            log.debug("Dropping %s — depth %d > max_depth %d", request.url, request.depth, self._max_depth)
            return False

        if self._max_requests is not None and self._done_count >= self._max_requests:
            log.debug("Dropping %s — max_requests %d reached", request.url, self._max_requests)
            return False

        accepted = await self._queue.push(request)

        if accepted:
            self._check_large_crawl_warning()

        return accepted

    async def enqueue_all(self, requests: Iterable[Request | str]) -> int:
        """
        Push multiple requests.

        Returns the count of requests that were accepted (not deduplicated).
        """
        count = 0
        for req in requests:
            if await self.enqueue(req):
                count += 1
        return count

    @property
    def dataset(self) -> Dataset:
        """The default dataset (named 'default')."""
        return self.get_dataset("default")

    @property
    def store(self) -> Store:
        """The default key-value store (named 'default')."""
        return self.get_store("default")

    def get_dataset(self, name: str) -> Dataset:
        """Return (creating if necessary) the named dataset."""
        if self._storage is None:
            raise RuntimeError("Crawler storage is not initialised — use async with Crawler(...) as c:")
        return self._storage.dataset(name)

    def get_store(self, name: str) -> Store:
        """Return (creating if necessary) the named key-value store."""
        if self._storage is None:
            raise RuntimeError("Crawler storage is not initialised — use async with Crawler(...) as c:")
        return self._storage.store(name)

    async def queue_stats(self) -> QueueStats:
        """Return current queue statistics."""
        if self._queue is None:
            return QueueStats()
        return await self._queue.stats()

    async def dead_letter_count(self) -> int:
        """Return the number of requests in the dead-letter state."""
        stats = await self.queue_stats()
        return stats.dead_letter

    async def dead_letters(self) -> AsyncIterator[Request]:
        """Async-iterate over all dead-lettered requests."""
        if self._queue is None:
            return
        async for req in self._queue.peek_dead_letters():
            yield req

    def on_stats(self, callback: Callable) -> None:
        """
        Register a callback to be called with the CrawlResult at the end of
        each run().  May be sync or async.
        """
        self._stats_callbacks.append(callback)

    # =========================================================================
    # Internal — queue / storage factory
    # =========================================================================

    def _build_queue(self) -> QueueBackend:
        spec = self._queue_spec

        if isinstance(spec, str):
            if spec == "memory":
                return MemoryQueue(
                    retry_policy=self._retry_policy,
                    strip_params=self._dedupe_strip_params,
                )
            elif spec == "sqlite":
                db_path = self._storage_path / "queue.db"
                db_path.parent.mkdir(parents=True, exist_ok=True)
                return SqliteQueue(
                    db_path=str(db_path),
                    retry_policy=self._retry_policy,
                    strip_params=self._dedupe_strip_params,
                )
            else:
                raise ValueError(f"Unknown queue type: {spec!r}. Use 'memory' or 'sqlite'.")
        else:
            # Assumed to implement QueueBackend protocol
            return spec  # type: ignore[return-value]

    def _build_storage(self) -> StorageBackend:
        spec = self._storage_spec

        if isinstance(spec, str):
            if spec == "sqlite":
                self._storage_path.mkdir(parents=True, exist_ok=True)
                storage_db = self._storage_path / "storage.db"
                return SqliteStorageBackend(path=storage_db)
            else:
                raise ValueError(f"Unknown storage type: {spec!r}. Use 'sqlite'.")
        else:
            return spec  # type: ignore[return-value]

    # =========================================================================
    # Internal — scheduling loop
    # =========================================================================

    async def _run_loop(self) -> None:
        """Main scheduling loop — pulls from the queue and dispatches handlers."""
        log.debug("Scheduling loop started")

        pending_tasks: set[asyncio.Task] = set()

        while not self._shutting_down:
            # Check stop conditions
            if self._max_requests is not None and self._done_count >= self._max_requests:
                log.info("max_requests=%d reached — stopping loop", self._max_requests)
                break

            # If nothing is active and the queue is empty, we're done
            if self._active_count == 0 and len(pending_tasks) == 0:
                # Do a final check: try to pop from the queue
                try:
                    request = await asyncio.wait_for(
                        self._pop_request(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    request = None

                if request is None:
                    # Queue is empty and nothing in-flight
                    break

                # We got a request — process it
                await self._semaphore.acquire()
                self._active_count += 1
                task = asyncio.create_task(
                    self._run_handler(request),
                    name=f"handler-{request.url}",
                )
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)
                continue

            # Try to pop the next request without blocking long
            try:
                request = await asyncio.wait_for(
                    self._pop_request(), timeout=0.5
                )
            except asyncio.TimeoutError:
                request = None

            if request is None:
                if self._active_count == 0 and len(pending_tasks) == 0:
                    break
                # Nothing to pop right now; yield and retry
                await asyncio.sleep(0.05)
                continue

            # Rate limiting
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire()

            await self._semaphore.acquire()
            self._active_count += 1
            task = asyncio.create_task(
                self._run_handler(request),
                name=f"handler-{request.url}",
            )
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        # Wait for all in-flight handlers to finish
        if pending_tasks:
            log.debug("Waiting for %d in-flight handlers to complete", len(pending_tasks))
            deadline = monotonic() + self._shutdown_timeout
            remaining = list(pending_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*remaining, return_exceptions=True),
                    timeout=max(0.0, deadline - monotonic()),
                )
            except asyncio.TimeoutError:
                log.warning(
                    "%d handler(s) did not finish within shutdown_timeout — cancelling",
                    len(pending_tasks),
                )
                for task in pending_tasks:
                    task.cancel()

        log.debug("Scheduling loop finished")

    async def _pop_request(self) -> Request | None:
        """Pop the next request from the queue, returning None if empty."""
        return await self._queue.pop()

    # =========================================================================
    # Internal — handler execution
    # =========================================================================

    async def _run_handler(self, request: Request) -> None:
        """
        Execute the full lifecycle for a single request:

        1. Run before_request hooks (may replace the request).
        2. Acquire a browser from the pool.
        3. Navigate to request.url.
        4. Check for bot-block signals.
        5. Dispatch to the router handler.
        6. Ack / nack the queue entry.
        7. Run after_request hooks.
        8. Release the browser.
        """
        handler_start = monotonic()
        page = None
        handle = None
        handler_error: Exception | None = None

        try:
            # ── 1. Before-request hooks ───────────────────────────────────────
            hook_ctx = HookContext(
                request=request,
                log=logging.getLogger(f"zencrawler.handler.{request.label or 'default'}"),
                store=self.get_store("default"),
            )
            try:
                replacement = await self._router.run_before_hooks(hook_ctx)
                if replacement is not None:
                    request = replacement
            except SkipRequest:
                log.debug("before_request hook raised SkipRequest for %s", request.url)
                await self._queue.nack(request, SkipRequest(), retry=False)
                return
            except Exception as exc:
                log.error("before_request hook raised for %s: %r", request.url, exc, exc_info=True)
                await self._queue.nack(request, exc, retry=True)
                self._failed_count += 1
                return

            # ── 2. Acquire browser ────────────────────────────────────────────
            try:
                handle = await self._pool.checkout(request)
                page = handle.page
            except Exception as exc:
                log.error("Failed to acquire browser for %s: %r", request.url, exc)
                await self._queue.nack(request, exc, retry=True)
                self._failed_count += 1
                return

            # ── 3. Navigate ───────────────────────────────────────────────────
            try:
                await asyncio.wait_for(
                    page.get(request.url),
                    timeout=self._page_load_timeout,
                )
            except asyncio.TimeoutError as exc:
                from .errors import TimeoutError as CrawlTimeoutError
                crawl_exc = CrawlTimeoutError(
                    f"Page load timed out after {self._page_load_timeout}s for {request.url}",
                    request=request,
                    cause=exc,
                )
                handler_error = crawl_exc
                action = await self._resolve_error_action(request, page, crawl_exc)
                await self._apply_error_action(action, request, crawl_exc)
                return
            except Exception as exc:
                from .errors import NavigationError
                crawl_exc = NavigationError(
                    f"Navigation failed for {request.url}: {exc}",
                    request=request,
                    cause=exc,
                )
                handler_error = crawl_exc
                action = await self._resolve_error_action(request, page, crawl_exc)
                await self._apply_error_action(action, request, crawl_exc)
                return

            # ── 4. Bot-block check ────────────────────────────────────────────
            try:
                await self._check_bot_block(page, request)
            except BotBlockError as exc:
                log.warning("Bot block detected for %s — signal: %r", request.url, exc.signal)
                handler_error = exc
                action = await self._resolve_error_action(request, page, exc)
                await self._apply_error_action(action, request, exc)
                return

            # ── 5. Dispatch handler ───────────────────────────────────────────
            crawl_ctx = CrawlContext(
                page=page,
                request=request,
                _enqueue_fn=self.enqueue,
                _dataset_fn=self.get_dataset,
                _store_fn=self.get_store,
                _default_dataset=self.get_dataset("default"),
                _default_store=self.get_store("default"),
                log=logging.getLogger(f"zencrawler.handler.{request.label or 'default'}"),
            )

            try:
                await self._router.dispatch(crawl_ctx)
            except SkipRequest as exc:
                log.debug("Handler raised SkipRequest for %s: %s", request.url, exc)
                await self._queue.nack(request, exc, retry=False)
                return
            except UnhandledRequestError as exc:
                log.warning("No handler for %s", request.url)
                await self._queue.nack(request, exc, retry=False)
                return
            except Exception as exc:
                wrapped = HandlerError(
                    f"Handler raised for {request.url}: {exc}",
                    request=request,
                    cause=exc,
                )
                handler_error = wrapped
                action = await self._resolve_error_action(request, page, wrapped)
                await self._apply_error_action(action, request, wrapped)
                return

            # ── 6. Ack success ────────────────────────────────────────────────
            await self._queue.ack(request)
            self._done_count += 1
            log.debug("Completed %s in %.2fs", request.url, monotonic() - handler_start)

        except BrowserCrashError as exc:
            handler_error = exc
            log.warning("Browser crashed processing %s", request.url)
            try:
                await self._queue.nack(request, exc, retry=True)
                self._failed_count += 1
            except Exception:
                pass

        except asyncio.CancelledError:
            # Shutting down — nack without retry so state is consistent
            if request is not None:
                try:
                    await self._queue.nack(request, CrawlError("Cancelled"), retry=False)
                except Exception:
                    pass
            raise

        except Exception as exc:
            handler_error = exc
            log.error("Unexpected error processing %s: %r", request.url, exc, exc_info=True)
            try:
                await self._queue.nack(request, exc, retry=True)
                self._failed_count += 1
            except Exception:
                pass

        finally:
            elapsed = monotonic() - handler_start

            # ── 7. After-request hooks ────────────────────────────────────────
            if page is not None:
                after_ctx = AfterHookContext(
                    request=request,
                    log=logging.getLogger(f"zencrawler.handler.{request.label or 'default'}"),
                    store=self.get_store("default"),
                    page=page,
                    error=handler_error,
                    elapsed=elapsed,
                )
                try:
                    await self._router.run_after_hooks(after_ctx)
                except Exception:
                    log.error("after_request hook raised unexpectedly", exc_info=True)

            # ── 8. Release browser ────────────────────────────────────────────
            if handle is not None:
                try:
                    crashed = handle.crashed
                    await self._pool.release(handle, crashed=crashed)
                except Exception:
                    log.debug("Error releasing browser handle", exc_info=True)

            # Release the concurrency semaphore and decrement active count
            self._active_count -= 1
            self._semaphore.release()

    # =========================================================================
    # Internal — bot-block detection
    # =========================================================================

    async def _check_bot_block(self, page: Any, request: Request) -> None:
        """
        Inspect the page title and body content for bot-block signals.

        Raises BotBlockError if a signal is matched.
        """
        # Check page title
        try:
            title: str = await page.evaluate("document.title") or ""
            title_lower = title.lower()
            for signal in self._bot_title_signals:
                if signal in title_lower:
                    raise BotBlockError(
                        f"Bot block detected in page title for {request.url}",
                        signal=signal,
                        page_title=title,
                        request=request,
                    )
        except BotBlockError:
            raise
        except Exception:
            log.debug("Could not read page title for bot-block check", exc_info=True)

        # Check page content
        try:
            content: str = await page.get_content() or ""
            content_lower = content.lower()
            for signal in self._bot_signals:
                if signal in content_lower:
                    raise BotBlockError(
                        f"Bot block detected in page content for {request.url}",
                        signal=signal,
                        request=request,
                    )
        except BotBlockError:
            raise
        except Exception:
            log.debug("Could not read page content for bot-block check", exc_info=True)

    # =========================================================================
    # Internal — error handling
    # =========================================================================

    async def _resolve_error_action(
        self,
        request: Request,
        page: Any | None,
        error: Exception,
    ) -> ErrorAction:
        """
        Determine what to do with a failed request by consulting the router's
        error hooks, then falling back to the router's default action.
        """
        if page is not None:
            crawl_ctx = CrawlContext(
                page=page,
                request=request,
                _enqueue_fn=self.enqueue,
                _dataset_fn=self.get_dataset,
                _store_fn=self.get_store,
                _default_dataset=self.get_dataset("default"),
                _default_store=self.get_store("default"),
                log=logging.getLogger(f"zencrawler.handler.{request.label or 'default'}"),
            )
            return await self._router.dispatch_error(crawl_ctx, error)
        else:
            return self._router._default_error_action(error)

    async def _apply_error_action(
        self,
        action: ErrorAction,
        request: Request,
        error: Exception,
    ) -> None:
        """Apply the resolved ErrorAction to the queue entry."""
        if action == ErrorAction.RETRY:
            await self._queue.nack(request, error, retry=True)
            self._failed_count += 1
        elif action == ErrorAction.SKIP:
            await self._queue.nack(request, error, retry=False)
            # Skipped — not counted as failed
        elif action == ErrorAction.DEAD_LETTER:
            await self._queue.nack(request, error, retry=False)
            self._failed_count += 1
        elif action == ErrorAction.RAISE:
            await self._queue.nack(request, error, retry=False)
            self._failed_count += 1
            raise error
        else:
            # Unknown action — default to dead-letter
            await self._queue.nack(request, error, retry=False)
            self._failed_count += 1

    # =========================================================================
    # Internal — browser crash handler
    # =========================================================================

    async def _on_browser_crash(self, handle: Any) -> None:
        """Called by BrowserPool when a browser crashes mid-request."""
        request = getattr(handle, "request", None)
        if request is not None:
            log.warning(
                "Browser crash during %s — re-queuing with retry",
                request.url,
            )
            try:
                await self._queue.nack(
                    request,
                    BrowserCrashError("Browser crashed", request=request),
                    retry=True,
                )
            except Exception:
                log.error("Failed to nack request after browser crash", exc_info=True)

    # =========================================================================
    # Internal — large-crawl warning
    # =========================================================================

    def _check_large_crawl_warning(self) -> None:
        """Emit a one-time warning when using memory queue without a request cap and crawl is large."""
        if self._large_crawl_warned:
            return
        if self._queue_spec != "memory":
            return
        if self._max_requests is not None:
            return
        if self._done_count > 1000:
            self._large_crawl_warned = True
            log.warning(
                "Large crawl detected (>1000 requests) without SQLite queue — "
                "progress will be lost on crash. Consider queue='sqlite'."
            )

    # =========================================================================
    # Internal — signal handling
    # =========================================================================

    def _handle_signal(self) -> None:
        """Handle SIGTERM / SIGINT by setting the shutdown flag."""
        log.info("Shutdown signal received — draining queue and stopping")
        self._shutting_down = True

    # =========================================================================
    # Internal — teardown
    # =========================================================================

    async def _teardown(self) -> None:
        """Shut down all components in the correct order."""
        self._shutting_down = True

        if self._pool is not None:
            try:
                await self._pool.close(shutdown_timeout=self._shutdown_timeout)
            except Exception:
                log.warning("Error closing browser pool during teardown", exc_info=True)
            finally:
                self._pool = None

        if self._queue is not None:
            try:
                await self._queue.close()
            except Exception:
                log.warning("Error closing queue during teardown", exc_info=True)
            finally:
                self._queue = None

        if self._storage is not None:
            try:
                await self._storage.close()
            except Exception:
                log.warning("Error closing storage during teardown", exc_info=True)
            finally:
                self._storage = None

        self._entered = False
        log.debug("Crawler teardown complete")


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (simple, asyncio-native)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """
    Async token-bucket rate limiter.

    Parameters
    ----------
    rate:
        Token refill rate in tokens per second.
    burst:
        Maximum number of tokens the bucket can hold (peak capacity).
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens: float = float(burst)
        self._last_refill: float = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            async with self._lock:
                now = monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._burst),
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Calculate how long until one token is available
                wait = (1.0 - self._tokens) / self._rate

            await asyncio.sleep(wait)
