# Contributing to portalc

Thanks for looking. portalc is small on purpose, and the fastest way to get a change merged is to keep it that way.

By opening a pull request you agree that your contribution is licensed under the [MIT License](LICENSE).

## Get set up

```bash
git clone https://github.com/uraruguy/portalc.git
cd portalc
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
portalc doctor
```

`portalc doctor` should report Python 3.10+ and `jsonschema`. Chrome, `browser-use` and Node are listed as informational: none of them is needed to build, test or review portalc as it stands.

## Run the tests

```bash
pytest
```

The suite starts the bundled demo portal on a free port, compiles the `list-invoices` flow into a temporary workspace, breaks the portal, and checks that `verify` fails and `repair` fixes it. It needs no network access, no API key and no browser. Three tests, about a second.

Before you open a pull request, also walk the drift cycle by hand — it is the behaviour users care about and it catches things the unit tests do not:

```bash
portalc demo                         # terminal 1
portalc explore --yes                # terminal 2
portalc verify                       # green
python demo/mutate.py rename-total
portalc verify                       # must fail with exit 8
portalc repair
portalc verify                       # green again
python demo/mutate.py reset
```

`demo/mutate.py` also offers `rename-issued` and `status` if you want a second kind of drift to test against.

## How the code is laid out

| Module | Responsibility |
|---|---|
| `src/portalc/cli.py` | Typer commands. A thin skin: parse arguments, call `pipeline`, print through `ui` |
| `src/portalc/pipeline.py` | What each command actually does. Library code, no printing |
| `src/portalc/engine.py` | The deterministic HTTP engine: cookie jar, form login, fetch a table |
| `src/portalc/extract.py` | HTML table to typed columns and rows |
| `src/portalc/schemautil.py` | JSON Schema generation, the header contract, row validation |
| `src/portalc/codegen.py` | The generated TypeScript client |
| `src/portalc/workspace.py` | Where artifacts live, and how `portal.yaml` is read |
| `src/portalc/lockfile.py` | `portalc.lock`: versions, artifact hashes, last green run |
| `src/portalc/errors.py` | Every failure portalc can raise, with its exit code |
| `src/portalc/ui.py` | All terminal output. One console, one vocabulary |

## House rules

These are the decisions the project is built on. A pull request that breaks one of them needs to argue for it in the description.

1. **`verify` never asks a model anything.** The compile step (`explore`, `repair`) may eventually use an agent. The daily run may not. A test whose verdict comes from an LLM is not a test.
2. **Secrets are names, not values.** `portal.yaml` stores the *name* of an environment variable; values are read at call time and never written to an artifact, a fixture, or a log line.
3. **Errors come from `errors.py`, and every one of them has a hint.** Raise a `PortalcError` subclass with a title, detail lines and the next command to try. A traceback is what `--debug` is for.
4. **Exit codes are API.** They are how CI and the generated client tell drift from an outage. Do not reuse or renumber one; a new failure kind needs a new code, a new class, and a row in [docs/troubleshooting.md](docs/troubleshooting.md).
5. **Printing happens in `ui.py`.** No bare `print()`, no `rich` imports outside that module, and everything stays readable when output is piped or `--quiet` is set.
6. **Generated code is generated.** Change `codegen.py` and re-run `portalc build`; never hand-edit `generated/`.
7. **The demo stays synthetic.** `demo/` must never resemble a real company or hold real data, and the bundled credentials must stay obviously fake.

There is no linter configured yet. Match the style of the file you are editing: `from __future__ import annotations`, type hints on public functions, a module docstring that says why the module exists rather than what it contains.

## Commits and pull requests

- One logical change per commit, and a subject line that says what changed for the user.
- Update the docs in the same commit as the behaviour. A new flag with no documentation is an unfinished change.
- If you change a user-facing string, check whether `README.md` or `docs/` quotes it.
- If you change an artifact format, say what happens to workspaces that already have the old one.
- Keep pull requests reviewable. A 2,000-line pull request that moves modules around and adds a feature will get asked to split.

## Scope, so you do not waste a weekend

In v0.1 portalc compiles a form login plus an HTML table, over HTTP, with no LLM anywhere. That is deliberate: it is the shape most partner portals actually have, and it is the part that can be made deterministic.

Known gaps, all of them fair game to work on — open an issue first so two people do not build the same thing:

- the Chrome/CDP and `browser-use` engines for portals that are not a table (`--engine chrome` currently exits 7)
- write flows; today `policy.readOnly` is expected to be `true`
- a Python client alongside the TypeScript one
- `repair` for drift that is not a column rename — a moved column, a split field, a changed date format
- pagination, and portals whose data arrives as JSON rather than HTML

Please do not send pull requests that point portalc at a bank, add bulk scraping of a site you do not have an account on, or make the runtime path depend on a model.

## Reporting a bug

Open an issue with the command you ran, the full output including the `✗` line, and `portalc doctor`'s findings. Re-run with `--debug` for the traceback. Never paste real credentials, real customer rows or an un-redacted evidence file — a redacted fixture is almost always enough.

Security issues go to [SECURITY.md](SECURITY.md) instead, not to the issue tracker.
