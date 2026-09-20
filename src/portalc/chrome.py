"""Real Chrome over CDP — the browser engine for portals the HTTP engine cannot see.

Two jobs, and only two:

* `portalc explore --engine chrome` drives a real browser so a **human** signs in
  (2FA, CAPTCHA and the ToS question all stay on the human side of the line) and
  portalc reads the *rendered* DOM afterwards.
* `portalc run|verify --engine chrome` replays the compiled steps in that browser:
  navigate, wait for the document, read, extract by rule. Still deterministic —
  no model, no judgement, no tokens.

The LLM does not live here. It lives in `agent.py` and only `explore` and `repair`
are allowed to call it.

Talking to Chrome is a websocket and five CDP methods, so portalc talks to it
directly instead of taking a browser framework as a dependency. `CdpChannel` is
the seam: tests drive the engine through a fake channel, so the logic is covered
on machines with no display and no Chrome.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .engine import FetchResult
from .errors import (
    BrowserUnavailableError,
    PortalAuthError,
    PortalBlockedError,
    PortalDownError,
)
from .extract import extract_table

CHROME_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)

#: Persisted Chrome profile. It holds the session a human signed in with, so
#: `explore` does not ask for 2FA twice. Never inside the repository.
DEFAULT_PROFILE_DIR = Path.home() / ".portalc" / "chrome-profile"

#: Generic login selectors. A portal can override them under `auth.selectors`
#: in portal.yaml when its form is not conventional.
DEFAULT_LOGIN_SELECTORS = {
    "username": 'input[name="username"], input[name="email"], input[type="email"], input[autocomplete="username"]',
    "password": 'input[type="password"]',
    "submit": 'button[type="submit"], input[type="submit"]',
}

LOGIN_WALL_SELECTOR = '[data-portalc="login-form"], form input[type="password"]'


class CdpChannel(Protocol):
    """One open DevTools connection to one page."""

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def take_events(self, method: str) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class WebSocketChannel:
    """Request/response over the DevTools websocket, events buffered for later."""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        try:
            import websocket  # provided by websocket-client
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise BrowserUnavailableError(
                "websocket-client is not installed",
                "the Chrome engine talks CDP over a websocket",
                hint="pip install -e .   (websocket-client is a runtime dependency of portalc)",
            ) from exc
        self.ws_url = ws_url
        self.timeout = timeout
        self._events: list[dict[str, Any]] = []
        self._id = 0
        try:
            self._ws = websocket.create_connection(
                ws_url, timeout=timeout, max_size=None, suppress_origin=True
            )
        except Exception as exc:  # noqa: BLE001 - websocket raises a wide family
            raise BrowserUnavailableError(
                "could not open the CDP websocket",
                [ws_url, str(exc)],
                hint="start Chrome with --remote-debugging-port=9222 and pass --cdp-url http://127.0.0.1:9222",
            ) from exc

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        message_id = self._id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except Exception as exc:  # noqa: BLE001
                raise PortalDownError(
                    "the CDP connection dropped",
                    [method, str(exc)],
                    hint="the browser was probably closed mid-run; re-run the command",
                ) from exc
            if not raw:
                continue
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise PortalDownError(
                        f"Chrome refused {method}",
                        json.dumps(message["error"])[:400],
                    )
                return message.get("result") or {}
            if "method" in message:
                self._events.append(message)
        raise PortalDownError(
            f"Chrome did not answer {method} within {self.timeout:.0f}s",
            hint="raise the timeout with PORTALC_CHROME_TIMEOUT, or check the page is not blocked on a dialog",
        )

    def take_events(self, method: str) -> list[dict[str, Any]]:
        keep, taken = [], []
        for message in self._events:
            (taken if message.get("method") == method else keep).append(message)
        self._events = keep
        return taken

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


@dataclass
class ChromeOptions:
    """How to get a browser. Attaching to yours is the interesting case."""

    cdp_url: str | None = None
    headless: bool = False
    profile_dir: Path | None = None
    binary: str | None = None
    timeout: float = 20.0
    keep_open: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "ChromeOptions":
        env = os.environ
        opts = cls(
            cdp_url=env.get("PORTALC_CDP_URL") or None,
            headless=env.get("PORTALC_CHROME_HEADLESS") == "1",
            profile_dir=Path(env["PORTALC_CHROME_PROFILE"]) if env.get("PORTALC_CHROME_PROFILE") else None,
            binary=env.get("PORTALC_CHROME_BINARY") or None,
            timeout=float(env.get("PORTALC_CHROME_TIMEOUT") or 20.0),
        )
        for key, value in overrides.items():
            if value is None:
                continue
            if not hasattr(opts, key):
                raise TypeError(f"unknown Chrome option {key!r}")
            setattr(opts, key, Path(value) if key == "profile_dir" else value)
        return opts


def find_chrome(explicit: str | None = None) -> str:
    for candidate in [c for c in (explicit, os.environ.get("PORTALC_CHROME_BINARY"), *CHROME_BINARIES) if c]:
        found = shutil.which(candidate)
        if found:
            return found
        if Path(candidate).is_file():
            return candidate
    raise BrowserUnavailableError(
        "no Chrome or Chromium binary on this machine",
        f"looked for: {', '.join(CHROME_BINARIES)}",
        hint="install Google Chrome, or drop --engine chrome (the HTTP engine needs no browser)",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _devtools_json(url: str, timeout: float = 5.0, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def page_ws_url(devtools_url: str, *, prefer_host: str | None = None, timeout: float = 20.0) -> str:
    """Find a page target on a running browser, asking for a tab if there is none."""
    base = devtools_url.rstrip("/")
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            targets = _devtools_json(f"{base}/json/list")
        except OSError as exc:
            last_error = exc
            time.sleep(0.15)
            continue
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if prefer_host:
            for target in pages:
                if prefer_host in (target.get("url") or ""):
                    return target["webSocketDebuggerUrl"]
        if pages:
            return pages[0]["webSocketDebuggerUrl"]
        with contextlib.suppress(OSError):
            _devtools_json(f"{base}/json/new?about:blank", method="PUT")
        time.sleep(0.15)
    raise BrowserUnavailableError(
        "the browser is reachable but exposed no page target",
        [base, str(last_error or "")],
        hint="open one tab in that Chrome window, then re-run",
    )


class ChromeEngine:
    """The same surface `HttpEngine` offers, backed by a real rendering engine."""

    name = "chrome"

    def __init__(
        self,
        base_url: str,
        channel: CdpChannel,
        *,
        timeout: float = 20.0,
        process: subprocess.Popen | None = None,
        devtools_url: str | None = None,
        keep_open: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.cdp = channel
        self.timeout = timeout
        self.devtools_url = devtools_url
        self.keep_open = keep_open
        self._process = process
        self._seconds = 0.0
        self.cdp.send("Page.enable")
        self.cdp.send("Network.enable")

    # -- lifecycle -------------------------------------------------------
    @classmethod
    def start(cls, base_url: str, *, options: ChromeOptions | None = None, **overrides: Any) -> "ChromeEngine":
        opts = options or ChromeOptions.from_env(**overrides)
        host = urllib.parse.urlsplit(base_url).hostname

        if opts.cdp_url:
            ws_url = (
                opts.cdp_url
                if opts.cdp_url.startswith("ws")
                else page_ws_url(opts.cdp_url, prefer_host=host, timeout=opts.timeout)
            )
            return cls(
                base_url,
                WebSocketChannel(ws_url, opts.timeout),
                timeout=opts.timeout,
                devtools_url=opts.cdp_url,
                keep_open=True,
            )

        binary = find_chrome(opts.binary)
        profile = Path(opts.profile_dir or DEFAULT_PROFILE_DIR)
        profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--window-size=1366,900",
            "about:blank",
        ]
        if opts.headless:
            args.insert(1, "--headless=new")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        try:
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BrowserUnavailableError("could not start Chrome", [binary, str(exc)]) from exc
        devtools_url = f"http://127.0.0.1:{port}"
        try:
            ws_url = page_ws_url(devtools_url, timeout=max(opts.timeout, 20.0))
        except BrowserUnavailableError as exc:
            _stop(process)
            raise BrowserUnavailableError(
                "Chrome started but never exposed a debugging port",
                [*exc.detail, f"profile: {profile}"],
                hint=f"another Chrome may already own {profile}; close it or pass --chrome-profile",
            ) from exc
        return cls(
            base_url,
            WebSocketChannel(ws_url, opts.timeout),
            timeout=opts.timeout,
            process=process,
            devtools_url=devtools_url,
            keep_open=opts.keep_open,
        )

    def close(self) -> None:
        self.cdp.close()
        if self._process is not None and not self.keep_open:
            _stop(self._process)
            self._process = None

    def __enter__(self) -> "ChromeEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def browser_seconds(self) -> float:
        return self._seconds

    # -- primitives ------------------------------------------------------
    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.base_url + (path if path.startswith("/") else "/" + path)
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        return url

    def evaluate(self, expression: str) -> Any:
        result = self.cdp.send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        if result.get("exceptionDetails"):
            raise PortalDownError(
                "JavaScript in the page raised",
                json.dumps(result["exceptionDetails"])[:400],
            )
        return (result.get("result") or {}).get("value")

    def current_url(self) -> str:
        return self.evaluate("location.href") or ""

    def html(self) -> str:
        return self.evaluate("document.documentElement.outerHTML") or ""

    def exists(self, selector: str) -> bool:
        return bool(self.evaluate(f"!!document.querySelector({json.dumps(selector)})"))

    def screenshot_png(self) -> bytes:
        result = self.cdp.send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result.get("data") or "")

    def _wait_ready(self, deadline: float) -> None:
        while time.time() < deadline:
            if self.evaluate("document.readyState") == "complete":
                return
            time.sleep(0.1)
        raise PortalDownError(
            "the page did not finish loading",
            self.current_url(),
            hint="raise PORTALC_CHROME_TIMEOUT, or the portal is genuinely slow",
        )

    def _document_status(self) -> int | None:
        status: int | None = None
        for message in self.cdp.take_events("Network.responseReceived"):
            params = message.get("params") or {}
            if params.get("type") != "Document":
                continue
            code = int((params.get("response") or {}).get("status") or 0)
            if code:
                status = code
        return status

    # -- the HttpEngine-shaped surface -----------------------------------
    def get(self, path: str, query: dict[str, str] | None = None) -> FetchResult:
        url = self._url(path, query)
        started = time.time()
        self.cdp.take_events("Network.responseReceived")
        result = self.cdp.send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise PortalDownError(
                "Chrome could not load the page",
                [url, str(result["errorText"])],
                hint="check the portal is reachable from this machine",
            )
        self._wait_ready(time.time() + self.timeout)
        status = self._document_status() or 200
        self._seconds += time.time() - started
        if status == 403:
            raise PortalBlockedError(
                f"portal returned HTTP {status}",
                url,
                hint="the portal refused a browser it does not recognise; sign in by hand in this profile first",
            )
        if status >= 500:
            raise PortalDownError(f"portal returned HTTP {status}", url)
        return FetchResult(url=self.current_url() or url, status=status, html=self.html())

    def login(
        self,
        username: str,
        password: str,
        path: str = "/login",
        *,
        selectors: dict[str, str] | None = None,
    ) -> None:
        """Fill a conventional login form in the real browser.

        The value is injected at the input layer (`Input.insertText`), never
        interpolated into JavaScript source, so it does not land in a page
        script, a console history or a CDP script cache.

        A real portal should prefer `await_human_login`: portalc has no reason
        to own a password it cannot solve 2FA for. This exists for portals like
        the bundled demo, whose credentials are already public.
        """
        self.get(path)
        selector = dict(DEFAULT_LOGIN_SELECTORS)
        selector.update(selectors or {})
        before = self.current_url()
        self._type_into(selector["username"], username, field="username")
        self._type_into(selector["password"], password, field="password")
        self.cdp.take_events("Network.responseReceived")
        self._submit(selector["submit"])
        status = self._await_navigation(before)
        if status == 403:
            raise PortalBlockedError(
                "the portal refused the sign-in with HTTP 403",
                self.current_url(),
                hint="anti-bot, not a wrong password: sign in by hand in this Chrome profile once",
            )
        if self.exists(LOGIN_WALL_SELECTOR):
            raise PortalAuthError(
                "sign-in did not produce a session",
                "the portal is still showing its login form",
                hint="check the env vars named under auth.secrets in portal.yaml; portalc stores names, never values",
            )

    def await_human_login(
        self,
        *,
        login_path: str = "/",
        timeout: float = 600.0,
        wait: Callable[[], None] | None = None,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        """Hand the browser to the human, then wait for an authenticated session.

        This is the default for real portals. There is deliberately no automated
        path past authentication: the human types the password and answers 2FA,
        and portalc inherits the session from the profile.
        """
        say = announce or _default_announce
        self.get(login_path)
        say(f"sign in in the Chrome window portalc opened ({self.current_url()})")
        if wait is not None:
            wait()
        elif sys.stdin.isatty():
            say("press Enter once you are signed in")
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input()
        else:
            deadline = time.time() + timeout
            say(f"waiting up to {timeout / 60:.0f} min for the login wall to disappear")
            while time.time() < deadline:
                if not self.exists(LOGIN_WALL_SELECTOR):
                    break
                time.sleep(1.0)
        if self.exists(LOGIN_WALL_SELECTOR):
            raise PortalAuthError(
                "nobody signed in",
                self.current_url(),
                hint="re-run and sign in in the browser window; portalc never types a password for you",
            )

    def fetch_table(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        marker: str = "invoices",
    ) -> FetchResult:
        result = self.get(path, query)
        if "/login" in result.url or self.exists(LOGIN_WALL_SELECTOR):
            raise PortalAuthError(
                "the session is not authenticated",
                result.url,
                hint="run `portalc explore --engine chrome` and sign in; the profile keeps the session for later runs",
            )
        try:
            result.table = extract_table(result.html, marker=marker)
        except ValueError as exc:
            raise PortalDownError(
                "could not find the expected table on the rendered page",
                [result.url, str(exc)],
                hint="the table may have moved: `portalc repair --engine chrome --agent` can look for it",
            ) from exc
        return result

    # -- internals -------------------------------------------------------
    def _type_into(self, selector: str, value: str, *, field: str) -> None:
        focused = self.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return false; el.focus(); el.select && el.select();"
            " el.value = ''; return true; })()" % json.dumps(selector)
        )
        if not focused:
            raise PortalAuthError(
                f"could not find the {field} field on the login page",
                selector,
                hint="set auth.selectors.%s in portal.yaml to a CSS selector that matches" % field,
            )
        self.cdp.send("Input.insertText", {"text": value})

    def _submit(self, selector: str) -> None:
        submitted = self.evaluate(
            "(() => { const b = document.querySelector(%s);"
            " if (b) { b.click(); return true; }"
            " const f = document.querySelector('form');"
            " if (!f) return false;"
            " f.requestSubmit ? f.requestSubmit() : f.submit(); return true; })()" % json.dumps(selector)
        )
        if not submitted:
            raise PortalAuthError(
                "found no way to submit the login form",
                selector,
                hint="set auth.selectors.submit in portal.yaml",
            )

    def _await_navigation(self, previous_url: str) -> int | None:
        """Wait for the portal's answer and return its HTTP status if it gave one.

        A rejected sign-in usually re-renders the same URL, so "the URL changed"
        is not enough: a new document response is what proves the portal replied.
        """
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(0.15)
            if self.evaluate("document.readyState") != "complete":
                continue
            status = self._document_status()
            if status is not None:
                return status
            if self.current_url() != previous_url or not self.exists(LOGIN_WALL_SELECTOR):
                return None
        raise PortalDownError(
            "the portal did not respond to the sign-in",
            previous_url,
            hint="raise PORTALC_CHROME_TIMEOUT if the portal is slow",
        )


def _default_announce(message: str) -> None:
    from .ui import step

    step(message)


def _stop(process: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()


def chrome_status() -> dict[str, Any]:
    """What `portalc doctor` needs to know about this machine."""
    try:
        binary = find_chrome()
    except BrowserUnavailableError:
        return {"ok": False, "binary": None, "version": None}
    version = None
    with contextlib.suppress(Exception):
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    return {"ok": True, "binary": binary, "version": version or None}
