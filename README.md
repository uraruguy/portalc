# portalc

[![portalc verify](https://github.com/uraruguy/portalc/actions/workflows/portalc.yml/badge.svg)](https://github.com/uraruguy/portalc/actions/workflows/portalc.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Compile a login-walled portal into a **versioned, typed dependency**: JSON Schema, a TypeScript client, a contract test, and a patch when the portal changes. **If you cannot `git diff` the integration, you do not have an integration.**

## How this differs from workflow-use, Integuru, and Browser Use Skills

| | Artifact | Where it lives | How you learn it broke |
|---|---|---|---|
| **workflow-use** | recorded workflow | a local JSON file | when a production call fails |
| **Integuru** | reverse-engineered HTTP API | their cloud | their on-call |
| **Browser Use Skills** | "deterministic API endpoint" | their cloud (`legacy` docs) | on the next call |
| **portalc** | typed SDK + schema + fixture + CI test | **your git repo, semver, CHANGELOG** | **nightly `verify`, before a customer sees it** |

portalc is not "an agent that clicks around every time". The agent (optional, later) is a *compiler*. The daily run is deterministic HTTP/CDP with no LLM verdict. Secrets never enter git — only environment variable *names*.

Use a real API when one exists. portalc is for the long tail of partner portals where you already have an account and the ToS allows it. Not banks (PSD2). Not bulk scraping.

## Requirements

- Python 3.10 or newer (3.12 is what CI runs)
- Nothing else for the bundled demo. Chrome, `browser-use` and Node are optional — `portalc doctor` tells you what it found and what it does not need.

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
| `portalc doctor` | What this machine can run. Chrome and `browser-use` are optional |
| `portalc init --demo` | Scaffold a new workspace elsewhere (`--path`). The clone is already set up |
| `portalc demo` | Serve the bundled marina at `http://127.0.0.1:8787` |
| `portalc explore --yes` | Sign in, extract, dry-run twice, write schema, flow and fixture |
| `portalc verify` | Contract test: headers, JSON Schema, min rows. **No model** |
| `portalc repair` | Re-read the page and patch a column *title* rename |
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

- **http** — the v0.1 default and the daily-run path: form login plus an HTML table, no LLM, milliseconds per call. It is what the demo and the test suite run on. Compiling a portal of your own is not a configuration change yet: [docs/adding-a-portal.md](docs/adding-a-portal.md#what-v01-actually-requires) states the three things `explore` currently hard-codes, so you can judge in ten minutes rather than an afternoon.
- **chrome / browser-use** — reserved for `explore` and `repair` on portals that are not a table. Not wired up in v0.1; `--engine chrome` exits 7 and says so. Daily `verify` will stay deterministic either way: a model never gets to decide that a test passed.

## Continuous verification

`.github/workflows/portalc.yml` runs `portalc verify --all` nightly at 03:00 UTC and on every push that touches a portal. That schedule is the point of the product — you find out that a partner changed a column at 03:00, from CI, not from a customer. Put the portal's credentials in repository secrets under the env var names from `portal.yaml`.

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
