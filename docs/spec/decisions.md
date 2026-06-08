# Design Decisions

Non-goals, open questions, and the rationale log for significant choices.

---

## Non-Goals

These are explicitly excluded. Each has a reason and a note on the extension
point if a user needs it anyway.

| Excluded | Reason | Extension point |
|---|---|---|
| Fingerprint injection / user-agent spoofing | zendriver handles at CDP level; duplicating creates conflicts and false confidence | Configure via `BrowserPoolConfig.launch_args` and zendriver's own API |
| Proxy rotation | Operational infra concern, not orchestration; proxy assignment belongs outside the library | Pass proxy via `launch_args` per Chrome process; swap BrowserPool for custom implementation |
| Distributed queue (Redis, Kafka, SQS) | Adds external service dependency; single-process SQLite covers the vast majority of use cases | Implement `QueueBackend` Protocol |
| HTTP-only requests (no browser) | Different tool (httpx, aiohttp); mixing abstractions muddies the API | Use `aiohttp` directly in a handler for API calls; `ctx.page` does not need to navigate |
| JavaScript injection / raw CDP commands | Exposed via `ctx.page` (zendriver passthrough); no wrapper needed or desired | `ctx.page.evaluate(...)`, `ctx.page.cdp_cmd(...)` |
| Login / session orchestration | Sticky sessions (v2) are the primitive; multi-step login flows are handler logic | Implement in `before_request` hook + Store for token persistence |
| CAPTCHA solving | Requires third-party service with API keys and billing; out of scope | Raise `SkipRequest` in `on_error(BotBlockError)`, integrate CAPTCHA service externally |
| Link extraction helpers | Handler code; BeautifulSoup/parsel/cssselect are user's choice | Use any parser library in handler code |
| Visual regression / screenshot diffing | Testing concern; `ctx.page.save_screenshot()` is the escape hatch | Call `save_screenshot()` in `after_request` hook, diff externally |
| Schema validation on Dataset | User's responsibility; Pydantic/attrs/dataclasses all work | Validate before `ctx.dataset.push()` |
| Built-in cloud export (S3, GCS, BigQuery) | Adds cloud SDK dependencies; implement `StorageBackend` Protocol instead | Implement `StorageBackend` Protocol |
| robots.txt compliance | Policy, not mechanism; different sites have different requirements | Read and parse `robots.txt` in handler or `before_request` hook |
| Sitemap parsing | Application-level logic; trivial to implement in a handler | Fetch and parse `sitemap.xml` in seed handler |
| Headless browser alternatives (Playwright, Puppeteer) | zendriver is the explicit foundation; abstraction over multiple drivers adds complexity | Fork or wrap the library |
| Multi-process / multiprocessing crawling | asyncio single-process is the design constraint; multi-process needs IPC for queue and storage | Run multiple Crawler instances pointing at shared SQLite queue [SCOPE RISK] |
| Scheduled / cron crawling | Operational concern; combine with a cron scheduler externally | `python-crontab`, systemd timers, or cloud schedulers |

---

## Open Questions

Design decisions that have meaningful tradeoffs and require a human call before
implementation begins.

---

### 1. Queue default: `memory` or `sqlite`?

**Context:** The default affects developer experience on first run and prod reliability.

| Option | Developer experience | Production | On crash |
|---|---|---|---|
| Memory (default) | Zero setup, instant | Must opt in to SQLite | State lost |
| SQLite (default) | File created on first run | Works out of the box | Recovers |
| Memory (default) + warning if no `queue=` set | Best of both | Must be intentional | |

**Tradeoff:** SQLite as default means every dev run creates a `crawl_data.db` file.
Memory as default means production users must remember to set `queue="sqlite"` or
lose progress on crash.

**Recommendation:** Memory default, but emit a one-time `WARNING` log if
`max_requests` is not set and the crawl processes more than 1,000 requests
(suggesting a large crawl that probably wants persistence).

**✅ Resolved:** Accept recommendation — `queue="memory"` default, with the
large-crawl warning.

---

### 2. Fresh context vs browser reuse granularity

**Context:** The current design creates a fresh CDP `BrowserContext` (incognito) per
request, reusing the Chrome process.

**Question:** Does zendriver's bot evasion hold at this granularity? Some bot-detection
systems may correlate CDP sessions within a single browser process (same process ID,
rotating contexts). If so, fresh Chrome processes may be required — at ~500ms cost
per request.

**Options:**
- A: Current design — fresh context, reuse process (fast, may be detectable)
- B: Fresh process per request (slow, maximum isolation)
- C: Fresh process per N requests, configurable `process_recycle_after=50` (middle ground)
- D: Both A and C available; user configures (complexity cost)

**Call needed:** What isolation level does zendriver actually need for mainstream target sites?

---

### 3. eTLD+1 domain bucketing without external deps

**Context:** Per-domain rate limiting groups requests to `blog.example.co.uk` and
`shop.example.co.uk` into one bucket when `per="domain"`. Accurate eTLD+1 extraction
needs the Mozilla Public Suffix List (~200KB data file).

**Options:**
- A: Bundle PSL as a data file — accurate, zero runtime deps, adds 200KB to package
- B: Use heuristic (2-char second-to-last TLD part) — zero cost, wrong for some ccTLDs
- C: Default to `per="netloc"` (per-subdomain) — simple, occasionally over-permissive
- D: Auto-detect `tldextract` if installed — best accuracy when available, heuristic fallback

**Recommendation:** Option D. The library should not penalise users who don't need it.

**✅ Resolved:** Option D — auto-detect `tldextract` if installed, heuristic fallback
otherwise. Document as `pip install zencrawler[tldextract]` for accurate bucketing.

---

### 4. Handler return / error contract: enum vs sentinel exceptions

**Context:** When a handler wants to explicitly skip or retry, two styles are possible.

**Style A — ErrorAction return value (current design):**
```python
@router.on_error(BotBlockError)
async def handle(ctx, error) -> ErrorAction:
    return ErrorAction.SKIP
```

**Style B — Sentinel exceptions raised from handlers:**
```python
@router.on("https://example.com/**")
async def handler(ctx):
    if is_blocked(ctx.page):
        raise SkipRequest("bot block detected")
    if needs_retry(ctx.page):
        raise RetryRequest("not ready yet", delay=5.0)
```

Style A: error hooks and handlers are cleanly separated; explicit return type.
Style B: more Pythonic; handler logic and skip/retry decisions are co-located.

**✅ Resolved:** Both styles supported. `ErrorAction` returned from `on_error` hooks
(Style A). `SkipRequest` raised from handlers or `before_request` hooks (Style B
subset). `RetryRequest` deferred to v2 — direct retry with custom delay complicates
the queue state machine.

---

### 5. Multiple datasets in v1

**Context:** `ctx.dataset` is the default. Named datasets are accessed via
`ctx.get_dataset("name")`.

Single dataset with type tag:
```python
await ctx.dataset.push({"type": "product", "title": ..., "price": ...})
```

Named datasets:
```python
await ctx.get_dataset("products").push({"title": ..., "price": ...})
```

Named datasets simplify export and avoid heterogeneous CSV columns. They add
API surface but are trivial to implement (keyed dict of Dataset instances).

**✅ Resolved:** Named datasets included in v1. The `ctx.get_dataset(name)` API is
confirmed. Default dataset name is `"default"`.

---

## Design Decision Log

Significant choices already made, for audit purposes.

| Decision | Chosen | Rejected | Reason |
|---|---|---|---|
| Concurrency primitive | `asyncio.Semaphore` | Thread pool, `asyncio.Queue`-based | Semaphore directly maps to "slots"; simplest correct implementation |
| Rate limiting algorithm | Token bucket | Leaky bucket, sliding window | Token bucket allows burst (better UX for low-traffic starts); well-understood |
| Dedup hash | SHA256[:16] hex | Full SHA256, MD5, URL string | 128-bit prefix: negligible collision risk, fast, compact |
| Error taxonomy | Custom exception hierarchy | HTTP status codes only | Status codes don't cover browser crashes, structure errors, bot blocks |
| Storage default | SQLite (stdlib) | Redis, PostgreSQL, JSON files | Zero external deps; crash-recovery; WAL mode handles concurrency |
| Protocol typing | `typing.Protocol` | ABCs, dataclasses | Structural subtyping — no import coupling, easier to mock in tests |
| Glob matching | Custom (fnmatch-based) | Regex only, URL templates | Glob is more readable for URL patterns; users shouldn't need regex for common cases |
| Handler signature | `async def handler(ctx)` | `async def handler(page, request, ...)` | Single context object is extensible without signature changes |
| Batch flush size | 100 rows / 1 second | Per-row, 1000 rows | 100 rows balances latency and write amplification at typical crawl speeds |
| Error hook return | `ErrorAction` enum | Raise sentinel, return dict | Enum is explicit and finite; dict is stringly-typed and error-prone |
