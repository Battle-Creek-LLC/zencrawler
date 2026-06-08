# Browser Pool

Manages Chrome process lifecycle. Responsible for checkout, return, crash recovery,
idle cleanup, and graceful shutdown. Users never interact with this directly.

---

## Design Decision: Process vs Context Reuse

| Strategy | Startup cost | Isolation | Chosen? |
|---|---|---|---|
| Fresh Chrome process per request | ~500–800ms | Perfect | No — too slow |
| Reuse process, reuse context | ~0ms | None (cookie bleed) | No — unsafe |
| Reuse process, fresh CDP context | ~10ms | Good (incognito profile) | **Yes** |

**Chosen:** reuse the Chrome process, open a new `BrowserContext` (incognito) per
request. The context is closed on return, not reused.

Why a fresh context matters: cookies, localStorage, and cached credentials are
scoped to a `BrowserContext`. Without isolation, request A's session bleeds into
request B. This is unacceptable for correctness and may cause bot-detection
(unexpected auth state).

---

## Configuration

```python
@dataclass
class BrowserPoolConfig:
    min_size:        int   = 1       # browsers kept alive even when idle
    max_size:        int   = 5       # hard ceiling on concurrent browsers
    idle_timeout:    float = 60.0    # seconds before idle browser is closed
    launch_timeout:  float = 30.0    # fail request if browser won't start in time
    crash_max_retries: int = 3       # max browser replacement attempts before giving up
    headless:        bool  = True
    launch_args:     list[str] = field(default_factory=list)  # passed to Chrome
```

**`launch_args`** examples:
```python
launch_args = [
    "--disable-gpu",
    "--no-sandbox",              # required in Docker
    "--disable-dev-shm-usage",   # required in Docker (shared memory)
    "--window-size=1920,1080",
]
```

Docker containers require `--no-sandbox` and `--disable-dev-shm-usage`. These are
not injected automatically — the user opts in via `launch_args` to avoid hidden
behaviour changes.

---

## Pool State

```
                  ┌──────────────────────────────┐
                  │           BrowserPool         │
                  │                              │
                  │  idle:   [Browser, Browser]  │
                  │  active: {Browser: Request}  │
                  │  slots:  Semaphore(max_size) │
                  └──────────────────────────────┘
```

- `idle`: browsers waiting for work (process alive, no active context)
- `active`: browsers currently running a handler
- `slots`: semaphore; acquiring blocks until a browser slot is available

---

## Checkout Flow

```
acquire(request)
  1. await semaphore.acquire()              # blocks if all slots taken
  2. browser = idle.pop() if idle else None
  3. if browser is None:
       browser = await launch_new_browser()  # may raise LaunchTimeoutError
  4. context = await browser.new_context()   # fresh incognito BrowserContext
  5. page    = await context.new_page()
  6. active[browser] = request
  7. return BrowserHandle(browser, context, page)
```

`LaunchTimeoutError` triggers `nack(request, retry=True)` in the caller — the
request re-enters the queue with `retry_count += 1`.

---

## Return Flow

```
release(handle, *, crashed: bool = False)
  1. if crashed:
       log warning with request URL
       discard handle (don't return browser to idle)
       asyncio.create_task(_replace_browser())   # async, non-blocking
  2. else:
       await handle.context.close()              # discards cookies, cache
       del active[handle.browser]
       if len(idle) < min_size or not _over_capacity():
           idle.append(handle.browser)
       else:
           await handle.browser.close()          # we have enough idle browsers
  3. semaphore.release()
```

`_replace_browser()` launches a new browser and adds it to idle so the pool
stays at capacity after a crash.

---

## Crash Detection

Two independent signals:

**1. Process monitor** (started at browser launch):
```python
async def _monitor_process(browser, handle):
    await browser.process.wait()           # blocks until process exits
    if handle in active:
        await _on_crash(handle)
```

**2. CDP disconnect** (zendriver emits this):
```python
browser.on("disconnected", lambda: asyncio.create_task(_on_crash(handle)))
```

`_on_crash(handle)`:
1. Mark handle as crashed
2. Call `release(handle, crashed=True)`
3. Re-queue the in-flight request (if `retry_count < max_retries`)
4. If `retry_count >= max_retries` → dead-letter

The two signals may both fire for the same crash. The second is a no-op because
`active` no longer contains the handle after the first fires.

---

## Idle Timeout

Background reaper task — runs every `idle_timeout / 2` seconds:

```python
async def _reaper():
    while not _shutting_down:
        await asyncio.sleep(idle_timeout / 2)
        now = monotonic()
        for browser in list(idle):
            if now - browser.last_used > idle_timeout:
                if len(idle) > min_size:   # never reap below min_size
                    idle.remove(browser)
                    await browser.close()
```

`min_size` browsers are exempt from reaping. This ensures at least one browser
is always warm, reducing latency for the next request.

---

## Graceful Shutdown

Called by `Crawler.__aexit__`. Sequence:

```
1. Set _shutting_down = True
2. Stop accepting new requests (close queue input)
3. await asyncio.wait_for(
       _all_active_tasks_done(),
       timeout=shutdown_timeout        # default 30s
   )
4. Cancel reaper task
5. for browser in (idle + active.keys()):
       await browser.close()
6. Log count of requests that were in-flight at shutdown
```

If `shutdown_timeout` expires, in-flight requests are abandoned. Their queue
entries remain in state `processing` — if the queue is SQLite-backed, they will
be recovered as `pending` on the next run (see [`request-queue.md`](request-queue.md)).

---

## Sticky Sessions (Deferred to v2)

Some sites require maintaining cookies across a sequence of requests (login →
authenticated pages). The current design does not support this — each request
gets a fresh context.

Planned v2 primitive: `SessionGroup`

```python
# v2 concept — not in v1
@dataclass
class SessionGroup:
    label: str              # requests with this label share a context
    context: BrowserContext # reused across requests
    last_used: float
```

Requests tagged `sticky=True` (or by label) would be routed to a context from
the session pool rather than a fresh incognito context.

[SCOPE RISK]: Sticky sessions require a separate pool and complicate crash recovery —
a crashed context invalidates all pending sticky requests sharing it.
