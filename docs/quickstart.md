# Quickstart

Ten commands, no API key, no browser, no network access beyond your own loopback interface. At the end you will have watched a portal break an integration silently, and watched portalc catch it.

The portal is the bundled **Demo Marina**: a synthetic partner portal with a login wall and an invoice table. It is not a real marina, its credentials are public on purpose, and `demo/mutate.py` exists to break it.

## 1. Install

```bash
git clone https://github.com/uraruguy/portalc.git
cd portalc
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On Debian and Ubuntu, `python3 -m venv` needs `sudo apt install python3-venv` first.

## 2. Check the machine

```bash
portalc doctor
```

```
portalc doctor  /home/you/portalc
   check        result
✓  python       3.12.3 (needs >=3.10)
✓  jsonschema   installed
✓  chrome       /usr/bin/google-chrome
✓  browser-use  not installed — optional, only for agent explore/repair
✓  node         /usr/bin/node
✓  demo         /home/you/portalc/demo/portal/server.py
✓  workspace    /home/you/portalc  portals: demo-marina
```

`chrome`, `browser-use` and `node` are informational. Nothing in this walkthrough needs them, and `workspace … portals: demo-marina` is the line that matters: the clone already contains a compiled portal.

There is no `portalc init` step here. The clone is the workspace. `portalc init --demo` scaffolds a *new* workspace somewhere else with `--path`, and in the clone it exits 2 with `portal 'demo-marina' already exists`, which is correct and not your mistake.

You do not need a `.env` either. `portalc` falls back to the published demo credentials when `DEMO_MARINA_USER` and `DEMO_MARINA_PASS` are unset. `.env.example` explains the pattern for when you get to a real portal.

## 3. Start the portal

In one terminal, and leave it running:

```bash
portalc demo
```

```
portalc demo  http://127.0.0.1:8787
  /home/you/portalc/demo/portal/server.py
  sign in as skipper@demo-marina.test / demo-portal-password
```

Open `http://127.0.0.1:8787` in a browser if you want to see what portalc is about to read. If port 8787 is taken, use `portalc demo --port 9911` **and** change `baseUrl` in `portals/demo-marina/portal.yaml` to match — the compiled flow is pinned to that origin.

Everything below runs in a second terminal, with the virtualenv activated.

## 4. Compile the flow

```bash
portalc explore --yes
```

```
portalc explore  demo-marina/list-invoices
  → signing in (credentials from the environment, never from git)
schema  list-invoices
field      type    required  sample
number     string  yes       2026-000328
issued_on  date    yes       2026-09-09
due_on     date    yes       2026-09-23
berth      string  yes       A-02
vessel     string  yes       Gulet Anima Maris
net_eur    number  yes       3687.71
vat_eur    number  yes       811.3
total_eur  number  yes       4499.01
currency   string  yes       EUR
status     string  yes       open
  ✓ 6 rows  ·  dry-run  2/2 identical
  ✓ wrote portals/demo-marina/flows/list-invoices.flow.json
  ✓ wrote portals/demo-marina/schemas/invoice.schema.json
  ✓ wrote portals/demo-marina/fixtures/2026-09-20.list-invoices.json
  cost  no LLM call (engine: http)  ·  browser 1s ≈ $0.0000 if run on hosted infrastructure
```

Three things worth noticing:

- **`dry-run 2/2 identical`.** portalc reads the page twice and refuses to compile a flow whose rows are not reproducible. A portal that cannot answer the same question twice is not a v1 candidate.
- **Types are inferred, not guessed at runtime.** `net_eur` is a `number` and `issued_on` is a `date` from here on. A row that stops matching is an error, not a surprise `"3.687,71"` in your database.
- **`cost  no LLM call`.** Every run says what it cost. On the HTTP engine, nothing.

Drop `--yes` at a terminal and portalc prints the same table, writes nothing, and exits 2 — a review step for when the portal is not a demo. Off a terminal, where there is nobody to review it, `explore` compiles as if `--yes` had been passed, so a script or a CI job does not hang on a review nobody will do.

## 5. Run the contract test

```bash
portalc verify
```

```
portalc verify  all portals
  ✓ demo-marina/list-invoices  6 rows
   check    result
✓  headers  10 column headers match the contract
✓  schema   6 rows match Invoice
✓  minRows  6 rows (min 1)
```

Three checks, no model: the column headers still say what they said at compile time, every row validates against the JSON Schema, and there are at least as many rows as `flows/list-invoices.checks.json` demands.

## 6. Generate the typed client

```bash
portalc build
```

```
portalc build  demo-marina
  ✓ generated generated/ts/portal-demo-marina  v0.1.0
  the client shells out to `portalc run` so secrets never enter the package
```

```ts
import { listInvoices } from "@portalc/portal-demo-marina";

const rows = await listInvoices({ month: "2026-09" });
```

`rows` is `Invoice[]`. The package spawns `portalc run` and parses JSON, so the credentials stay in your environment; `portalc` needs to be on `PATH`, or pass `{ bin: "/path/to/portalc" }`.

You can see the same data the client sees:

```bash
portalc run demo-marina list-invoices --set month=2026-09 --json
```

## 7. Break the portal the way portals actually break

```bash
python demo/mutate.py rename-total
```

```
demo portal mutated: "Total (EUR)" column header renamed to "Amount (EUR)"
the portal still returns 200 and still renders every row — nothing looks broken
```

This is the whole problem in one line. No status code changed. No exception fired. A scraper keyed on column order carries on and quietly starts reading the wrong money.

```bash
portalc verify
```

```
portalc verify  all portals
  ✗ demo-marina/list-invoices
   check            result
✗  headers.renamed  total_eur: 'Total (EUR)' → 'Amount (EUR)'
✓  schema           6 rows match Invoice
✓  minRows          6 rows (min 1)

✗ total_eur: 'Total (EUR)' → 'Amount (EUR)'
  demo-marina/list-invoices: total_eur: 'Total (EUR)' → 'Amount (EUR)'

  try  portalc repair <portal> --flow <flow>   will re-read the page and write a patch
```

Exit code 8. In CI that is a red build at 03:00, naming the field, the old header and the new one.

## 8. Repair it

```bash
portalc repair
```

```
portalc repair  demo-marina/list-invoices
  → re-reading the live page (no LLM on --engine http)
  ✓ rename patch  →  v0.1.1  ·  confirmations 2/2
  - total_eur: Total (EUR)
  + total_eur: Amount (EUR)
  ✓ PR body  .portalc/repair-demo-marina-list-invoices.md
  cost  no LLM call (engine: http)  ·  browser 2s ≈ $0.0000 if run on hosted infrastructure
```

`repair` only handles the case it can prove: a column whose *title* changed while its position, type and values did not. It confirms that twice, bumps the portal to `0.1.1` in `portalc.lock`, writes the diff and leaves a pull request body on disk. The field name in your code — `total_eur` — does not change, so nothing downstream has to.

```bash
portalc verify
```

```
portalc verify  all portals
  ✓ demo-marina/list-invoices  6 rows
   check    result
✓  headers  10 column headers match the contract
```

Green. `git diff` now shows exactly what the partner did:

```bash
git diff --stat
```

The patch is a reviewable change to a schema and a lockfile, not a mystery in a log.

## 9. Put the demo back

```bash
python demo/mutate.py reset
git checkout -- portals generated portalc.lock   # discard the walkthrough's artifacts
git status                                       # today's fixture is a new file; delete it if you like
```

`demo/mutate.py` also has `rename-issued` for a second rename and `status` for what is currently mutated.

## Where to go next

- [concepts.md](concepts.md) — why compile time and run time are separate, and what each artifact is for
- [adding-a-portal.md](adding-a-portal.md) — doing this against a portal of your own
- [troubleshooting.md](troubleshooting.md) — if any step above did not look like this
