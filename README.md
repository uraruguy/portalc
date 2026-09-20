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
| `portalc run <portal> <flow> --json` | Typed rows or a drift error (exit 6) |
| `portalc build` | Generate the TypeScript client |
| `portalc doctor` | Chrome/browser-use are optional; HTTP engine is enough for v0.1 |

## Engines

- **http** (v0.1 default): form login + HTML table. No LLM. Used by the demo and by any portal that is a session cookie plus a table.
- **chrome / browser-use**: reserved for explore/repair on portals that are not a table. Not required to complete the demo. Daily `verify` still must not ask a model whether the test passed.

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
