from __future__ import annotations

import fnmatch
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, TYPE_CHECKING

from .types import ErrorAction, Request

if TYPE_CHECKING:
    from .context import CrawlContext, HookContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SkipRequest(Exception):
    """Raised to dead-letter a request cleanly, without consuming a retry."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class UnhandledRequestError(Exception):
    """Raised when no handler matches and no default is registered."""

    def __init__(self, request: Request) -> None:
        super().__init__(f"No handler matched request: {request.url!r}")
        self.request = request


# ---------------------------------------------------------------------------
# Internal route descriptors
# ---------------------------------------------------------------------------

# Handler type: async def handler(ctx: CrawlContext) -> None
Handler = Callable[..., Coroutine[Any, Any, None]]

# Before-request hook: async def hook(hctx: HookContext) -> None | Request
BeforeHook = Callable[..., Coroutine[Any, Any, "Request | None"]]

# After-request hook: async def hook(hctx: HookContext) -> None
AfterHook = Callable[..., Coroutine[Any, Any, None]]

# Error hook: async def hook(ctx: CrawlContext, error: Exception) -> ErrorAction
ErrorHook = Callable[..., Coroutine[Any, Any, ErrorAction]]

# Predicate: (Request) -> bool
Predicate = Callable[[Request], bool]


@dataclass
class _LabelRoute:
    label: str
    handler: Handler


@dataclass
class _ExactRoute:
    url: str
    handler: Handler


@dataclass
class _GlobRoute:
    pattern: str
    handler: Handler


@dataclass
class _DomainRoute:
    domain_pattern: str
    handler: Handler


@dataclass
class _PredicateRoute:
    predicate: Predicate
    handler: Handler


@dataclass
class _ErrorRoute:
    error_types: tuple[type[BaseException], ...]
    hook: ErrorHook


# ---------------------------------------------------------------------------
# URL glob helpers
# ---------------------------------------------------------------------------

def _url_glob_to_fnmatch(pattern: str) -> str:
    """
    Convert ZenCrawler URL glob syntax to an fnmatch pattern.

    ZenCrawler semantics:
      **  — any characters including /
      *   — any characters except /
      ?   — exactly one character (any, including /)

    fnmatch uses:
      *   — any characters (including /)
      **  — same as * in fnmatch (no special meaning)
      ?   — exactly one character

    Strategy: replace ** with a sentinel, translate * to [^/]*, then restore
    the sentinel as * (fnmatch *).
    """
    # We'll do the translation manually to get the right semantics.
    # Build a regex pattern instead and use re for matching.
    return pattern  # placeholder — we use _url_glob_match directly


def _url_glob_match(pattern: str, url: str) -> bool:
    """
    Match a URL against a ZenCrawler URL glob pattern.

    **  matches any sequence of characters including /
    *   matches any sequence of characters that does NOT contain /
    ?   matches exactly one character (any character)
    """
    # Convert the glob pattern to a regex.
    # We iterate character-by-character to handle ** vs * distinctly.
    regex_parts: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # ** — match anything including /
                regex_parts.append(".*")
                i += 2
            else:
                # * — match anything except /
                regex_parts.append("[^/]*")
                i += 1
        elif ch == "?":
            regex_parts.append(".")
            i += 1
        else:
            regex_parts.append(re.escape(ch))
            i += 1

    regex = "^" + "".join(regex_parts) + "$"
    return bool(re.match(regex, url))


def _domain_glob_match(pattern: str, netloc: str) -> bool:
    """Match a domain glob pattern against a URL's netloc."""
    return fnmatch.fnmatch(netloc, pattern)


def _is_exact_url(pattern: str) -> bool:
    """Return True if the pattern contains no glob characters."""
    return "*" not in pattern and "?" not in pattern


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    """
    Dispatches CrawlContext instances to registered handlers based on URL
    patterns, domain globs, labels, or custom predicates.

    Match priority (first match wins):
      1. Label exact match
      2. Exact URL match
      3. URL glob (longest pattern first)
      4. Domain glob (longest pattern first)
      5. Custom predicate (registration order)
      6. Default handler
      7. UnhandledRequestError
    """

    def __init__(self) -> None:
        self._label_routes: list[_LabelRoute] = []
        self._exact_routes: list[_ExactRoute] = []
        self._glob_routes: list[_GlobRoute] = []
        self._domain_routes: list[_DomainRoute] = []
        self._predicate_routes: list[_PredicateRoute] = []
        self._default_handler: Handler | None = None

        self._before_hooks: list[BeforeHook] = []
        self._after_hooks: list[AfterHook] = []
        self._error_routes: list[_ErrorRoute] = []

    # -----------------------------------------------------------------------
    # Registration decorators
    # -----------------------------------------------------------------------

    def on(
        self,
        pattern: str | Predicate | None = None,
        *,
        domain: str | None = None,
        label: str | None = None,
    ) -> Callable[[Handler], Handler]:
        """
        Register a handler for a URL glob pattern, domain glob, label, or
        custom predicate.

        Usage:
            @router.on("https://example.com/products/**")
            @router.on(domain="example.com")
            @router.on(label="product")
            @router.on(lambda req: req.metadata.get("depth") == 0)
        """
        def decorator(handler: Handler) -> Handler:
            if label is not None:
                self._label_routes.append(_LabelRoute(label=label, handler=handler))
            elif domain is not None:
                self._domain_routes.append(
                    _DomainRoute(domain_pattern=domain, handler=handler)
                )
            elif pattern is None:
                raise ValueError(
                    "router.on() requires a positional pattern, domain=, or label="
                )
            elif callable(pattern) and not isinstance(pattern, str):
                # Custom predicate
                self._predicate_routes.append(
                    _PredicateRoute(predicate=pattern, handler=handler)
                )
            elif isinstance(pattern, str):
                if _is_exact_url(pattern):
                    self._exact_routes.append(
                        _ExactRoute(url=pattern, handler=handler)
                    )
                else:
                    self._glob_routes.append(
                        _GlobRoute(pattern=pattern, handler=handler)
                    )
            else:
                raise TypeError(
                    f"router.on() positional argument must be a str or callable, "
                    f"got {type(pattern)!r}"
                )
            return handler

        # Support @router.on(predicate_fn) where pattern is already a callable
        # passed as the first positional arg — return decorator applied immediately
        # only if pattern is callable (predicate shorthand).
        if callable(pattern) and not isinstance(pattern, str):
            return decorator  # type: ignore[return-value]

        return decorator

    @property
    def default(self) -> Callable[[Handler], Handler]:
        """
        Register the default (catch-all) handler.

        Usage:
            @router.default
            async def catch_all(ctx): ...
        """
        def decorator(handler: Handler) -> Handler:
            if self._default_handler is not None:
                log.warning(
                    "router.default: replacing existing default handler %r with %r",
                    self._default_handler,
                    handler,
                )
            self._default_handler = handler
            return handler

        return decorator

    @property
    def before_request(self) -> Callable[[BeforeHook], BeforeHook]:
        """
        Register a before-request lifecycle hook.

        The hook runs after a request is dequeued but before the browser is
        acquired. It may return None (proceed unchanged) or a new Request
        (used for this cycle only).

        Usage:
            @router.before_request
            async def log_start(hctx: HookContext) -> None: ...
        """
        def decorator(hook: BeforeHook) -> BeforeHook:
            self._before_hooks.append(hook)
            return hook

        return decorator

    @property
    def after_request(self) -> Callable[[AfterHook], AfterHook]:
        """
        Register an after-request lifecycle hook.

        Runs after the handler returns or raises, before the browser context
        is closed. Exceptions from these hooks are logged and swallowed.

        Usage:
            @router.after_request
            async def record_timing(hctx: HookContext) -> None: ...
        """
        def decorator(hook: AfterHook) -> AfterHook:
            self._after_hooks.append(hook)
            return hook

        return decorator

    def on_error(self, *error_types: type[BaseException]) -> Callable[[ErrorHook], ErrorHook]:
        """
        Register an error hook matched by exception type.

        More specific exception types should be registered first, but the
        router also sorts by MRO specificity at match time.

        Usage:
            @router.on_error(BotBlockError)
            async def handle_bot(ctx, err): return ErrorAction.DEAD_LETTER
        """
        if not error_types:
            raise ValueError("on_error() requires at least one exception type")

        def decorator(hook: ErrorHook) -> ErrorHook:
            self._error_routes.append(
                _ErrorRoute(error_types=error_types, hook=hook)
            )
            return hook

        return decorator

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

    def _find_handler(self, request: Request) -> Handler | None:
        """Return the best matching handler for *request*, or None."""
        # 1. Label match
        if request.label is not None:
            for route in self._label_routes:
                if route.label == request.label:
                    return route.handler

        url = request.url
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc

        # 2. Exact URL match
        for route in self._exact_routes:
            if route.url == url:
                return route.handler

        # 3. URL glob — longest pattern first (most specific wins)
        sorted_globs = sorted(
            self._glob_routes, key=lambda r: len(r.pattern), reverse=True
        )
        for route in sorted_globs:
            if _url_glob_match(route.pattern, url):
                return route.handler

        # 4. Domain glob — longest domain pattern first
        sorted_domains = sorted(
            self._domain_routes,
            key=lambda r: len(r.domain_pattern),
            reverse=True,
        )
        for route in sorted_domains:
            if _domain_glob_match(route.domain_pattern, netloc):
                return route.handler

        # 5. Custom predicate — registration order
        for route in self._predicate_routes:
            try:
                if route.predicate(request):
                    return route.handler
            except Exception:
                log.exception(
                    "Predicate %r raised an exception for %r; skipping",
                    route.predicate,
                    url,
                )

        # 6. Default handler
        if self._default_handler is not None:
            return self._default_handler

        return None

    async def dispatch(self, ctx: "CrawlContext") -> None:
        """
        Find the matching handler for ctx.request and call it.

        Raises UnhandledRequestError if no handler matches and no default is
        registered.
        """
        handler = self._find_handler(ctx.request)
        if handler is None:
            raise UnhandledRequestError(ctx.request)
        await handler(ctx)

    async def dispatch_error(
        self, ctx: "CrawlContext", error: Exception
    ) -> ErrorAction:
        """
        Find the most specific error hook that matches *error* and call it.

        Matching is by isinstance check; hooks registered for more specific
        types in the MRO are preferred. Among hooks of equal specificity,
        registration order is used.

        Returns the ErrorAction returned by the hook, or a sensible default
        if no hook matches or the hook itself raises.
        """
        matched_hook: ErrorHook | None = None
        best_depth: int = -1

        error_type = type(error)
        mro = error_type.__mro__

        for route in self._error_routes:
            for exc_type in route.error_types:
                if isinstance(error, exc_type):
                    # Depth in MRO — higher index means less specific.
                    try:
                        depth = mro.index(exc_type)
                    except ValueError:
                        # exc_type not in mro (e.g. registered against a sibling
                        # class but matched via multiple inheritance) — treat as
                        # lowest specificity.
                        depth = len(mro)

                    # Lower depth index = more specific.
                    # We want the most specific (lowest depth), and among ties,
                    # the first registered.
                    if matched_hook is None or depth < best_depth:
                        matched_hook = route.hook
                        best_depth = depth
                    break  # only count first matching type per route

        if matched_hook is None:
            return self._default_error_action(error)

        try:
            return await matched_hook(ctx, error)
        except Exception:
            log.exception(
                "Error hook %r raised while handling %r; applying default action",
                matched_hook,
                error,
            )
            return self._default_error_action(error)

    # -----------------------------------------------------------------------
    # Lifecycle hook runners (called by the crawler, not user code)
    # -----------------------------------------------------------------------

    async def run_before_hooks(self, hctx: "HookContext") -> "Request | None":
        """
        Run all before_request hooks in registration order.

        Returns the last non-None Request returned by any hook (the crawler
        should use it to replace the current request for this cycle), or None
        if all hooks returned None.

        Raises SkipRequest or any other exception from the first hook that
        raises — subsequent hooks do not run.
        """
        current: "Request | None" = None
        for hook in self._before_hooks:
            result = await hook(hctx)
            if result is not None:
                current = result
                # Update hctx so subsequent hooks see the modified request.
                # HookContext is a dataclass; we use object.__setattr__ to
                # handle frozen dataclasses gracefully (fall back to normal
                # attribute set for mutable ones).
                try:
                    object.__setattr__(hctx, "request", current)
                except (TypeError, AttributeError):
                    hctx.request = current  # type: ignore[misc]
        return current

    async def run_after_hooks(self, hctx: "HookContext") -> None:
        """
        Run all after_request hooks in registration order.

        Exceptions are logged at ERROR level and swallowed so that all hooks
        always run.
        """
        for hook in self._after_hooks:
            try:
                await hook(hctx)
            except Exception:
                log.error(
                    "after_request hook %r raised an exception; continuing",
                    hook,
                    exc_info=True,
                )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _default_error_action(error: Exception) -> ErrorAction:
        """
        Return the default ErrorAction for an error type based on the taxonomy
        defined in error-handling.md.

        This avoids importing the full error hierarchy here (which would create
        a circular import) by using string-based class name matching as a
        fallback — real classification happens via the error hooks registered
        by users or the crawler itself.
        """
        # Walk the MRO names to classify without importing crawler error types.
        type_names = {cls.__name__ for cls in type(error).__mro__}

        if "BotBlockError" in type_names:
            return ErrorAction.DEAD_LETTER
        if "StructureError" in type_names:
            return ErrorAction.DEAD_LETTER
        if "HandlerError" in type_names:
            return ErrorAction.DEAD_LETTER
        if "NetworkError" in type_names:
            return ErrorAction.RETRY
        if "SiteDownError" in type_names:
            return ErrorAction.RETRY
        if "BrowserCrashError" in type_names:
            return ErrorAction.RETRY
        if "SkipRequest" in type_names:
            return ErrorAction.SKIP

        # Unknown error — dead-letter to avoid silent loss.
        return ErrorAction.DEAD_LETTER
