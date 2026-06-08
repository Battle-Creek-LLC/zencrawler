"""Fake/in-memory implementations for testing ZenCrawler handlers.

Provides:
  - FakePage   — mirrors zendriver.Tab
  - FakeElement — mirrors zendriver.Element
  - MemoryDataset — in-memory Dataset protocol implementation
  - MemoryStore   — in-memory Store protocol implementation
  - build_context — construct a CrawlContext wired to fakes
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, AsyncIterator, Iterable


# ---------------------------------------------------------------------------
# Minimal HTML tree
# ---------------------------------------------------------------------------

class _Node:
    """A parsed HTML element node."""

    __slots__ = ("tag", "attrs", "children", "parent", "text_content")

    def __init__(
        self,
        tag: str,
        attrs: dict[str, str],
        parent: "_Node | None" = None,
    ) -> None:
        self.tag: str = tag
        self.attrs: dict[str, str] = attrs
        self.children: list["_Node | _TextNode"] = []
        self.parent: "_Node | None" = parent
        self.text_content: str = ""

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def _inner_text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, _TextNode):
                parts.append(child.text)
            elif isinstance(child, _Node):
                parts.append(child._inner_text())
        return "".join(parts)

    def _outer_html(self) -> str:
        attr_str = ""
        for k, v in self.attrs.items():
            attr_str += f' {k}="{v}"'
        inner = ""
        for child in self.children:
            if isinstance(child, _TextNode):
                inner += child.text
            elif isinstance(child, _Node):
                inner += child._outer_html()
        return f"<{self.tag}{attr_str}>{inner}</{self.tag}>"


class _TextNode:
    __slots__ = ("text", "parent")

    def __init__(self, text: str, parent: "_Node | None" = None) -> None:
        self.text = text
        self.parent = parent


class _RootNode:
    """Synthetic root container that holds top-level nodes."""

    __slots__ = ("children",)

    def __init__(self) -> None:
        self.children: list["_Node | _TextNode"] = []


# ---------------------------------------------------------------------------
# HTML parser that builds a node tree
# ---------------------------------------------------------------------------

# Tags that are self-closing and never have children
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._root = _RootNode()
        self._stack: list[_Node | _RootNode] = [self._root]

    @property
    def root(self) -> _RootNode:
        return self._root

    def _current(self) -> "_Node | _RootNode":
        return self._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        node = _Node(tag.lower(), attr_dict, parent=self._current() if isinstance(self._current(), _Node) else None)  # type: ignore[arg-type]
        self._current().children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        # Pop until we match the tag (handles mis-nested HTML gracefully)
        for i in range(len(self._stack) - 1, 0, -1):
            item = self._stack[i]
            if isinstance(item, _Node) and item.tag == tag:
                self._stack = self._stack[:i]
                return

    def handle_data(self, data: str) -> None:
        self._current().children.append(_TextNode(data, parent=self._current() if isinstance(self._current(), _Node) else None))  # type: ignore[arg-type]


def _parse_html(html: str) -> _RootNode:
    builder = _TreeBuilder()
    builder.feed(html)
    return builder.root


# ---------------------------------------------------------------------------
# Minimal CSS selector engine
# ---------------------------------------------------------------------------

@dataclass
class _SimpleSelector:
    tag: str | None = None
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str | None]] = field(default_factory=list)  # (name, value_or_None)


def _parse_simple_selector(sel: str) -> _SimpleSelector:
    """Parse a simple selector (no combinators) into a _SimpleSelector."""
    result = _SimpleSelector()
    # Extract id
    id_match = re.search(r"#([\w-]+)", sel)
    if id_match:
        result.id = id_match.group(1)
        sel = sel[:id_match.start()] + sel[id_match.end():]

    # Extract classes
    for m in re.finditer(r"\.([\w-]+)", sel):
        result.classes.append(m.group(1))
    sel = re.sub(r"\.([\w-]+)", "", sel)

    # Extract attribute selectors [attr] or [attr=val]
    for m in re.finditer(r"\[([^\]]+)\]", sel):
        attr_expr = m.group(1)
        if "=" in attr_expr:
            attr_name, attr_val = attr_expr.split("=", 1)
            # Strip quotes from value
            attr_val = attr_val.strip('"\'')
            result.attrs.append((attr_name.strip(), attr_val))
        else:
            result.attrs.append((attr_expr.strip(), None))
    sel = re.sub(r"\[[^\]]+\]", "", sel)

    # Remaining text is the tag (may be * or empty → any tag)
    tag = sel.strip()
    if tag and tag != "*":
        result.tag = tag.lower()

    return result


def _matches_simple(node: _Node, sel: _SimpleSelector) -> bool:
    if sel.tag is not None and node.tag != sel.tag:
        return False
    if sel.id is not None and node.attrs.get("id") != sel.id:
        return False
    if sel.classes:
        node_classes = set(node.attrs.get("class", "").split())
        if not all(c in node_classes for c in sel.classes):
            return False
    for attr_name, attr_val in sel.attrs:
        if attr_name not in node.attrs:
            return False
        if attr_val is not None and node.attrs[attr_name] != attr_val:
            return False
    return True


def _all_nodes(root: "_RootNode | _Node") -> list[_Node]:
    """Return all _Node descendants in document order."""
    result: list[_Node] = []
    stack = list(reversed(root.children))
    while stack:
        item = stack.pop()
        if isinstance(item, _Node):
            result.append(item)
            stack.extend(reversed(item.children))
    return result


def _children_nodes(node: "_Node | _RootNode") -> list[_Node]:
    return [c for c in node.children if isinstance(c, _Node)]


def _match_selector_against(
    candidates: list[_Node],
    selector_str: str,
) -> list[_Node]:
    """Match a CSS selector (supporting descendant ' ' and child '>' combinators)."""
    selector_str = selector_str.strip()

    # Split on '>' (direct child) and ' ' (descendant) while preserving which combinator
    # We tokenise: alternating simple-selector tokens and combinator tokens.
    # Strategy: split on '>' first, then handle descendant within each part.
    parts = _tokenise_selector(selector_str)
    # parts is a list of (combinator, simple_selector_str)
    # first entry has combinator None

    # Start from all nodes for the first simple part, restricted to candidates
    matched = candidates

    for i, (combinator, simple_str) in enumerate(parts):
        simple = _parse_simple_selector(simple_str)
        if i == 0:
            # Filter candidates to those matching this simple selector
            matched = [n for n in matched if _matches_simple(n, simple)]
        elif combinator == ">":
            # Direct child: expand matched to their direct-child nodes, then filter
            children: list[_Node] = []
            for parent in matched:
                children.extend(_children_nodes(parent))
            matched = [n for n in children if _matches_simple(n, simple)]
        else:
            # Descendant (combinator == " ")
            descendants: list[_Node] = []
            for ancestor in matched:
                descendants.extend(_all_nodes(ancestor))
            matched = [n for n in descendants if _matches_simple(n, simple)]

    return matched


def _tokenise_selector(selector_str: str) -> list[tuple[str | None, str]]:
    """Split a selector into (combinator, simple_selector) pairs.

    Handles ' ' (descendant) and '>' (child) combinators.
    """
    tokens: list[tuple[str | None, str]] = []

    # We split on '>' boundaries, and within those on whitespace
    # But we need to be careful: class/id/attr selectors can't contain those chars
    # so a simple split is fine.

    # Normalise multiple spaces
    s = re.sub(r"\s+", " ", selector_str.strip())

    # Split preserving > combinator
    # e.g. "div > span em" → ["div", ">", "span", "em"]
    raw_tokens = re.split(r"\s*(>)\s*|\s+", s)
    raw_tokens = [t for t in raw_tokens if t]

    combinator: str | None = None
    first = True
    for tok in raw_tokens:
        if tok == ">":
            combinator = ">"
        else:
            if first:
                tokens.append((None, tok))
                first = False
            else:
                tokens.append((combinator if combinator else " ", tok))
            combinator = None

    return tokens


def _query_all(root: "_RootNode | _Node", selector: str) -> list[_Node]:
    """Select all nodes matching *selector* under *root*."""
    all_descendants = _all_nodes(root)
    return _match_selector_against(all_descendants, selector)


def _find_by_text(root: "_RootNode | _Node", text: str) -> "_Node | None":
    """Return the deepest node whose inner text contains *text*.

    We walk the full node list and track the last match; since _all_nodes
    yields ancestors before descendants, the final match in the list will be
    the most specific (deepest) element that still contains the text.
    """
    best: "_Node | None" = None
    for node in _all_nodes(root):
        if text in node._inner_text():
            best = node
    return best


# ---------------------------------------------------------------------------
# FakeElement
# ---------------------------------------------------------------------------

class FakeElement:
    """Mirrors zendriver.Element for use in handler tests."""

    def __init__(self, node: _Node) -> None:
        self._node = node

    @property
    def text(self) -> str:
        return self._node._inner_text()

    @property
    def text_all(self) -> str:
        return self._node._inner_text()

    @property
    def attrs(self) -> dict[str, str]:
        return dict(self._node.attrs)

    def get(self, name: str) -> str | None:
        return self._node.attrs.get(name)

    def get_html(self) -> str:
        return self._node._outer_html()

    async def query_selector(self, sel: str) -> "FakeElement | None":
        results = _query_all(self._node, sel)
        return FakeElement(results[0]) if results else None

    async def query_selector_all(self, sel: str) -> "list[FakeElement]":
        results = _query_all(self._node, sel)
        return [FakeElement(n) for n in results]

    def __repr__(self) -> str:
        return f"FakeElement(<{self._node.tag}>)"


# ---------------------------------------------------------------------------
# FakePage
# ---------------------------------------------------------------------------

class FakePage:
    """Mirrors zendriver.Tab for use in handler tests.

    Parses *html* at construction time; all selector/find methods query
    the resulting tree synchronously but expose an async interface to match
    the real API.
    """

    def __init__(
        self,
        html: str,
        url: str = "https://example.com/",
        title: str = "Fake Page",
    ) -> None:
        self._html = html
        self._url = url
        self._title = title
        self._root = _parse_html(html)

    @property
    def url(self) -> str:
        return self._url

    @property
    def title(self) -> str:
        return self._title

    async def get_content(self) -> str:
        return self._html

    async def save_screenshot(self, filename: str = "auto") -> str:
        return filename

    async def select(self, selector: str) -> FakeElement:
        """Return the first element matching *selector*, raising if not found."""
        results = _query_all(self._root, selector)
        if not results:
            raise LookupError(f"No element found for selector: {selector!r}")
        return FakeElement(results[0])

    async def select_all(self, selector: str) -> list[FakeElement]:
        """Return all elements matching *selector*."""
        results = _query_all(self._root, selector)
        return [FakeElement(n) for n in results]

    async def query_selector(self, selector: str) -> "FakeElement | None":
        """Return the first element matching *selector*, or None."""
        results = _query_all(self._root, selector)
        return FakeElement(results[0]) if results else None

    async def query_selector_all(self, selector: str) -> list[FakeElement]:
        """Return all elements matching *selector*."""
        results = _query_all(self._root, selector)
        return [FakeElement(n) for n in results]

    async def find(self, text: str) -> FakeElement:
        """Return the first element whose inner text contains *text*."""
        node = _find_by_text(self._root, text)
        if node is None:
            raise LookupError(f"No element found containing text: {text!r}")
        return FakeElement(node)

    def __repr__(self) -> str:
        return f"FakePage(url={self._url!r})"


# ---------------------------------------------------------------------------
# MemoryDataset
# ---------------------------------------------------------------------------

class MemoryDataset:
    """In-memory Dataset protocol implementation for testing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: list[dict[str, Any]] = []

    async def push(self, item: dict[str, Any]) -> None:
        self._items.append(item)

    async def push_many(self, items: Iterable[dict[str, Any]]) -> None:
        self._items.extend(items)

    async def flush(self) -> None:
        pass  # nothing to flush

    async def iter(self) -> AsyncIterator[dict[str, Any]]:
        for item in list(self._items):
            yield item

    async def count(self) -> int:
        return len(self._items)

    async def clear(self) -> None:
        self._items.clear()

    async def export_json(
        self,
        path: "Path | str",
        *,
        lines: bool = False,
        indent: "int | None" = 2,
    ) -> None:
        path = Path(path)
        if lines:
            path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in self._items),
                encoding="utf-8",
            )
        else:
            path.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=indent),
                encoding="utf-8",
            )

    async def export_csv(
        self,
        path: "Path | str",
        *,
        fieldnames: "list[str] | None" = None,
        extrasaction: str = "ignore",
    ) -> None:
        path = Path(path)
        rows = list(self._items)
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        if fieldnames is None:
            fieldnames = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction=extrasaction)
            writer.writeheader()
            writer.writerows(rows)

    def __repr__(self) -> str:
        return f"MemoryDataset(name={self.name!r}, count={len(self._items)})"


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """In-memory Store protocol implementation for testing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def set(self, key: str, value: bytes) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def keys(self, prefix: str = "") -> list[str]:
        if prefix:
            return [k for k in self._data if k.startswith(prefix)]
        return list(self._data.keys())

    async def get_json(self, key: str) -> Any | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any) -> None:
        self._data[key] = json.dumps(value, ensure_ascii=False).encode()

    async def clear(self) -> None:
        self._data.clear()

    def __repr__(self) -> str:
        return f"MemoryStore(name={self.name!r}, keys={list(self._data.keys())!r})"


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------

def build_context(
    url: str,
    page: "FakePage | Any",
    metadata: "dict[str, Any] | None" = None,
    label: "str | None" = None,
    dataset: "MemoryDataset | None" = None,
    store: "MemoryStore | None" = None,
) -> "Any":  # returns CrawlContext
    """Create a CrawlContext wired to in-memory fakes.

    Args:
        url:      The URL of the request being processed.
        page:     A FakePage (or any object duck-typing zendriver.Tab).
        metadata: Optional metadata dict attached to the Request.
        label:    Optional label attached to the Request.
        dataset:  Override the default MemoryDataset (one is created if omitted).
        store:    Override the default MemoryStore (one is created if omitted).

    Returns:
        A CrawlContext whose enqueue calls record to ctx.enqueued but do not
        execute, and whose get_dataset / get_store calls return fresh
        MemoryDataset / MemoryStore instances keyed by name.
    """
    from ..context import CrawlContext
    from ..types import Request

    request = Request(
        url=url,
        label=label,
        metadata=metadata or {},
    )

    default_dataset = dataset if dataset is not None else MemoryDataset("default")
    default_store = store if store is not None else MemoryStore("default")

    _datasets: dict[str, MemoryDataset] = {"default": default_dataset}
    _stores: dict[str, MemoryStore] = {"default": default_store}

    async def _enqueue_fn(req: Request) -> bool:
        # Records are already appended by CrawlContext.enqueue; just return True.
        return True

    def _dataset_fn(name: str) -> MemoryDataset:
        if name not in _datasets:
            _datasets[name] = MemoryDataset(name)
        return _datasets[name]

    def _store_fn(name: str) -> MemoryStore:
        if name not in _stores:
            _stores[name] = MemoryStore(name)
        return _stores[name]

    ctx = CrawlContext(
        page=page,
        request=request,
        _enqueue_fn=_enqueue_fn,
        _dataset_fn=_dataset_fn,
        _store_fn=_store_fn,
        _default_dataset=default_dataset,
        _default_store=default_store,
        log=logging.getLogger("zencrawler.testing"),
    )
    return ctx
