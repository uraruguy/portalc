# Security model

portalc runs unattended against a portal someone signed a contract for, using a real
account. That puts three questions in front of every feature: where may it go, what may it
say, and what may it write. The answers are enforced in code, not documented as advice.

## Where portalc may go

`policy.allowedDomains` in `portal.yaml` is the allowlist, and it is enforced on every
request the deterministic engine makes — including one that arrives inside a compiled flow
file, and including a redirect.

```yaml
policy:
  readOnly: true
  allowedDomains:
    - partner.example.com     # exactly this host
    - "*.example.com"         # any subdomain, but not example.com itself
    - 127.0.0.1:8787          # this host on this port only
  timeoutSeconds: 15
  retries: 2
  maxResponseBytes: 8388608
```

* **Empty allowlist is not "anywhere".** It means the one host in `baseUrl`.
* **A `baseUrl` outside the allowlist is a configuration error**, reported at startup
  rather than at the first request.
* **`http` and `https` only.** A flow step holding `file:///etc/passwd` is refused instead
  of being joined onto `baseUrl` as a relative path.
* **Credentials in a URL are refused.** `https://user:pass@host/` never leaves the process,
  and never reaches a log.
* **A redirect is a new request.** A portal that bounces sign-in to an identity provider on
  another domain stops until that domain is added to `allowedDomains` and committed. The
  session is not handed to a host the repository never agreed to.
* **`readOnly: true` allows exactly one POST**, the login form. A write flow needs
  `readOnly: false` — a reviewable change, not a runtime decision.

## How portalc talks

* One timeout (`policy.timeoutSeconds`), applied to every request.
* `policy.retries` with exponential backoff, and `Retry-After` honoured when the portal
  sends a sane one. Retried only on a timeout, a connection failure, `429`, or `5xx`.
* **A POST is never replayed.** A second submit can duplicate an action or trip an account
  lockout; neither is portalc's to risk.
* Responses are bounded by `policy.maxResponseBytes`.
* Each status maps to a failure a human can act on, with its own exit code: `401` is not
  the same as `403`, and neither is drift.

## What portalc may say, and write

portalc keeps the *names* of credentials in git and never the values. The values that do
pass through the process are registered once, where they enter (`login()`, and
`resolve_secrets()` when `portal.yaml` expands `${VAR}`), which makes two guarantees
checkable:

* **No credential in an error.** Every message is redacted, URLs lose their userinfo, and
  query parameters that look like tokens are masked even when the value was never
  registered.
* **No credential in an artifact.** Every flow, schema, checks file, fixture and lockfile is
  written through one function that refuses the write if a registered value is inside it.
  Evidence HTML is redacted before it is saved.
* **`${VAR}` expansion is in-memory only.** The raw `${VAR}` form is the only form on disk.

## What the lockfile guarantees

`portalc.lock` claims "this exact flow and this exact schema were green". It carries the
digests that make the claim checkable:

* per-flow `flowSha256` / `schemaSha256` (file bytes) and `flowFingerprint` /
  `schemaFingerprint` (canonical JSON, so re-indenting a file is not reported as a change);
* an `integrity` digest over the lockfile's own contents, so an edited hash is detected
  when the file is read.

A lockfile written before integrity digests existed still loads; it is reported as
unverified and gets a digest on the next green run.

## Exit codes

| Code | Meaning |
|---|---|
| 2 | usage or configuration (`portal.yaml`, flow file, lockfile shape) |
| 3 | authentication — a human is needed, not a retry |
| 4 | the portal refused us (403, rate limit, anti-bot) |
| 5 | the portal was unreachable or too slow |
| 6 | drift — the portal no longer matches the contract |
| 7 | no browser backend |
| 8 | `verify` found a failing check |
| 9 | policy violation — a request outside `allowedDomains`, scheme or `readOnly` |
| 10 | lockfile integrity — hand-edited lock, or an artifact changed since the last green run |
| 11 | refused to write a credential value to disk |

## Reporting

portalc is pre-1.0 and has no published release channel yet. Report a security issue on the
repository's issue tracker, or privately to the maintainers if it concerns a portal you
operate.
