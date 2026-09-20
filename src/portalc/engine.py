"""Deterministic HTTP engine for compiled flows.

This is the daily-run path: no LLM. Login, follow steps, extract a table,
validate nothing here — callers decide what a mismatch means.

Every request is checked against the portal's policy before it leaves the
process: allowed host, http(s) only, one timeout, a bounded response, and
retries only where a retry is safe. Credentials seen by `login()` are
registered with `policy.SECRETS`, so no failure message can quote them back.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from http.cookiejar import CookieJar
from typing import Any

from .errors import PortalAuthError, PortalBlockedError, PortalDownError
from .extract import ExtractedTable, extract_table
from .policy import SECRETS, PolicyViolation, RequestPolicy, assert_no_secrets, redact, safe_url

#: Statuses where trying the same request again is the right move.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
USER_AGENT = "portalc/0.1 (+https://github.com/portalc/portalc; deterministic-engine)"


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    table: ExtractedTable | None = None
    attempts: int = 1


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect is a new request, so it gets the same allowlist check.

    Portals that bounce sign-in to an identity provider on another domain are
    common. Following that automatically would send the session somewhere the
    repository never agreed to, so it is refused until the host is listed.
    """

    def __init__(self, policy: RequestPolicy) -> None:
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        self.policy.check(newurl, what="redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpEngine:
    def __init__(
        self,
        base_url: str,
        timeout: float | None = None,
        *,
        policy: RequestPolicy | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.policy = policy or RequestPolicy.for_base_url(self.base_url)
        if timeout is not None:
            self.policy = replace(self.policy, timeout=timeout)
        self.policy.check(self.base_url, what="baseUrl")
        self.timeout = self.policy.timeout
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            _PolicyRedirectHandler(self.policy),
        )
        self.opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "text/html,application/xhtml+xml"),
        ]

    # -- credentials ------------------------------------------------------
    def register_secrets(self, *values: str | None) -> None:
        """Remember credential values only so they can be kept out of output."""
        SECRETS.register(*values)

    # -- requests ---------------------------------------------------------
    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        if path.startswith("/") and not path.startswith("//"):
            url = self.base_url + path
        elif _SCHEME_RE.match(path) or path.startswith("//"):
            # Anything that looks absolute is treated as absolute, so a flow
            # file holding `file:///etc/passwd` is refused rather than quietly
            # joined onto baseUrl as a relative path.
            url = path
        else:
            url = self.base_url + "/" + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        return self.policy.check(url)

    def get(self, path: str, query: dict[str, str] | None = None) -> FetchResult:
        url = self._url(path, query)
        return self._open(url, method="GET", retryable=True)

    def post_form(self, path: str, fields: dict[str, str]) -> FetchResult:
        url = self._url(path)
        self._check_write_allowed(url)
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        # A POST is never replayed: a second submit can duplicate an action or
        # trip an account lockout, and neither is ours to risk.
        return self._open(req, method="POST", retryable=False, url=url)

    def _check_write_allowed(self, url: str) -> None:
        if not self.policy.read_only:
            return
        path = urllib.parse.urlsplit(url).path.rstrip("/") or "/"
        login = urllib.parse.urlsplit(self.policy.login_path).path.rstrip("/") or "/"
        if path != login:
            raise PolicyViolation(
                f"POST {path} was refused: this portal is policy.readOnly",
                [f"the only POST a read-only portal allows is the login form ({login})"],
                hint="write flows need readOnly: false in portal.yaml — a reviewable change, "
                "not a runtime decision",
                data={"path": path, "loginPath": login},
            )

    def _open(
        self,
        target: str | urllib.request.Request,
        *,
        method: str,
        retryable: bool,
        url: str | None = None,
    ) -> FetchResult:
        url = url or (target if isinstance(target, str) else target.full_url)
        max_attempts = self.policy.retries + 1 if retryable else 1
        attempt = 0
        while True:
            attempt += 1
            try:
                with self.opener.open(target, timeout=self.policy.timeout) as resp:
                    final_url = resp.geturl()
                    # Defence in depth: the redirect handler already checked this.
                    self.policy.check(final_url, what="response")
                    html = self._read(resp, final_url)
                    status = getattr(resp, "status", 200) or 200
                    return FetchResult(url=final_url, status=status, html=html, attempts=attempt)
            except urllib.error.HTTPError as exc:
                body = self._read_error_body(exc)
                if exc.code in RETRY_STATUSES and attempt < max_attempts:
                    time.sleep(self.policy.sleep_for(attempt, _retry_after(exc)))
                    continue
                return self._from_http_error(exc, url, body, method=method, attempts=attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < max_attempts:
                    time.sleep(self.policy.sleep_for(attempt))
                    continue
                raise self._unreachable(exc, attempts=attempt) from exc

    def _read(self, resp: Any, url: str) -> str:
        limit = self.policy.max_response_bytes
        if limit:
            declared = resp.headers.get("Content-Length") if resp.headers else None
            if declared and declared.isdigit() and int(declared) > limit:
                raise self._too_large(url, int(declared), limit)
            raw = resp.read(limit + 1)
            if len(raw) > limit:
                raise self._too_large(url, len(raw), limit)
        else:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")

    def _read_error_body(self, exc: urllib.error.HTTPError) -> str:
        if not exc.fp:
            return ""
        limit = self.policy.max_response_bytes or None
        try:
            raw = exc.read(limit) if limit else exc.read()
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def _too_large(self, url: str, size: int, limit: int) -> PolicyViolation:
        return PolicyViolation(
            "the portal returned more data than policy.maxResponseBytes allows",
            [safe_url(url), f"{size} bytes > limit {limit}"],
            hint="raise policy.maxResponseBytes in portal.yaml if the page is genuinely that big",
            data={"bytes": size, "limit": limit},
        )

    def _from_http_error(
        self,
        exc: urllib.error.HTTPError,
        url: str,
        body: str,
        *,
        method: str,
        attempts: int,
    ) -> FetchResult:
        """Map a status onto the failure a human can act on.

        Returning a `FetchResult` means "the caller decides"; anything portalc
        can name is raised here instead, so the message is the diagnosis.
        """
        code = exc.code
        where = safe_url(url)
        tried = {"status": code, "attempts": attempts, "method": method}
        reason = redact(str(exc.reason or ""))

        if code == 401:
            raise PortalAuthError(
                "the portal rejected the session (HTTP 401)"
                if method == "GET"
                else "sign-in was rejected (HTTP 401)",
                [where],
                hint="portalc never types a password it was not given: export the env vars named "
                "under auth.secrets in portal.yaml and retry",
                data=tried,
            ) from exc
        if code == 403:
            raise PortalBlockedError(
                "the portal refused the request (HTTP 403)",
                [where, "the account may have lost access, or the portal blocks automation"],
                hint="check the account in a browser first; portalc will not try to work around a block",
                data=tried,
            ) from exc
        if code == 404:
            raise PortalDownError(
                "the page in the flow file is gone (HTTP 404)",
                [where],
                hint="the portal moved the page: re-run explore and review the diff",
                data=tried,
            ) from exc
        if code == 429:
            retry_after = _retry_after(exc)
            raise PortalBlockedError(
                f"the portal is rate-limiting portalc (HTTP 429){_attempts(attempts)}",
                [where] + ([f"Retry-After: {retry_after:g}s"] if retry_after is not None else []),
                hint="lower the schedule in .github/workflows/portalc.yml, or raise policy.retries",
                data={**tried, "retryAfter": retry_after},
            ) from exc
        if code >= 500:
            raise PortalDownError(
                f"the portal is failing (HTTP {code}){_attempts(attempts)}",
                [where, reason],
                hint="this is the portal's side: verify again later before changing the contract",
                data=tried,
            ) from exc
        return FetchResult(url=url, status=code, html=body, attempts=attempts)

    def _unreachable(self, exc: BaseException, *, attempts: int) -> PortalDownError:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
        if timed_out:
            return PortalDownError(
                f"the portal did not answer within {self.policy.timeout:g}s{_attempts(attempts)}",
                [safe_url(self.base_url)],
                hint="raise policy.timeoutSeconds in portal.yaml if the portal is simply slow",
                data={"timeout": self.policy.timeout, "attempts": attempts},
            )
        return PortalDownError(
            f"the portal is not reachable{_attempts(attempts)}",
            [safe_url(self.base_url), redact(str(reason))],
            hint="for the bundled demo run `portalc demo` in another terminal",
            data={"attempts": attempts},
        )

    # -- flows ------------------------------------------------------------
    def login(self, username: str, password: str, path: str = "/login") -> None:
        # Registered before the request: if anything downstream fails, the
        # values are already excluded from the message.
        self.register_secrets(username, password)
        result = self.post_form(path, {"username": username, "password": password})
        rejected = (
            result.status == 401
            or 'data-portalc="login-error"' in result.html
            or ('name="password"' in result.html and "invoices" not in result.url)
        )
        if rejected and ("/login" in result.url or result.status == 401):
            raise PortalAuthError(
                "sign-in did not produce a session",
                ["the portal sent us back to the login form"],
                hint="export the username/password env vars named in portal.yaml and retry",
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
                [safe_url(result.url)],
                hint="portalc does not type your password; set the env vars named in portal.yaml",
            )
        try:
            result.table = extract_table(result.html, marker=marker)
        except ValueError as exc:
            raise PortalDownError("could not find the expected table", redact(str(exc))) from exc
        return result


def _attempts(attempts: int) -> str:
    """Say how many tries it took, but only when it took more than one."""
    return f" after {attempts} attempts" if attempts > 1 else ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = (exc.headers.get("Retry-After") if exc.headers else None) or ""
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


def dump_evidence(html: str) -> str:
    """Compact evidence: keep the table region, drop CSS — and no credentials.

    Evidence is committed, so it goes through the same redaction as errors: a
    portal that echoes the signed-in account into the page must not put it in
    the repository.
    """
    start = html.find("<table")
    end = html.find("</table>")
    body = html[:8000] if start == -1 or end == -1 else html[start : end + len("</table>")]
    body = redact(body)
    assert_no_secrets(body, where="evidence")
    return body


def rows_fingerprint(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
