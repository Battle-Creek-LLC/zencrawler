# Configuration

## RetryPolicy

Controls how failed requests are retried with exponential back-off.

```python
from zencrawler import RetryPolicy

policy = RetryPolicy(
    max_retries=5,
    backoff_base=2.0,
    backoff_max=120.0,
    backoff_jitter=0.1,
)
```

### Fields

`max_retries` *(int)*
:   Maximum number of retry attempts before moving the request to dead-letter.
    Default: `3`.

`backoff_base` *(float)*
:   Base of the exponential back-off formula: `base ^ retry_count` seconds.
    Default: `2.0` (1s, 2s, 4s, 8s, …).

`backoff_max` *(float)*
:   Upper bound on the delay between retries, in seconds.
    Default: `300.0` (5 minutes).

`backoff_jitter` *(float)*
:   Fraction of the computed delay added or subtracted randomly, to avoid thundering-herd.
    `0.1` means ±10%.
    Default: `0.1`.

`retry_on` *(tuple[type[Exception], ...])*
:   Exception types that should trigger a retry. If empty, the crawler's built-in defaults apply.
    Default: `()`.

`no_retry_on` *(tuple[type[Exception], ...])*
:   Exception types that should never be retried. Takes precedence over `retry_on`.
    Default: `()`.

### Methods

`should_retry(error)` → `bool`
:   Returns `True` if this error should be retried according to the policy.
    `no_retry_on` wins if the exception matches both lists.

`backoff_delay(retry_count)` → `float`
:   Returns the delay in seconds for the given retry attempt.

### Example

```python
from zencrawler import Crawler, RetryPolicy
from zencrawler.errors import BotBlockError, StructureError

async with Crawler(
    router=router,
    retry_policy=RetryPolicy(
        max_retries=5,
        backoff_base=3.0,
        backoff_max=300.0,
        no_retry_on=(BotBlockError,),   # never retry bot blocks
    ),
) as crawler:
    await crawler.run(seeds)
```

---

## BrowserPoolConfig

Tuning knobs for the Chrome browser pool.

```python
from zencrawler import BrowserPoolConfig

config = BrowserPoolConfig(
    min_size=2,
    max_size=10,
    headless=True,
)
```

Pass it to `Crawler` via… actually, `BrowserPoolConfig` is used internally by the `Crawler` and constructed from the Crawler's parameters (`max_concurrency`, `ZENCRAWLER_HEADLESS` env var, etc.). Export it for custom pool implementations.

### Fields

`min_size` *(int)*
:   Minimum number of browsers kept alive in the idle pool.
    Default: `1`.

`max_size` *(int)*
:   Maximum total browsers (idle + active). Maps to `max_concurrency` on the Crawler.
    Default: `5`.

`idle_timeout` *(float)*
:   Seconds a browser can be idle before the reaper closes it (subject to `min_size`).
    Default: `60.0`.

`launch_timeout` *(float)*
:   Seconds allowed for Chrome to start. Raises `LaunchTimeoutError` if exceeded.
    Default: `30.0`.

`crash_max_retries` *(int)*
:   How many times to attempt launching a replacement browser after a crash before giving up.
    Default: `3`.

`headless` *(bool)*
:   Run Chrome in headless mode. Set `ZENCRAWLER_HEADLESS=false` to open a visible browser window (useful for debugging).
    Default: `True`.

`launch_args` *(list[str])*
:   Additional command-line arguments passed to Chrome.

    ```python
    BrowserPoolConfig(
        launch_args=["--proxy-server=http://proxy.example.com:8080"],
    )
    ```

    Default: `[]`.

---

## Custom backends

### Custom queue

Implement the `QueueBackend` protocol:

```python
from zencrawler.types import QueueBackend, Request, QueueStats

class RedisQueue:
    async def push(self, request: Request) -> bool: ...
    async def push_many(self, requests) -> int: ...
    async def pop(self) -> Request | None: ...
    async def ack(self, request: Request) -> None: ...
    async def nack(self, request, error, *, retry=True) -> None: ...
    def peek_dead_letters(self): ...  # AsyncIterator[Request]
    async def stats(self) -> QueueStats: ...
    async def close(self) -> None: ...

async with Crawler(router=router, queue=RedisQueue()) as crawler:
    await crawler.run(seeds)
```

### Custom storage backend

Implement the `StorageBackend` protocol:

```python
from zencrawler.types import StorageBackend, Dataset, Store

class S3StorageBackend:
    def dataset(self, name: str) -> Dataset: ...
    def store(self, name: str) -> Store: ...
    async def close(self) -> None: ...

async with Crawler(router=router, storage=S3StorageBackend()) as crawler:
    await crawler.run(seeds)
```
