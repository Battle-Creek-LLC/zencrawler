from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import zendriver

from .types import Request

log = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass
class BrowserPoolConfig:
    min_size:          int        = 1
    max_size:          int        = 5
    idle_timeout:      float      = 60.0
    launch_timeout:    float      = 30.0
    crash_max_retries: int        = 3
    headless:          bool       = True
    launch_args:       list[str]  = field(default_factory=list)


# ── Handle ─────────────────────────────────────────────────────────────────────

@dataclass
class BrowserHandle:
    browser:   Any          # zendriver.Browser
    page:      Any          # zendriver.Tab
    request:   Request
    last_used: float = field(default_factory=monotonic)
    crashed:   bool  = False


# ── Exceptions ─────────────────────────────────────────────────────────────────

class LaunchTimeoutError(Exception):
    """Raised when a browser process does not start within launch_timeout."""


class PoolShuttingDownError(Exception):
    """Raised when checkout is attempted during shutdown."""


# ── Pool ───────────────────────────────────────────────────────────────────────

class BrowserPool:
    """
    Manages a pool of zendriver Browser instances.

    One browser process per concurrent request slot.  Idle browsers are kept
    alive to amortise the ~600 ms startup cost and are reaped after
    ``idle_timeout`` seconds of disuse (subject to ``min_size``).
    """

    def __init__(
        self,
        config: BrowserPoolConfig,
        *,
        on_crash: "Any | None" = None,
    ) -> None:
        """
        Parameters
        ----------
        config:
            Pool tuning knobs.
        on_crash:
            Optional async callable ``(handle: BrowserHandle) -> None`` called
            when a browser crashes while processing a request.  The pool's own
            crash handler fires first (to release the slot and replace the
            browser); this callback fires afterward for higher-level re-queuing
            logic.
        """
        self._config   = config
        self._on_crash = on_crash

        # Semaphore limits total active + idle browsers to max_size.
        self._slots    = asyncio.Semaphore(config.max_size)

        # Idle browsers waiting for work.
        self._idle:    list[Any]               = []   # list[zendriver.Browser]

        # Active handles indexed by browser object.
        self._active:  dict[Any, BrowserHandle] = {}  # {browser: BrowserHandle}

        # Tracks timestamps of last-return per browser for reaper.
        self._last_used: dict[Any, float] = {}

        self._shutting_down = False
        self._reaper_task: asyncio.Task[None] | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Warm the pool to ``min_size`` and launch the idle reaper."""
        log.debug("BrowserPool starting (min=%d, max=%d)", self._config.min_size, self._config.max_size)
        for _ in range(self._config.min_size):
            try:
                browser = await self._launch_browser()
                self._idle.append(browser)
                self._last_used[id(browser)] = monotonic()
                # Semaphore starts at max_size; consume one slot per warm browser.
                await self._slots.acquire()
                self._slots.release()
            except Exception:
                log.warning("Failed to pre-warm browser during pool start", exc_info=True)

        self._reaper_task = asyncio.create_task(self._reaper(), name="browser-pool-reaper")
        log.info("BrowserPool ready")

    async def close(self, *, shutdown_timeout: float = 30.0) -> None:
        """
        Graceful shutdown.

        1. Signals the pool to stop accepting checkouts.
        2. Waits up to *shutdown_timeout* for active browsers to be returned.
        3. Cancels the reaper.
        4. Closes all remaining browsers.
        """
        log.info("BrowserPool shutting down …")
        self._shutting_down = True

        if self._active:
            log.info("Waiting for %d in-flight browser(s) …", len(self._active))
            deadline = monotonic() + shutdown_timeout
            while self._active and monotonic() < deadline:
                await asyncio.sleep(0.2)
            if self._active:
                log.warning(
                    "%d browser(s) still active at shutdown timeout — abandoning",
                    len(self._active),
                )

        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

        all_browsers = list(self._idle) + list(self._active.keys())
        self._idle.clear()
        self._active.clear()

        for browser in all_browsers:
            await self._close_browser(browser)

        log.info("BrowserPool closed")

    # ── Public API ─────────────────────────────────────────────────────────────

    async def checkout(self, request: Request) -> BrowserHandle:
        """
        Acquire a browser for *request*.

        Blocks until a slot is available (semaphore) then returns (or launches)
        an idle browser with a clean ``about:blank`` tab.

        Raises
        ------
        PoolShuttingDownError
            If the pool is shutting down.
        LaunchTimeoutError
            If a new browser cannot be started within ``launch_timeout``.
        """
        if self._shutting_down:
            raise PoolShuttingDownError("BrowserPool is shutting down; cannot checkout")

        await self._slots.acquire()

        if self._shutting_down:
            # Raced with shutdown between the check above and the acquire.
            self._slots.release()
            raise PoolShuttingDownError("BrowserPool is shutting down; cannot checkout")

        try:
            browser = await self._get_or_launch()
            page    = await self._fresh_tab(browser)
        except Exception:
            self._slots.release()
            raise

        handle = BrowserHandle(browser=browser, page=page, request=request)
        self._active[browser] = handle
        self._attach_crash_monitor(browser, handle)

        log.debug("Checked out browser for %s (idle remaining: %d)", request.url, len(self._idle))
        return handle

    async def release(self, handle: BrowserHandle, *, crashed: bool = False) -> None:
        """
        Return *handle* to the pool.

        Parameters
        ----------
        handle:
            The handle returned by :meth:`checkout`.
        crashed:
            ``True`` when the browser process terminated unexpectedly.
            The browser is discarded and a replacement is launched in the
            background so the pool stays at capacity.
        """
        browser = handle.browser
        self._active.pop(browser, None)

        if crashed or handle.crashed:
            log.warning(
                "Browser crashed while processing %s — discarding",
                handle.request.url,
            )
            # Replace the dead browser asynchronously so we don't hold the caller.
            asyncio.create_task(
                self._replace_browser(),
                name="browser-pool-replace",
            )
            # Do NOT release the semaphore here — _replace_browser will do it
            # after the new browser is in idle (or give up and release).
            return

        # Happy path: close only the tab, keep the browser warm.
        try:
            # zendriver tabs can be closed via tab.close() if available.
            if hasattr(handle.page, "close"):
                await handle.page.close()
        except Exception:
            log.debug("Could not close tab on browser return", exc_info=True)

        now = monotonic()
        self._last_used[id(browser)] = now

        if len(self._idle) < self._config.min_size or not self._over_capacity():
            self._idle.append(browser)
            log.debug(
                "Browser returned to idle (idle: %d, active: %d)",
                len(self._idle),
                len(self._active),
            )
        else:
            log.debug("Pool over capacity — closing returned browser")
            await self._close_browser(browser)

        self._slots.release()

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _get_or_launch(self) -> Any:
        """Pop an idle browser or launch a new one."""
        if self._idle:
            browser = self._idle.pop()
            log.debug("Reusing idle browser (idle remaining: %d)", len(self._idle))
            return browser

        log.debug("No idle browsers — launching new browser")
        try:
            browser = await asyncio.wait_for(
                self._launch_browser(),
                timeout=self._config.launch_timeout,
            )
        except asyncio.TimeoutError:
            raise LaunchTimeoutError(
                f"Browser did not start within {self._config.launch_timeout}s"
            )
        return browser

    async def _launch_browser(self) -> Any:
        """Launch a single zendriver Browser instance."""
        config = zendriver.Config(
            headless=self._config.headless,
            browser_args=list(self._config.launch_args),
        )
        browser = await zendriver.start(config)
        log.debug("Launched browser pid=%s", getattr(browser, "pid", "?"))
        return browser

    async def _fresh_tab(self, browser: Any) -> Any:
        """Navigate to about:blank to get a clean tab."""
        tab = await browser.get("about:blank")
        return tab

    async def _close_browser(self, browser: Any) -> None:
        """Attempt to close *browser*, logging but not raising on failure."""
        self._last_used.pop(id(browser), None)
        try:
            await browser.stop()
        except Exception:
            log.debug("Error closing browser", exc_info=True)

    def _over_capacity(self) -> bool:
        """True when idle + active is already at or above max_size."""
        return (len(self._idle) + len(self._active)) >= self._config.max_size

    # ── Crash handling ─────────────────────────────────────────────────────────

    def _attach_crash_monitor(self, browser: Any, handle: BrowserHandle) -> None:
        """
        Wire up two independent crash signals for *browser*:

        1. Process exit monitor (asyncio task watching the subprocess).
        2. CDP disconnect event emitted by zendriver.
        """
        # Signal 1: process-exit monitor.
        proc = getattr(browser, "process", None)
        if proc is not None:
            asyncio.create_task(
                self._monitor_process(proc, handle),
                name="browser-pool-proc-monitor",
            )

        # Signal 2: CDP disconnect event (zendriver may call this "disconnected").
        def _on_disconnect() -> None:
            if not handle.crashed:
                asyncio.create_task(
                    self._on_crash_internal(handle),
                    name="browser-pool-disconnect-crash",
                )

        try:
            browser.on("disconnected", _on_disconnect)
        except Exception:
            # zendriver may not support .on() in all versions — non-fatal.
            log.debug("Could not attach disconnect listener", exc_info=True)

    async def _monitor_process(self, proc: Any, handle: BrowserHandle) -> None:
        """Wait for the browser subprocess to exit; trigger crash handler if active."""
        try:
            await proc.wait()
        except Exception:
            return

        if handle.browser in self._active and not handle.crashed:
            log.debug("Browser process exited unexpectedly — triggering crash handler")
            await self._on_crash_internal(handle)

    async def _on_crash_internal(self, handle: BrowserHandle) -> None:
        """
        Internal crash handler — idempotent (second call is a no-op because the
        handle will no longer be in ``_active`` after the first fires).
        """
        if handle.crashed:
            return
        if handle.browser not in self._active:
            return

        handle.crashed = True

        await self.release(handle, crashed=True)

        if self._on_crash is not None:
            try:
                await self._on_crash(handle)
            except Exception:
                log.warning("on_crash callback raised", exc_info=True)

    async def _replace_browser(self) -> None:
        """
        Launch a replacement browser and add it to idle.  Releases the semaphore
        slot once done (or on failure, so the slot is not leaked).
        """
        for attempt in range(1, self._config.crash_max_retries + 1):
            if self._shutting_down:
                self._slots.release()
                return
            try:
                browser = await asyncio.wait_for(
                    self._launch_browser(),
                    timeout=self._config.launch_timeout,
                )
                self._idle.append(browser)
                self._last_used[id(browser)] = monotonic()
                self._slots.release()
                log.info("Replacement browser launched after crash (attempt %d)", attempt)
                return
            except asyncio.TimeoutError:
                log.warning(
                    "Replacement browser launch timed out (attempt %d/%d)",
                    attempt,
                    self._config.crash_max_retries,
                )
            except Exception:
                log.warning(
                    "Replacement browser launch failed (attempt %d/%d)",
                    attempt,
                    self._config.crash_max_retries,
                    exc_info=True,
                )

        log.error(
            "Could not launch replacement browser after %d attempts — releasing slot without replacement",
            self._config.crash_max_retries,
        )
        self._slots.release()

    # ── Idle reaper ────────────────────────────────────────────────────────────

    async def _reaper(self) -> None:
        """Background task that closes browsers idle longer than idle_timeout."""
        interval = max(self._config.idle_timeout / 2, 1.0)
        log.debug("Idle reaper started (interval=%.1fs, timeout=%.1fs)", interval, self._config.idle_timeout)

        while not self._shutting_down:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

            if self._shutting_down:
                break

            now = monotonic()
            to_close: list[Any] = []

            for browser in list(self._idle):
                if len(self._idle) - len(to_close) <= self._config.min_size:
                    break  # never reap below min_size
                last = self._last_used.get(id(browser), now)
                if now - last > self._config.idle_timeout:
                    to_close.append(browser)

            for browser in to_close:
                self._idle.remove(browser)
                log.debug("Reaping idle browser (idle remaining: %d)", len(self._idle))
                await self._close_browser(browser)
                # Return the slot to the semaphore so new requests aren't blocked.
                self._slots.release()

        log.debug("Idle reaper stopped")
