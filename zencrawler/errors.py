from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .types import Request

class CrawlError(Exception):
    def __init__(self, message: str, *, request: "Request | None" = None, cause: Exception | None = None):
        super().__init__(message)
        self.request = request
        self.cause = cause
        if cause:
            self.__cause__ = cause

class NetworkError(CrawlError): pass

class NavigationError(NetworkError):
    def __init__(self, message, *, status_code: int | None = None, chrome_error: str | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.status_code = status_code
        self.chrome_error = chrome_error

class TimeoutError(NetworkError): pass

class BotBlockError(CrawlError):
    def __init__(self, message, *, signal: str = "", page_title: str | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.signal = signal
        self.page_title = page_title

class StructureError(CrawlError):
    def __init__(self, message, *, selector: str | None = None, context: str | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.selector = selector
        self.context = context

class SiteDownError(CrawlError): pass
class BrowserCrashError(CrawlError): pass

class HandlerError(CrawlError):
    pass

class SkipRequest(Exception):
    pass

class UnhandledRequestError(CrawlError):
    pass

class LaunchTimeoutError(CrawlError):
    pass
