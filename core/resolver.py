"""Resolve index paths into signed worker links through p.111477.xyz.

Cloudflare protects the proxy page.  We keep a browser profile so a normal
Chrome window only has to complete the check occasionally; after that the
cf_clearance cookie lets plain HTTP requests mint links (10 at a time).
"""
from __future__ import annotations

import json
import time
from typing import Callable, Iterable, Sequence
from urllib.parse import quote

import requests

from .config import BASE_URL, BROWSER_DIR, PROXY_URL, SESSION_PATH, Settings
from .store import Store

_BATCH = 10
_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class ResolverError(Exception):
    pass


class ChallengeNeeded(ResolverError):
    pass


class ClearanceError(ResolverError):
    pass


class Resolver:
    def __init__(self, store: Store, settings: Settings,
                 log: Callable[[str], None] | None = None,
                 status: Callable[[str], None] | None = None):
        self.store = store
        self.settings = settings
        self.log = log or (lambda m: None)
        self.status = status or (lambda m: None)
        self._cookies: list[dict] = []
        self._ua: str = _UA_FALLBACK
        self._saved_at: float = 0.0
        self._http: requests.Session | None = None
        self._load_session()

    # ------------------------------------------------------------- persistence
    def _load_session(self) -> None:
        try:
            data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cookies = data.get("cookies") or []
        if isinstance(cookies, list) and cookies:
            self._cookies = cookies
            self._ua = data.get("ua") or _UA_FALLBACK
            self._saved_at = float(data.get("saved_at") or 0)
            self._http = None

    def _save_session(self, cookies: list[dict], ua: str) -> None:
        self._cookies = cookies
        self._ua = ua or _UA_FALLBACK
        self._saved_at = time.time()
        self._http = None
        payload = {"cookies": cookies, "ua": self._ua, "saved_at": self._saved_at}
        try:
            SESSION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log(f"Could not save proxy session: {exc}")

    @property
    def has_session(self) -> bool:
        return any(c.get("name") == "cf_clearance" for c in self._cookies)

    @property
    def session_age_minutes(self) -> float | None:
        if not self._saved_at:
            return None
        return (time.time() - self._saved_at) / 60

    def forget_session(self) -> None:
        self._cookies = []
        self._saved_at = 0
        self._http = None
        try:
            SESSION_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    # ----------------------------------------------------------------- session
    def _session(self) -> requests.Session:
        if self._http is not None:
            return self._http
        s = requests.Session()
        s.headers.update({
            "User-Agent": self._ua,
            "Accept": "text/html,text/plain,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        for cookie in self._cookies:
            try:
                s.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain") or ".111477.xyz",
                    path=cookie.get("path") or "/",
                )
            except KeyError:
                continue
        self._http = s
        return s

    # ------------------------------------------------------------------ minting
    def _looks_like_challenge(self, resp: requests.Response) -> bool:
        if resp.status_code in (403, 503):
            return True
        text = resp.text[:600].lower()
        if "just a moment" in text or "performing security verification" in text:
            return True
        if "<!doctype html" in text and "workers.dev" not in resp.text:
            return True
        return False

    def _post_batch(self, urls: Sequence[str]) -> list[str]:
        resp = None
        delays = (10, 25, 45)
        for attempt in range(len(delays) + 1):
            resp = self._session().post(
                PROXY_URL + "/bulk",
                data={"paths": "\n".join(urls), "format": "links"},
                timeout=90,
                allow_redirects=False,
            )
            if resp.status_code != 429 or attempt == len(delays):
                break
            delay = delays[attempt]
            self.log(f"Proxy is throttling – waiting {delay}s…")
            self.status(f"Proxy busy · retrying in {delay}s…")
            time.sleep(delay)

        assert resp is not None
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            return [location] if location else []
        if self._looks_like_challenge(resp):
            raise ChallengeNeeded("Cloudflare verification expired")
        if resp.status_code == 429:
            raise ResolverError("The proxy is rate-limiting this IP (HTTP 429). Wait a moment.")
        if resp.status_code != 200:
            raise ResolverError(f"Proxy returned HTTP {resp.status_code}.")

        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        links = []
        for line in lines:
            if line.startswith("http://") or line.startswith("https://"):
                links.append(line)
            else:
                # Error text for that entry; keep the position so mapping stays aligned.
                links.append("")
        return links

    def _get_one(self, url: str) -> str:
        resp = self._session().get(
            PROXY_URL + "/bulk", params={"u": url}, timeout=90, allow_redirects=False
        )
        if resp.status_code == 429:
            self.log("Proxy is throttling a single link – waiting 10s…")
            self.status("Proxy busy · retrying in 10s…")
            time.sleep(10)
            resp = self._session().get(
                PROXY_URL + "/bulk", params={"u": url}, timeout=90, allow_redirects=False
            )
        if resp.status_code in (301, 302, 303, 307, 308):
            return resp.headers.get("location", "")
        if self._looks_like_challenge(resp):
            raise ChallengeNeeded("Cloudflare verification expired")
        if resp.status_code == 200:
            text = resp.text
            for token in text.split():
                if token.startswith("https://") and "workers.dev/d/" in token:
                    return token.strip('"\'')
        return ""

    def mint(self, paths: Iterable[str],
             progress: Callable[[int, int], None] | None = None) -> tuple[dict[str, str], list[str]]:
        """Return ({index_path: worker_url}, [errors])."""
        path_list = [p for p in paths if p]
        if not path_list:
            return {}, []
        if not self.has_session:
            self.warm_up(path_list[0])

        result: dict[str, str] = {}
        errors: list[str] = []
        total = len(path_list)
        done = 0

        for start in range(0, total, _BATCH):
            chunk = path_list[start:start + _BATCH]
            urls = [BASE_URL + p for p in chunk]
            links: list[str] = []
            try:
                links = self._post_batch(urls)
            except ChallengeNeeded:
                self.log("Proxy session expired – re-verifying in Chrome…")
                self.warm_up(chunk[0], force=True)
                try:
                    links = self._post_batch(urls)
                except ResolverError as exc:
                    self.log(f"Batch failed after re-verification: {exc}")
            except ResolverError as exc:
                self.log(f"Batch failed: {exc}")

            if len(links) != len(chunk) or any(not link for link in links):
                # A line was missing or unparsable – resolve this batch one by one
                # so links never get attached to the wrong file.
                if links:
                    self.log("Batch response was incomplete; resolving individually…")
                fixed: list[str] = []
                for url in urls:
                    link = ""
                    try:
                        link = self._get_one(url)
                    except ChallengeNeeded:
                        self.warm_up(chunk[0], force=True)
                        try:
                            link = self._get_one(url)
                        except ResolverError as exc:
                            self.log(f"Resolve failed: {exc}")
                    except ResolverError as exc:
                        self.log(f"Resolve failed: {exc}")
                    fixed.append(link)
                    time.sleep(0.25)
                links = fixed

            for idx, path in enumerate(chunk):
                link = links[idx] if idx < len(links) else ""
                if link:
                    result[path] = link
                    self.store.put_link(path, link)
                else:
                    errors.append(path)

            done += len(chunk)
            if progress:
                progress(done, total)
            time.sleep(0.35)

        return result, errors

    # -------------------------------------------------------------- cloudflare
    def _validate(self, cookies: list[dict], ua: str, probe_path: str = "") -> bool:
        probe = requests.Session()
        probe.headers.update({"User-Agent": ua, "Accept": "text/html,*/*"})
        for cookie in cookies:
            probe.cookies.set(cookie["name"], cookie["value"],
                              domain=cookie.get("domain") or ".111477.xyz",
                              path=cookie.get("path") or "/")
        try:
            resp = probe.get(PROXY_URL + "/", timeout=30)
        except requests.RequestException:
            return False
        return resp.status_code == 200 and "Mint proxy links" in resp.text

    def warm_up(self, probe_path: str, force: bool = False) -> None:
        """Open a real Chrome window so Cloudflare can be passed once."""
        if not force and self.has_session and self._saved_at and time.time() - self._saved_at < 60:
            return

        self.status("Waiting for Cloudflare verification in Chrome…")
        self.log("Opening Chrome for the Cloudflare check (you may need to click a checkbox).")

        try:
            from patchright.sync_api import sync_playwright
            engine = "patchright"
        except ImportError as exc:
            raise ClearanceError(
                "The browser component (patchright) is missing. "
                "Run: pip install -r requirements.txt"
            ) from exc

        last_error = ""
        context = None
        with sync_playwright() as pw:
            for channel in ("chrome", "msedge"):
                try:
                    context = pw.chromium.launch_persistent_context(
                        user_data_dir=str(BROWSER_DIR),
                        channel=channel,
                        headless=False,
                        no_viewport=True,
                        accept_downloads=False,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    last_error = str(exc)

            if context is None:
                raise ClearanceError(
                    "Could not start Chrome or Edge. Close any browser window that is using "
                    f"this app's profile, then try again.\n\n{last_error}"
                )

            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                try:
                    page.goto(PROXY_URL + "/", wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc).splitlines()[0]

                deadline = time.time() + 180
                reloads = 0
                started = time.time()
                while time.time() < deadline:
                    cookies = [
                        {"name": c["name"], "value": c["value"],
                         "domain": c.get("domain", ".111477.xyz"),
                         "path": c.get("path", "/")}
                        for c in context.cookies()
                        if "111477" in c.get("domain", "") and c.get("name") == "cf_clearance"
                    ]
                    if cookies:
                        try:
                            ua = page.evaluate("navigator.userAgent")
                        except Exception:
                            ua = _UA_FALLBACK
                        if self._validate(cookies, ua, probe_path):
                            self._save_session(cookies, ua)
                            self.log(f"Cloudflare verification complete ({engine}).")
                            self.status("Verification complete. Resolving links…")
                            return
                    if not cookies and time.time() - started > 45 and reloads < 2:
                        reloads += 1
                        self.log("Still waiting for the check – reloading the page…")
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=60_000)
                        except Exception:
                            pass
                        started = time.time()
                    time.sleep(1)

                raise ClearanceError(
                    "Cloudflare verification did not complete in time.\n\n"
                    "Open the Chrome window that appeared, click the verification "
                    "checkbox if one is shown, then try again."
                    + (f"\n\nLast browser error: {last_error}" if last_error else "")
                )
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    def clear_browser_profile(self) -> None:
        self.forget_session()
        for item in BROWSER_DIR.glob("*"):
            try:
                if item.is_dir():
                    import shutil
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except OSError:
                pass
