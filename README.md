# portalc

Compile a login-walled portal into a **versioned, typed dependency**: JSON Schema, a TypeScript client, a contract test, and a patch when the portal changes.

If you cannot `git diff` the integration, you do not have an integration.

## How this differs from workflow-use, Integuru, and Browser Use Skills

| | Artifact | Where it lives | How you learn it broke |
|---|---|---|---|
| **workflow-use** | recorded workflow | a local JSON file | when a production call fails |
| **Integuru** | reverse-engineered HTTP API | their cloud | their on-call |
| **Browser Use Skills** | "deterministic API endpoint" | their cloud (`legacy` docs) | on the next call |
| **portalc** | typed SDK + schema + fixture + CI test | **your git repo, semver, CHANGELOG** | **nightly `verify`, before a customer sees it** |

portalc is not "an agent that clicks around every time". The agent (optional, later) is a *compiler*. The daily run is deterministic HTTP/CDP with no LLM verdict. Secrets never enter git — only environment variable *names*.

Use a real API when one exists. portalc is for the long tail of partner portals where you already have an account and the ToS allows it. Not banks (PSD2). Not bulk scraping.

## Five-minute onboarding (bundled demo)

The demo is a fake marina partner portal (login wall + invoice table). Nothing points at a real business.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
portalc doctor
portalc init --demo
portalc demo                 # leave this running
# other terminal:
portalc explore --yes
portalc verify
python demo/mutate.py rename-total   # portal still returns 200
portalc verify                       # fails: Total (EUR) → Amount (EUR)
portalc repair                       # writes a patch, 2/2 confirmations
portalc verify                       # green again
```

`portalc build` emits `generated/ts/portal-demo-marina/` — a typed client that shells out to `portalc run` so the package cannot smuggle secrets.

## Commands

| Command | What it does |
|---|---|
| `portalc init --demo` | Workspace, `portal.yaml`, GitHub Action, `.env.example` |
| `portalc demo` | Serve the bundled marina at `http://127.0.0.1:8787` |
| `portalc explore --yes` | Sign in, extract, dry-run twice, write schema/flow/fixture |
| `portalc verify` | Contract test: headers, JSON Schema, min rows. **No model.** |
| `portalc repair` | Re-read the page; auto-patch column *title* renames |
| `portalc repair --agent` | Ask a browser-use agent where the data moved, then prove it deterministically |
| `portalc run <portal> <flow> --json` | Typed rows or a drift error (exit 6) |
| `portalc build` | Generate the TypeScript client |
| `portalc doctor` | Reports the Chrome it found; browser-use stays optional |

## Engines

The browser layer is a plug-in. The artifacts do not depend on it: a flow compiled in Chrome is validated by the HTTP engine and the other way round.

- **http** (default): form login + HTML table over `urllib`. No browser, no LLM. Enough for any portal that is a session cookie plus a table — including the bundled demo.
- **chrome**: a real Chrome driven over CDP (`Page.navigate`, `Runtime.evaluate`, `Input.insertText`, `Page.captureScreenshot`). For portals that render their table in JavaScript, or that only let a human through the login. Still deterministic in `run` and `verify`: navigate, wait for the document, extract by rule.

```bash
portalc explore --engine chrome --yes          # Chrome portalc starts, profile kept for later runs
portalc explore --engine chrome --cdp-url http://127.0.0.1:9222   # attach to a Chrome you already use
portalc verify  --engine chrome --headless     # nightly, session from the profile
```

### What `portalc explore --engine chrome` needs

| Requirement | Why | If it is missing |
|---|---|---|
| Chrome or Chromium on `PATH` | the engine speaks CDP to it | `portalc doctor` says so; use `--engine http` |
| a desktop session, **or** `--headless` | a human cannot sign in to a headless window | `--headless` works for portals whose `auth.mode` is `form` |
| `~/.portalc/chrome-profile` (or `--chrome-profile`) | keeps the session a human signed in with, so 2FA is not asked twice | a fresh profile means signing in again |
| nothing else | no Playwright, no browser framework, no API key | — |

Attaching is the interesting mode: start your own Chrome with `--remote-debugging-port=9222`, sign in as yourself, then point portalc at it. portalc never types your password and there is no automated path past 2FA — for `auth.mode: human-session` it opens the page, waits for you, and inherits the session.

Environment equivalents, for CI: `PORTALC_CDP_URL`, `PORTALC_CHROME_HEADLESS=1`, `PORTALC_CHROME_PROFILE`, `PORTALC_CHROME_BINARY`, `PORTALC_CHROME_TIMEOUT`.

## Where the LLM is allowed

In two commands, and nowhere else:

```bash
pip install -e ".[agent]"                      # browser-use is an optional extra
portalc explore --engine chrome --agent --yes   # agent proposes where the data lives
portalc repair  --engine chrome --agent         # agent proposes where it moved to
```

The agent **proposes a location**; it never extracts the rows and never decides that a test passed. Whatever it proposes is then read by the deterministic engine, has to come back identical twice, has to satisfy the committed JSON Schema, and has to survive two green `verify` runs before a patch and a PR body are written. `run` and `verify` cannot call it at all — a model that grades its own homework is not a contract test.

It never receives a secret either: the human signs in and the agent inherits the session over the same CDP connection.

## Layout

```
portals/<name>/portal.yaml
portals/<name>/flows/<flow>.flow.json
portals/<name>/schemas/<schema>.schema.json
portals/<name>/fixtures/
portals/<name>/evidence/
generated/ts/portal-<name>/
portalc.lock
```

## Legal

Only compile portals where you have your own account and the right to access them. Do not point portalc at banks or payment accounts. Do not publish copies of third-party pages; commit redacted fixtures and derived schemas.

## License

MIT
