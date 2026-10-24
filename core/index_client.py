"""Client for the a.111477.xyz directory index."""
from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urlparse

import requests

from .config import BASE_URL
from .store import Store

_ROW_RE = re.compile(
    r'<tr data-entry="true" data-name="(.*?)" data-url="(.*?)">(.*?)</tr>', re.S
)
_SIZE_RE = re.compile(r'<td class="size"[^>]*data-sort="(-?\d+)"[^>]*>(.*?)</td>', re.S)
_DIR_SPAN_RE = re.compile(r'<span class="type-label">\s*Directory\s*</span>', re.I)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class IndexError_(Exception):
    pass


class NotFound(IndexError_):
    pass


class RateLimited(IndexError_):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    path: str          # percent-encoded absolute path, always starts with "/"
    is_dir: bool
    size: int          # bytes, -1 when unknown (folders)
    kind: str

    @property
    def url(self) -> str:
        return BASE_URL + self.path

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        return cls(**data)


def normalize_path(value: str) -> str:
    """Turn a URL, an absolute path or a pasted path into a safe index path."""
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return "/"
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc and "111477" not in parsed.netloc:
            raise ValueError(f"Unsupported host: {parsed.netloc}")
        value = unquote(parsed.path)
    if not value.startswith("/"):
        value = "/" + value
    parts = [p for p in value.split("/") if p not in ("", ".")]
    encoded = [quote(unquote(p), safe="") for p in parts]
    if not encoded:
        return "/"
    return "/" + "/".join(encoded) + "/"


def parent_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    return "/" + "/".join(parts[:-1]) + ("/" if len(parts) > 1 else "")


def path_segments(path: str) -> list[tuple[str, str]]:
    """[(display_name, encoded_path), ...] for breadcrumbs."""
    out: list[tuple[str, str]] = []
    acc = ""
    for part in [p for p in path.split("/") if p]:
        acc += "/" + part
        out.append((unquote(part), acc + "/"))
    return out


def natural_key(text: str) -> list:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]


class IndexClient:
    def __init__(self, store: Store, ttl_seconds: float = 900,
                 log: Callable[[str], None] | None = None):
        self.store = store
        self.ttl = ttl_seconds
        self.log = log or (lambda msg: None)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.min_interval = 1.3
        self._cache: dict[str, tuple[list[Entry], float]] = {}

    # ------------------------------------------------------------------ fetch
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _request(self, url: str) -> requests.Response:
        last_error = ""
        for attempt in range(4):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=45)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                self.log(f"Index request failed ({exc}); retrying…")
                time.sleep(4 * (attempt + 1))
                continue

            if resp.status_code == 200:
                return resp

            if resp.status_code == 404:
                raise NotFound("That folder does not exist on the index (404).")

            body = resp.text[:4000]
            if "Error 1015" in body or resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                delay = 20 * (attempt + 1)
                if retry_after and retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                last_error = "Cloudflare rate limit (Error 1015)"
                if attempt < 3:
                    self.log(f"Index is rate-limiting this IP – waiting {delay}s…")
                    time.sleep(delay)
                    continue
                raise RateLimited(
                    "The index is rate-limiting this IP (Cloudflare 1015). "
                    "Wait a minute or two and hit Refresh."
                )

            if resp.status_code in (403, 502, 503, 520, 521, 522, 524):
                last_error = f"HTTP {resp.status_code}"
                if attempt < 3:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise IndexError_(f"Index server returned HTTP {resp.status_code}.")

            raise IndexError_(f"Unexpected HTTP {resp.status_code} for {url}")

        raise IndexError_(last_error or "Could not reach the index.")

    def _parse(self, page: str) -> list[Entry]:
        entries: list[Entry] = []
        seen: set[str] = set()
        for name, path, inner in _ROW_RE.findall(page):
            name = html.unescape(name)
            path = html.unescape(path)
            if not path.startswith("/"):
                path = "/" + path
            is_dir = path.endswith("/") or bool(_DIR_SPAN_RE.search(inner))
            if not is_dir and not path.endswith("/"):
                pass
            size = -1
            m = _SIZE_RE.search(inner)
            if m:
                try:
                    size = int(m.group(1))
                except ValueError:
                    size = -1
            if is_dir:
                kind = "Folder"
                size = -1
            else:
                dot = name.rfind(".")
                kind = name[dot + 1:].upper() if dot > 0 else "File"
            if path in seen:
                continue
            seen.add(path)
            entries.append(Entry(name=name, path=path, is_dir=is_dir, size=size, kind=kind))
        return entries

    # -------------------------------------------------------------- interface
    def list_dir(self, path: str, refresh: bool = False) -> list[Entry]:
        path = normalize_path(path)
        if not refresh and path in self._cache:
            entries, cached_at = self._cache[path]
            if time.time() - cached_at <= self.ttl:
                return entries

        if not refresh:
            cached, _ = self.store.get_listing(path, self.ttl)
            if cached is not None:
                entries = [Entry.from_dict(d) for d in cached]
                self._cache[path] = (entries, time.time())
                return entries

        url = BASE_URL + path
        try:
            page = self._request(url).text
        except IndexError_ as exc:
            stale, fetched_at = self.store.get_stale_listing(path)
            if stale is not None:
                age = int((time.time() - (fetched_at or 0)) / 60)
                self.log(f"Using cached listing for {path} ({age} min old) – {exc}")
                entries = [Entry.from_dict(d) for d in stale]
                self._cache[path] = (entries, time.time())
                return entries
            raise

        entries = self._parse(page)
        if not entries and 'data-entry="true"' in page:
            raise IndexError_("Could not parse the index page (its layout may have changed).")
        self.store.put_listing(path, [e.to_dict() for e in entries])
        self._cache[path] = (entries, time.time())
        return entries

    def root_sections(self) -> list[str]:
        try:
            entries = self.list_dir("/")
        except IndexError_:
            return []
        return [e.name for e in entries if e.is_dir]

    def search_tree(self, start: str, query: str, limit: int = 200) -> Iterable[Entry]:
        """Breadth-first filename search below a folder."""
        query = query.lower()
        queue = [normalize_path(start)]
        seen: set[str] = set()
        found = 0
        while queue and found < limit:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for entry in self.list_dir(current):
                if query in entry.name.lower():
                    found += 1
                    yield entry
                    if found >= limit:
                        return
                if entry.is_dir:
                    queue.append(entry.path)
