# Security policy

portalc runs unattended against a portal someone signed a contract for, using a real
account. That puts three questions in front of every feature: where may it go, what may it
say, and what may it write. The answers are enforced in code, not documented as advice.
This page is that story, plus how to tell us when it is wrong.

## Reporting a vulnerability

Use GitHub's private reporting: **[Security → Report a vulnerability](https://github.com/uraruguy/portalc/security/advisories/new)** on this repository. Please do not open a public issue for anything that could expose someone's credentials or portal data.

Include the version or commit, what you did, what happened, and how bad you think it is. A minimal reproduction against the bundled demo portal is ideal, because it proves the problem without involving anyone's real account.

We will acknowledge your report, tell you what we found, and agree a disclosure date with you before anything is published. portalc is pre-1.0 and maintained on a best-effort basis; fixes land on `main` and are noted in the release entry. If you would like credit in the advisory, say so.

## Supported versions

Only `main` and the most recent release are supported. There are no backported fixes before 1.0.

## Where portalc may go

`policy.allowedDomains` in `portal.yaml` is the allowlist, and it is enforced on every
request either engine makes — the deterministic HTTP engine and every navigation the
Chrome/CDP engine performs — including one that arrives inside a compiled flow file, and
including a redirect.

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
pass through the process are registered once, where they enter (`login()` on either
engine, and `resolve_secrets()` when `portal.yaml` expands `${VAR}`), which makes two
guarantees checkable:

* **No credential in an error.** Every message is redacted, URLs lose their userinfo, and
  query parameters that look like tokens are masked even when the value was never
  registered.
* **No credential in an artifact.** Every flow, schema, checks file, fixture and lockfile is
  written through one function that refuses the write if a registered value is inside it.
  Evidence HTML is redacted before it is saved.
* **`${VAR}` expansion is in-memory only.** The raw `${VAR}` form is the only form on disk.

## How portalc handles credentials

This is the part to read before you point portalc at a real portal.

- **Files hold names, never values.** `portal.yaml` records the *name* of the environment variable that holds each secret, under `auth.secrets`. Values are read from the process environment at call time and are not written back to disk.
- **`.env` is yours and is ignored by git.** `.gitignore` ignores `.env` and `.env.*` and tracks only `.env.example`; `portalc init` adds the entry if your workspace does not have one yet. Never move a real value into `.env.example`, and never commit a `.env`.
- **Secrets are not written into artifacts.** Schemas, flows, fixtures, evidence files and `portalc.lock` contain structure and data, not credentials. A missing variable is reported by name — `missing: ACME_PORTAL_PASS` — and the value is never echoed.
- **The generated TypeScript client never sees a secret.** It spawns `portalc run` and reads JSON from stdout, which is the reason it works that way: a package in `node_modules` cannot smuggle out a credential it was never given.
- **The agent never sees a secret either.** `--agent` is only reachable from `explore` and `repair`; the human signs in and the agent inherits the session over the same CDP connection.
- **In CI, use repository secrets** with the same variable names as `portal.yaml`, and give the job nothing else.

## Before you commit compiled artifacts

`portalc explore` writes a fixture and an evidence file from a page you were signed in to. For the bundled demo that is synthetic data. For a real portal it is **your own business data, and possibly someone else's**.

- Read the fixture and the evidence file before the first commit, and redact what does not belong in a repository.
- Evidence is deliberately trimmed to the table region and passed through the redactor, which is not the same as being safe to publish.
- A Chrome-engine `explore` also writes a screenshot next to the evidence. Look at it before committing it.
- Do not commit copies of a third-party portal's pages beyond what you need as review evidence.
- If your repository is public, assume every fixture in it is public.

## What the lockfile guarantees

`portalc.lock` claims "this exact flow and this exact schema were green". It carries the
digests that make the claim checkable:

* per-flow `flowSha256` / `schemaSha256` (file bytes) and `flowFingerprint` /
  `schemaFingerprint` (canonical JSON, so re-indenting a file is not reported as a change);
* an `integrity` digest over the lockfile's own contents, so an edited hash is detected
  when the file is read.

A lockfile written before integrity digests existed still loads; it is reported as
unverified and gets a digest on the next green run.

## Trust boundaries

Stated plainly, so nothing here surprises you:

- **A compiled flow reads; it does not write.** The only POST either engine makes is the
  login form, and `policy.readOnly` refuses any other one.
- **`policy.readOnly` and `policy.allowedDomains` are enforced, not advisory** — but they
  are a policy, not a sandbox. They constrain portalc; they do not constrain other code
  running on the same machine.
- **`portalc run` executes the compiled flow in your workspace.** Treat `portals/` as code: a flow file that arrives in a pull request can change which URL you sign in to, so review it like you would review a script. The lockfile reports a flow edited since the last green run.
- **The browser profile is a credential.** `~/.portalc/chrome-profile` holds the session a
  human signed in with. It lives outside the repository; treat it like a password file.
- **The LLM proposes, it never decides.** `run` and `verify` cannot call it. What `--agent`
  proposes is read back by the deterministic engine, has to reproduce twice, and has to
  satisfy the committed schema before anything is written.
- **The bundled demo is not a secure server.** It binds `127.0.0.1` by default, its credentials are public on purpose, and it exists to be broken by `demo/mutate.py`. Do not expose it, and do not copy its login handling into anything real.
- **`portalc doctor` prints paths, not contents.** It is safe to paste into an issue; command output that includes rows from a real portal is not.

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

## Out of scope

- The bundled demo portal's published credentials (`skipper@demo-marina.test` / `demo-portal-password`) and its deliberately weak session handling.
- Anything that requires pointing portalc at a site you do not have an account on, or at a bank or payment account. That is against the terms in [README.md](README.md#legal-and-scope), and portalc is not the right tool — use PSD2.
- Missing features described as not implemented yet. Gaps are tracked in [CONTRIBUTING.md](CONTRIBUTING.md#scope-so-you-do-not-waste-a-weekend).
