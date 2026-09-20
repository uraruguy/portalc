"""What portalc is allowed to reach, and what it is allowed to say out loud.

Two guarantees live here, both fail-closed:

* **Where.** A compiled flow may only talk to a host on `policy.allowedDomains`.
  A portal that redirects to a login provider on another domain is a policy
  decision, not something the engine may take on its own.
* **What.** portalc knows the *names* of credentials, never keeps the values.
  Every value that does pass through the process is registered here, so any
  string on its way into an error message or an artifact can be checked.

Nothing in this module does I/O. The engine asks it for permission; the
workspace asks it whether a payload is safe to write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import ConfigError, PortalcError

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 4.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Values shorter than this are not worth redacting: they match too much
#: ordinary text to be told apart from it.
MIN_SECRET_LENGTH = 6
MASK = "***"

ALLOWED_SCHEMES = ("http", "https")

#: Query parameters whose value is masked in anything a human or a log sees.
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "code",
        "credential",
        "id_token",
        "key",
        "passwd",
        "password",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
)


class PolicyViolation(PortalcError):
    """portalc was asked to do something the portal's policy forbids."""

    kind = "policy"
    exit_code = 9


class SecretLeakError(PortalcError):
    """A credential value was about to be written to a file portalc manages.

    Raised instead of writing. The whole premise is that the repository holds
    the integration and never the credentials, so this is not recoverable by
    retrying — the artifact itself is wrong.
    """

    kind = "secret"
    exit_code = 11


class SecretRegistry:
    """The credential values this process has seen, so they can be kept out.

    Registration happens where a value enters portalc: `login()` on the engine
    and `resolve_secrets()` on the portal config. Nothing here is persisted.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, *values: str | None) -> None:
        for value in values:
            if value and len(value) >= MIN_SECRET_LENGTH:
                self._values.add(value)

    def clear(self) -> None:
        self._values.clear()

    def found_in(self, text: str) -> list[str]:
        """Return the registered values present in `text` (never the values themselves
        in an error message — only their length is safe to report)."""
        return [v for v in self._values if v and v in text]

    def redact(self, text: str) -> str:
        out = text
        # Longest first, so a value that contains another is masked as one unit.
        for value in sorted(self._values, key=len, reverse=True):
            out = out.replace(value, MASK)
        return out

    def __len__(self) -> int:
        return len(self._values)


#: Process-wide registry. Cross-cutting on purpose: the engine and the
#: workspace writer both need the same answer.
SECRETS = SecretRegistry()


def redact(text: str) -> str:
    return SECRETS.redact(text)


def assert_no_secrets(text: str, *, where: str) -> None:
    """Refuse to write `text` if a known credential value is inside it."""
    leaked = SECRETS.found_in(text)
    if not leaked:
        return
    raise SecretLeakError(
        "refused to write a credential value to disk",
        [
            f"{where} contains {len(leaked)} value(s) portalc knows as credentials",
            "portalc stores the environment variable *names* in git, never the values",
        ],
        hint="check the flow/schema it was built from; if the portal echoes the login back, "
        "exclude that field before compiling",
    )


# -- where portalc may go ------------------------------------------------------


@dataclass(frozen=True)
class HostPattern:
    """One `allowedDomains` entry: a host, optionally pinned to a port."""

    host: str
    port: int | None = None
    wildcard: bool = False

    @classmethod
    def parse(cls, entry: str) -> "HostPattern":
        raw = (entry or "").strip().lower()
        if not raw:
            raise ConfigError("policy.allowedDomains contains an empty entry")
        if "://" in raw:
            raw = urlsplit(raw).netloc or raw.split("://", 1)[1]
        raw = raw.strip("/")
        if "@" in raw:
            raise ConfigError(
                f"policy.allowedDomains entry {entry!r} contains credentials",
                "list the host only, e.g. partner.example.com",
            )
        host, port = _split_host_port(raw, origin=f"policy.allowedDomains entry {entry!r}")
        wildcard = host.startswith("*.") or host.startswith(".")
        if wildcard:
            host = host[1:] if host.startswith(".") else host[2:]
            if not host:
                raise ConfigError(f"policy.allowedDomains entry {entry!r} has no domain after the wildcard")
        if not host:
            raise ConfigError(f"policy.allowedDomains entry {entry!r} has no host")
        return cls(host=host, port=port, wildcard=wildcard)

    def matches(self, host: str, port: int | None) -> bool:
        if self.port is not None and self.port != port:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host

    def __str__(self) -> str:
        shown = ("*." if self.wildcard else "") + self.host
        return f"{shown}:{self.port}" if self.port is not None else shown


def _split_host_port(netloc: str, *, origin: str) -> tuple[str, int | None]:
    if netloc.startswith("["):  # IPv6 literal
        close = netloc.find("]")
        if close == -1:
            raise ConfigError(f"{origin} is not a valid host")
        host = netloc[: close + 1]
        rest = netloc[close + 1 :]
        port_part = rest[1:] if rest.startswith(":") else ""
    elif netloc.count(":") == 1:
        host, _, port_part = netloc.partition(":")
    else:
        host, port_part = netloc, ""
    if not port_part:
        return host, None
    if not port_part.isdigit():
        raise ConfigError(f"{origin} has a non-numeric port {port_part!r}")
    return host, int(port_part)


def parse_allowlist(entries: object, *, base_url: str = "") -> tuple[HostPattern, ...]:
    """Build the allowlist, defaulting to the portal's own host.

    An empty `policy.allowedDomains` is not "anywhere": it means the one origin
    the portal is configured with. An allowlist that is set but leaves the
    portal's own host out is a configuration mistake, and is reported as one.
    """
    if entries in (None, ""):
        items: list[str] = []
    elif isinstance(entries, str):
        items = [entries]
    elif isinstance(entries, (list, tuple)):
        items = [str(e) for e in entries]
    else:
        raise ConfigError(
            "policy.allowedDomains must be a list of hosts",
            f"got {type(entries).__name__}",
            hint="allowedDomains:\n  - partner.example.com",
        )

    patterns = tuple(HostPattern.parse(item) for item in items if str(item).strip())
    base_host, base_port = _url_host_port(base_url) if base_url else (None, None)

    if not patterns:
        if not base_host:
            raise ConfigError(
                "policy.allowedDomains is empty and baseUrl has no host",
                hint="set baseUrl, or list the hosts the flow may reach under policy.allowedDomains",
            )
        return (HostPattern(host=base_host),)

    if base_host and not any(p.matches(base_host, base_port) for p in patterns):
        raise ConfigError(
            "baseUrl is not covered by policy.allowedDomains",
            [
                f"baseUrl host: {base_host}",
                "allowedDomains: " + ", ".join(str(p) for p in patterns),
            ],
            hint=f"add {base_host} to policy.allowedDomains (or fix baseUrl)",
        )
    return patterns


def _url_host_port(url: str) -> tuple[str | None, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.hostname.lower() if parts.hostname else None), port


def url_is_allowed(url: str, allowlist: tuple[HostPattern, ...]) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or parts.username or parts.password:
        return False
    host, port = _url_host_port(url)
    if not host:
        return False
    return any(p.matches(host, port) for p in allowlist)


def assert_url_allowed(url: str, allowlist: tuple[HostPattern, ...], *, what: str = "request") -> str:
    """Return `url` if the policy allows it, otherwise raise `PolicyViolation`."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise PolicyViolation(
            f"{what} to a {scheme or 'scheme-less'} URL was refused",
            [safe_url(url), f"portalc only speaks {' and '.join(ALLOWED_SCHEMES)}"],
            hint="a flow file that points at anything else has been tampered with — read the diff",
        )
    if parts.username or parts.password:
        raise PolicyViolation(
            f"{what} URL carries credentials in the host part",
            [safe_url(url)],
            hint="put the credentials in environment variables named from portal.yaml, not in baseUrl",
        )
    host, port = _url_host_port(url)
    if not host:
        raise PolicyViolation(f"{what} URL has no host", [safe_url(url)])
    if not any(p.matches(host, port) for p in allowlist):
        raise PolicyViolation(
            f"{host} is not on this portal's allowedDomains",
            [
                safe_url(url),
                "allowedDomains: " + ", ".join(str(p) for p in allowlist),
            ],
            hint=f"if this host is genuinely part of the portal, add {host} to policy.allowedDomains "
            "in portal.yaml and commit that decision",
            data={"host": host, "allowedDomains": [str(p) for p in allowlist]},
        )
    return url


def safe_url(url: str) -> str:
    """A URL that is safe to print: no userinfo, no secret-looking query values."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = MASK + "@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [(k, MASK if k.lower() in SENSITIVE_QUERY_KEYS else v) for k, v in pairs],
            safe=MASK,  # keep the mask readable instead of percent-encoding it
        )
    return redact(urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)))


# -- how portalc talks --------------------------------------------------------


@dataclass(frozen=True)
class RequestPolicy:
    """The `policy:` block of portal.yaml, parsed once and enforced everywhere."""

    allowed_domains: tuple[HostPattern, ...]
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    read_only: bool = True
    login_path: str = "/login"

    @classmethod
    def for_base_url(cls, base_url: str, **overrides: object) -> "RequestPolicy":
        policy = cls(allowed_domains=parse_allowlist(None, base_url=base_url))
        return replace(policy, **overrides) if overrides else policy

    @classmethod
    def from_policy(
        cls,
        policy: dict | None,
        *,
        base_url: str,
        login_path: str = "/login",
    ) -> "RequestPolicy":
        doc = policy or {}
        return cls(
            allowed_domains=parse_allowlist(doc.get("allowedDomains"), base_url=base_url),
            timeout=_positive_float(doc.get("timeoutSeconds"), DEFAULT_TIMEOUT_SECONDS, "policy.timeoutSeconds"),
            retries=_non_negative_int(doc.get("retries"), DEFAULT_RETRIES, "policy.retries"),
            backoff=_positive_float(
                doc.get("retryBackoffSeconds"), DEFAULT_BACKOFF_SECONDS, "policy.retryBackoffSeconds"
            ),
            max_response_bytes=_non_negative_int(
                doc.get("maxResponseBytes"), DEFAULT_MAX_RESPONSE_BYTES, "policy.maxResponseBytes"
            ),
            read_only=bool(doc.get("readOnly", True)),
            login_path=login_path or "/login",
        )

    def allows(self, url: str) -> bool:
        return url_is_allowed(url, self.allowed_domains)

    def check(self, url: str, *, what: str = "request") -> str:
        return assert_url_allowed(url, self.allowed_domains, what=what)

    def sleep_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Exponential backoff, or the server's own `Retry-After` when it is sane."""
        if retry_after is not None and 0 <= retry_after <= MAX_BACKOFF_SECONDS * 4:
            return retry_after
        return min(self.backoff * (2 ** max(attempt - 1, 0)), MAX_BACKOFF_SECONDS)


def _positive_float(value: object, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number", f"got {value!r}") from None
    if out <= 0:
        raise ConfigError(f"{name} must be greater than 0", f"got {out!r}")
    return out


def _non_negative_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        out = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number", f"got {value!r}") from None
    if out < 0:
        raise ConfigError(f"{name} must not be negative", f"got {out!r}")
    return out
