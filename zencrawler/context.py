from __future__ import annotations
import dataclasses
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    import zendriver
    from .types import Dataset, Request, Store

@dataclass
class HookContext:
    request:  "Request"
    log:      logging.Logger
    store:    "Store"

@dataclass
class AfterHookContext(HookContext):
    page:    "zendriver.Tab"
    error:   Exception | None
    elapsed: float

@dataclass
class CrawlContext:
    page:    "zendriver.Tab"
    request: "Request"
    _enqueue_fn:  Callable
    _dataset_fn:  Callable[["str"], "Dataset"]
    _store_fn:    Callable[["str"], "Store"]
    _default_dataset: "Dataset"
    _default_store:   "Store"
    log:     logging.Logger
    enqueued: list  = dataclasses.field(default_factory=list)  # for testing

    @property
    def dataset(self) -> "Dataset":
        return self._default_dataset

    @property
    def store(self) -> "Store":
        return self._default_store

    def get_dataset(self, name: str) -> "Dataset":
        return self._dataset_fn(name)

    def get_store(self, name: str) -> "Store":
        return self._store_fn(name)

    async def enqueue(
        self,
        url: "str | Request",
        *,
        label: str | None = None,
        metadata: dict | None = None,
        depth_offset: int = 1,
        inherit_metadata: bool = True,
    ) -> bool:
        from .types import Request
        if isinstance(url, str):
            # Resolve relative URLs
            resolved = urllib.parse.urljoin(self.request.url, url)
            base_meta = dict(self.request.metadata) if inherit_metadata else {}
            if metadata:
                base_meta.update(metadata)
            req = Request(
                url=resolved,
                label=label or self.request.label,
                metadata=base_meta,
                depth=self.request.depth + depth_offset,
            )
        else:
            req = url
        result = await self._enqueue_fn(req)
        self.enqueued.append(req)
        return result

    async def enqueue_all(
        self,
        urls: Iterable["str | Request"],
        **kwargs,
    ) -> int:
        count = 0
        for url in urls:
            if url and await self.enqueue(url, **kwargs):
                count += 1
        return count
