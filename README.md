# portalc

[![portalc verify](https://github.com/uraruguy/portalc/actions/workflows/portalc.yml/badge.svg)](https://github.com/uraruguy/portalc/actions/workflows/portalc.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Compile a login-walled portal into a **versioned, typed dependency**: JSON Schema, a TypeScript client, a contract test, and a patch when the portal changes.

> **Status: unfinished side project.** v0.1 works end to end on the bundled demo portal, and its test suite runs nightly in CI. It has not been taken to a real portal yet: that still needs code changes, listed under [Known gaps](#known-gaps). It is not under active development.

## The idea

Some partner portals have no API: you sign in and read a table. portalc is an experiment in treating such a portal like a dependency. You compile it once, and what comes out is plain text in your own repo: a JSON Schema, the compiled flow, a fixture, a typed client, a version and a CHANGELOG. A nightly contract test then tells you when the portal changed (a renamed column still returns HTTP 200) before your code quietly reads the wrong thing.

The daily run is deterministic HTTP or CDP with no model involved. An LLM agent is optional and limited to two commands, `explore --agent` and `repair --agent`. `portal.yaml` holds environment variable *names*, never secret values.

Similar ideas already exist, for example [Libretto](https://github.com/cashew-labs/libretto) (agent-written deterministic scripts for login-walled portals), [Integuru](https://github.com/Integuru-AI/Integuru) (integration code generated from recorded network traffic) and [Spidermon](https://spidermon.readthedocs.io/en/latest/item-validation.html) (schema checks for scraped data). portalc tries one particular packaging of them: the integration as versioned files in git, plus a nightly contract test.

Use a real API when one exists. portalc is aimed at partner portals where you already have an account and the ToS allows it. Not banks (PSD2). Not bulk scraping.

## Requirements

- Python 3.10 or newer (3.12 is what CI runs). The optional `[agent]` extra needs 3.11 or newer.
- Nothing else for the bundled demo. Chrome, the agent extra and Node are optional; `portalc doctor` tells you what it found and what it does not need.

## Install

```bash
git clone https://github.com/uraruguy/portalc.git
cd portalc
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
portalc doctor
```

On Debian and Ubuntu, `python3 -m venv` needs the `python3-venv` package (`sudo apt install python3-venv`). Any other virtualenv tool works; portalc has no opinion.

## Five-minute quickstart

The clone already contains a compiled portal — `portals/demo-marina/` — so there is nothing to scaffold. The demo is a fake marina partner portal (login wall plus an invoice table) served from your own machine. Nothing points at a real business, and the demo credentials are built in, so you do not need a `.env` to get through this.

```bash
portalc demo                         # terminal 1: serves http://127.0.0.1:8787, leave it running
```

```bash
portalc explore --yes                # terminal 2: sign in, extract, dry-run twice, write schema + flow + fixture
portalc verify                       # contract test: headers, JSON Schema, min rows. No model.
portalc build                        # generate generated/ts/portal-demo-marina/
python demo/mutate.py rename-total   # the portal renames a column and still returns 200
portalc verify                       # fails, exit 8: total_eur: 'Total (EUR)' → 'Amount (EUR)'
portalc repair                       # re-reads the page, writes a rename patch, 2/2 confirmations
portalc verify                       # green again
python demo/mutate.py reset          # put the demo portal back
```

`portalc verify` is the step that matters. It fails on a column rename that no HTTP status code would ever tell you about, and it decides that without asking a model.

Full walkthrough with expected output: [docs/quickstart.md](docs/quickstart.md).

## What ends up in your repo

```
portals/<name>/portal.yaml              # base URL, auth mode, secret *names*, policy
portals/<name>/flows/<flow>.flow.json   # the compiled steps and the header contract
portals/<name>/flows/<flow>.checks.json # minRows and friends
portals/<name>/schemas/<schema>.schema.json
portals/<name>/fixtures/<date>.<flow>.json
portals/<name>/evidence/<date>.<flow>.html
portals/<name>/CHANGELOG.md
generated/ts/portal-<name>/             # typed client, generated — do not hand-edit
portalc.lock                            # per-portal version, artifact hashes, last green run
```

All of it is text, all of it is reviewable, and a portal change shows up as a diff in a pull request instead of as a support ticket.

## Commands

| Command | What it does |
|---|---|
| `portalc doctor` | What this machine can run: the Chrome it found, and what it does not need |
| `portalc init --demo` | Scaffold a new workspace elsewhere (`--path`): `portal.yaml`, GitHub Action, `.env.example`, `.gitignore` entry for `.env`. The clone is already set up |
| `portalc demo` | Serve the bundled marina at `http://127.0.0.1:8787` |
| `portalc explore --yes` | Sign in, extract, dry-run twice, write schema, flow and fixture |
| `portalc verify` | Contract test: headers, JSON Schema, min rows. **No model** |
| `portalc repair` | Re-read the page and patch a column *title* rename (demo only for now, see [Known gaps](#known-gaps)) |
| `portalc repair --agent` | Experimental: ask an LLM agent where the data moved, then prove it deterministically |
| `portalc run <portal> <flow> --json` | Typed rows, or a drift error (exit 6) |
| `portalc build` | Generate the TypeScript client |
| `portalc version` | Print the portalc version |

Add `--debug` for tracebacks, `--json` for machine-readable output where it applies, `--quiet` to drop the decoration. Every command explains its exit code in [docs/troubleshooting.md](docs/troubleshooting.md).

## Using the generated client

```ts
import { listInvoices } from "@portalc/portal-demo-marina";

const rows = await listInvoices({ month: "2026-09" });
```

The package shells out to `portalc run`, so credentials stay in your environment and never in `node_modules`. A drift throws `PortalDriftError` rather than returning half-right rows. `portalc` must be on `PATH` (or pass `bin`).

## Engines

The engine is a plug-in. The artifacts do not depend on it: a flow compiled in Chrome is validated by the HTTP engine and the other way round. Both engines apply the portal's `policy:` block (allowlist, read-only, timeouts). The HTTP engine checks every request it makes; the Chrome engine checks the pages it navigates to and where they end up, but not the requests a page makes on its own.

- **http** (default): form login + HTML table over `urllib`. No browser, no LLM, milliseconds per call. Enough for any portal that is a session cookie plus a table — including the bundled demo. Compiling a portal of your own is not purely a configuration change yet: [docs/adding-a-portal.md](docs/adding-a-portal.md#what-v01-actually-requires) states what `explore` still hard-codes, so you can judge in ten minutes rather than an afternoon.
- **chrome**: a real Chrome driven over CDP (`Page.navigate`, `Runtime.evaluate`, `Input.insertText`, `Page.captureScreenshot`). For portals that render their table in JavaScript, or that only let a human through the login. Still deterministic in `run` and `verify`: navigate, wait for the document, extract by rule. It waits for `document.readyState`, not for the table itself, so a table that renders late can be missed.

```bash
portalc explore --engine chrome --yes          # Chrome portalc starts, profile kept for later runs
portalc explore --engine chrome --cdp-url http://127.0.0.1:9222   # attach to a Chrome you already use
portalc verify  --engine chrome --headless     # nightly, session from the profile
```

### What `portalc explore --engine chrome` needs

| Requirement | Why | If it is missing |
|---|---|---|
| Chrome or Chromium on `PATH` | the engine speaks CDP to it | `portalc doctor` says so; use `--engine http`. On macOS, Chrome in `/Applications` is not on `PATH`: set `PORTALC_CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"` |
| a desktop session, **or** `--headless` | a human cannot sign in to a headless window | `--headless` works for portals whose `auth.mode` is `form` |
| `~/.portalc/chrome-profile` (or `--chrome-profile`) | keeps the cookies the portal marked persistent, so a remember-me session survives between runs | a portal that only sets a session cookie needs your own Chrome kept open and `--cdp-url` |
| nothing else | no Playwright, no browser framework, no API key | — |

Attaching is the interesting mode: start your own Chrome with `--remote-debugging-port=9222`, sign in as yourself, then point portalc at it. portalc never types your password and there is no automated path past 2FA — for `auth.mode: human-session` it opens the page, waits for you, and inherits the session. A stale session is reported as `PortalAuthError` (exit 3), never as a failed schema check: a human is needed, not a retry.

Environment equivalents, for CI: `PORTALC_CDP_URL`, `PORTALC_CHROME_HEADLESS=1`, `PORTALC_CHROME_PROFILE`, `PORTALC_CHROME_BINARY`, `PORTALC_CHROME_TIMEOUT`.

## Where the LLM is allowed

In two commands, and nowhere else:

```bash
pip install -e ".[agent]"                      # the optional agent extra (Python 3.11+)
portalc explore --engine chrome --agent --yes   # agent proposes where the data lives
portalc repair  --engine chrome --agent         # agent proposes where it moved to
```

The agent **proposes a location**; it never extracts the rows and never decides that a test passed. Whatever it proposes is then read by the deterministic engine, has to come back identical twice, has to satisfy the committed JSON Schema, and has to survive two green `verify` runs before a patch and a PR body are written. `run` and `verify` cannot call it at all — a model that grades its own homework is not a contract test.

With `--engine chrome` it never receives a secret either: the human signs in and the agent inherits the session over the same CDP connection.

> **Experimental.** This path has only been tested with a stub, never against a live model. Known issues: the model cannot be chosen from the CLI, the cost read-out is always empty, and without `--engine chrome` the agent starts a fresh session that is not signed in.

## Continuous verification

`.github/workflows/portalc.yml` runs `portalc verify --all` nightly at 03:00 UTC and on every push that touches a portal. That schedule is the point of the idea — you find out that a partner changed a column at 03:00, from CI, not from a customer. Put the portal's credentials in repository secrets under the env var names from `portal.yaml`.

## Known gaps

Where v0.1 stops, so you can judge it quickly:

- **A real portal needs code, not configuration.** Login posts fields named `username` and `password`, `explore` always reads `/invoices`, and the table is found by a `data-portalc="invoices"` marker that only the demo ships. [docs/adding-a-portal.md](docs/adding-a-portal.md#what-v01-actually-requires) shows where to change that.
- **`repair` handles a column title rename, and only on the demo.** It recognises a rename because the demo marks each column with `data-col`. On other pages the field name comes from the header text, so a renamed column looks like one field removed and another added, and `repair` refuses.
- **Types come from one sample.** Column types are inferred from the rows seen at `explore`: an invoice number like `412` becomes a number, and a European amount like `1.234,50` stays a string.
- **`verify --all` stops at the first portal that raises an error** (a stale login, for example) instead of reporting the rest.
- **`build` generates a client for the first flow of a portal only.**
- **The lockfile records artifact hashes, but `verify` does not check them yet.**
- **The credential guard is a plain text search** over the JSON artifacts portalc writes. Evidence HTML is redacted separately; screenshots, the CHANGELOG and the generated client are not scanned.
- **The Chrome engine** waits for `document.readyState` rather than the table, and on macOS needs `PORTALC_CHROME_BINARY` (see [Engines](#engines)).

## Documentation

- [docs/quickstart.md](docs/quickstart.md) — the demo run, with the output you should see
- [docs/concepts.md](docs/concepts.md) — compile time vs run time, and what each artifact is for
- [docs/adding-a-portal.md](docs/adding-a-portal.md) — `portal.yaml` reference and a real portal, with v0.1's limits stated plainly
- [docs/troubleshooting.md](docs/troubleshooting.md) — every error, every exit code, what to do next
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, what a good pull request looks like
- [SECURITY.md](SECURITY.md) — reporting a vulnerability, and how portalc handles credentials

## Legal and scope

Only compile portals where you have your own account and the right to access them. Do not point portalc at banks or payment accounts — use PSD2. Do not publish copies of third-party pages: commit redacted fixtures and derived schemas, and keep `policy.readOnly: true` unless you have a written reason not to.

## License

MIT — see [LICENSE](LICENSE).
