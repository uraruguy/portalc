"""Deterministic HTTP engine for compiled flows.

This is the daily-run path: no LLM. Login, follow steps, extract a table,
validate nothing here — callers decide what a mismatch means.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any

from .errors import PortalAuthError, PortalBlockedError, PortalDownError
from .extract import ExtractedTable, extract_table


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    table: ExtractedTable | None = None


class HttpEngine:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("User-Agent", "portalc/0.1 (+https://github.com/portalc/portalc; demo-engine)"),
            ("Accept", "text/html,application/xhtml+xml"),
        ]

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.base_url + (path if path.startswith("/") else "/" + path)
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        return url

    def get(self, path: str, query: dict[str, str] | None = None) -> FetchResult:
        url = self._url(path, query)
        try:
            with self.opener.open(url, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return FetchResult(url=resp.geturl(), status=getattr(resp, "status", 200) or 200, html=html)
        except urllib.error.HTTPError as exc:
            html = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in {401, 403}:
                raise PortalBlockedError(
                    f"portal returned HTTP {exc.code}",
                    url,
                    hint="check the account still has access, or re-run explore after signing in",
                ) from exc
            if exc.code >= 500:
                raise PortalDownError(f"portal returned HTTP {exc.code}", url) from exc
            return FetchResult(url=url, status=exc.code, html=html)
        except urllib.error.URLError as exc:
            raise PortalDownError(
                "portal is not reachable",
                [self.base_url, str(exc.reason)],
                hint="for the bundled demo run `portalc demo` in another terminal",
            ) from exc

    def post_form(self, path: str, fields: dict[str, str]) -> FetchResult:
        url = self._url(path)
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return FetchResult(url=resp.geturl(), status=getattr(resp, "status", 200) or 200, html=html)
        except urllib.error.HTTPError as exc:
            html = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code == 401:
                raise PortalAuthError(
                    "sign-in was rejected",
                    url,
                    hint="check DEMO_MARINA_USER / DEMO_MARINA_PASS (or the portal's own credentials)",
                ) from exc
            if exc.code in {403}:
                raise PortalBlockedError(f"portal returned HTTP {exc.code}", url) from exc
            return FetchResult(url=url, status=exc.code, html=html)
        except urllib.error.URLError as exc:
            raise PortalDownError(
                "portal is not reachable",
                [self.base_url, str(exc.reason)],
                hint="for the bundled demo run `portalc demo` in another terminal",
            ) from exc

    def login(self, username: str, password: str, path: str = "/login") -> None:
        result = self.post_form(path, {"username": username, "password": password})
        if result.status == 401 or 'data-portalc="login-error"' in result.html or 'name="password"' in result.html and "invoices" not in result.url:
            if "/login" in result.url or result.status == 401:
                raise PortalAuthError(
                    "sign-in did not produce a session",
                    "the portal sent us back to the login form",
                    hint="export the username/password env vars from portal.yaml and retry",
                )

    def fetch_table(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        marker: str = "invoices",
    ) -> FetchResult:
        result = self.get(path, query)
        if "/login" in result.url or 'data-portalc="login-form"' in result.html:
            raise PortalAuthError(
                "the session is not authenticated",
                result.url,
                hint="portalc does not type your password; set the env vars named in portal.yaml",
            )
        try:
            result.table = extract_table(result.html, marker=marker)
        except ValueError as exc:
            raise PortalDownError("could not find the expected table", str(exc)) from exc
        return result


def dump_evidence(html: str) -> str:
    """Compact evidence: keep the table region, drop CSS."""
    start = html.find("<table")
    end = html.find("</table>")
    if start == -1 or end == -1:
        return html[:8000]
    return html[start : end + len("</table>")]


def rows_fingerprint(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
