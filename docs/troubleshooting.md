# Troubleshooting

portalc tries to fail the way a developer tool should: what broke, where, and the next command to run. The traceback is behind `--debug` because it is rarely the useful part.

```
✗ the session is not authenticated
  http://127.0.0.1:8787/login

  try  portalc does not type your password; set the env vars named in portal.yaml
```

Start with `portalc doctor`. It prints paths, not portal data, so it is safe to paste into an issue.

## Exit codes

They are part of the interface: CI and the generated TypeScript client both branch on them, so they do not get renumbered.

| Code | Meaning | Typical cause |
|---|---|---|
| `0` | Success | |
| `1` | Generic portalc error | |
| `2` | Usage or configuration | A bad flag, a missing `portal.yaml`, an unparseable flow, an unset `${VAR}` in `baseUrl`, or `explore` without `--yes` at a terminal |
| `3` | Authentication | Credentials missing from the environment, or the portal sent us back to the login form |
| `4` | Blocked | HTTP 403 or 429: anti-bot, rate limit, or an account that lost access |
| `5` | Portal down | Unreachable, a 5xx, or the expected table was not on the page |
| `6` | **Drift** | The portal answered, but no longer matches the agreed schema |
| `7` | Browser unavailable | No Chrome or Chromium on the machine, no CDP connection, or `--agent` without `browser-use` |
| `8` | `verify` failed | At least one contract check did not pass |
| `9` | Policy violation | A request outside `policy.allowedDomains`, a non-http(s) URL, or a POST other than the login on a read-only portal |
| `10` | Lockfile integrity | `portalc.lock` was hand-edited, or an artifact changed since the last green run |
| `11` | Refused to write a secret | An artifact was about to contain a credential value |
| `70` | Unexpected | A bug. Re-run with `--debug` and please open an issue with the traceback |

Code 6 and code 5 are deliberately different: "the partner changed the invoice table" and "the partner's server is down" are not the same incident and should not page the same person.

## Installing and first run

**`ensurepip is not available` when creating a virtualenv.** Debian and Ubuntu ship `venv` separately: `sudo apt install python3-venv`, then create it again. Nothing to do with portalc.

**`portal 'demo-marina' already exists` (exit 2).** Expected, and not your mistake. The clone *is* a workspace with a compiled demo portal, so there is nothing to scaffold — go straight to `portalc demo`. `portalc init --demo --path ~/somewhere-else` makes a fresh workspace; `--force` re-scaffolds this one.

**`bundled demo portal is not on disk` (exit 2).** You installed portalc as a plain wheel. The demo is part of the repository, not the package: run it from a git clone.

**`OSError: [Errno 98] Address already in use`.** Something already holds port 8787 — often a `portalc demo` you left running. Reuse it, or start a second one on another port with `portalc demo --port 9911` **and** change `baseUrl` in `portals/demo-marina/portal.yaml` to match, because the compiled flow is pinned to that origin. This one still surfaces as a raw traceback; it is a known rough edge.

## Connecting and signing in

**`portal is not reachable` (exit 5).** For the demo, `portalc demo` is not running, or it is on a different port than `baseUrl` says. For a real portal, check the host from the same machine — portalc uses no proxy configuration of its own.

**`portal credentials are not in the environment` (exit 3).** The variables named under `auth.secrets` in `portal.yaml` are not set. Export them, or put them in `.env` (gitignored). The message names the variables and never prints a value.

**`environment variable X is not set` (exit 2).** `baseUrl` references `${X}` with no default. Either export it or give it one: `${X:-https://partners.acme.example}`.

**`sign-in did not produce a session` (exit 3).** The login POST came back to the login form. For the demo, check `DEMO_MARINA_USER` and `DEMO_MARINA_PASS` — or unset both, since portalc falls back to the published demo values. For a real portal this is usually the v0.1 limit rather than a typo: the form login posts fields named literally `username` and `password` and handles no CSRF token. See [adding-a-portal.md](adding-a-portal.md#what-v01-actually-requires).

**`portal returned HTTP 403` (exit 4).** The portal refused us rather than failing to answer. Confirm the account still has access by signing in with a browser. Do not add retries: a 403 is a decision, not a transient fault.

## Compiling a flow

**`could not find the expected table` (exit 5).** The page loaded but held no table portalc would accept. In v0.1 `explore` selects the table by a `data-portalc` attribute that only the bundled demo ships, and always looks at `/invoices`, so on any other portal this is the expected outcome, not a misconfiguration. The [limits section](adding-a-portal.md#what-v01-actually-requires) explains exactly which three things have to be true.

**`two dry runs of the same flow returned different rows` (exit 6).** portalc reads the page twice before writing a schema and refuses to compile a flow that is not reproducible. Pin the query — a month, an account, an explicit sort — and retry. If the portal is genuinely non-deterministic, a contract test cannot be built over it as it stands.

**`schema not written (re-run with --yes to compile)` (exit 2).** Not a failure. Without `--yes`, `explore` shows you the schema it inferred and writes nothing. Re-run with `--yes` when the field names and types look right. This is the interactive path only: with output redirected or in CI there is no one to review the table, so `explore` compiles without the flag.

**`no Chrome or Chromium binary on this machine` (exit 7).** `--engine chrome` needs a browser. Install Chrome or Chromium, point `PORTALC_CHROME_BINARY` at it, or drop the flag — the HTTP engine is what the demo and most of the tests use.

**`the browser is reachable but exposed no page target` (exit 7).** `--cdp-url` found a Chrome with no open tab. Open one tab in that window and re-run.

## Verifying and drifting

**`verify` exits 8 with `headers.renamed`.** A column title changed while the field and its values did not. Run `portalc repair <portal> --flow <flow>`: it re-reads the page, confirms the reading twice, patches the header in the schema, bumps the portal's patch version and leaves a pull request body in `.portalc/`. Your code keeps the same field name.

**`headers.missing` or `headers.extra`.** A field disappeared, or a new column appeared. `repair` will not touch these on purpose — deciding that a new column is "probably the old one" is how you silently reconcile against the wrong number. Read the page, then either re-run `explore --yes` to agree a new schema (a breaking change: read the diff) or fix the query.

**`schema` failed for a field.** The rows arrived but one no longer matches its type — a number that is now `"1 234,56"`, a date that is now `31/12/2026`, or a cell that is now empty in a required column. The message names the field and how many rows it affected; the fixture in `portals/<portal>/fixtures/` shows what it looked like when the schema was agreed.

**`minRows`.** The table was valid and too short — usually a filter that matched nothing, or a month with no data yet. Check the parameters before you lower the threshold in `flows/<flow>.checks.json`; an empty table that reconciles to zero is the failure this check exists to catch.

**`repair` cannot help.** It handles exactly one kind of drift: a renamed column title. Anything structural is a human decision, by design.

## Running and the generated client

**`portalc run` exits 6.** Correct behaviour: it refuses to hand back rows that do not match the contract. Do not add `--no-validate`; there is none. Fix the schema through `repair` or `explore`.

**`spawn portalc ENOENT` from the TypeScript client.** The generated package shells out to the `portalc` binary, so it must be on `PATH` in whatever runs Node — often the missing piece inside a container or a systemd unit. Otherwise pass it explicitly: `listInvoices({ bin: "/srv/app/.venv/bin/portalc" })`.

**The client throws `PortalDriftError` or `PortalAuthError`.** Those are exit codes 6 and 3 mapped into exceptions. Everything else becomes a plain `Error` carrying portalc's stderr, which is where the `✗` line and its hint are.

**`nothing to build` (exit 2).** No compiled flow for that portal yet. `portalc explore --yes` first.

## In CI

A red nightly `portalc verify --all` is the system working. The job log holds the same `✗` line and hint you would see locally.

If it fails on authentication, check that the repository secrets use the same names as `auth.secrets` in `portal.yaml`. If it fails on drift, open the run, read the renamed-header line, and run `repair` locally — the patch is meant to arrive as a reviewed pull request, not as an automatic commit.

## Still stuck

Re-run with `--debug`, then open an issue with the command, the full output including the `✗` line, and `portalc doctor`'s findings. Redact real portal data before pasting it; a schema and a header name are almost always enough to explain the problem. Anything that could expose credentials or someone else's data goes to [SECURITY.md](../SECURITY.md) instead.
