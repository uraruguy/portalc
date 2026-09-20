# Adding a portal of your own

**Read this before you start: in v0.1 the `explore` command is wired to the shape of the bundled demo, and pointing it at an arbitrary portal will not work yet.** The [limits](#what-v01-actually-requires) below are precise about why, so you can tell in ten minutes whether your portal is a candidate, instead of finding out after an afternoon of guessing.

What *is* usable today: the `portal.yaml` format, the artifact layout, and the whole verify/repair cycle once a flow is compiled. If you are here to make `explore` general, [CONTRIBUTING.md](../CONTRIBUTING.md#scope-so-you-do-not-waste-a-weekend) is the page you want.

## Before anything technical

- You must have **your own account** on the portal, and the right to use it this way. Check the terms.
- If the partner has an API, use the API. portalc is for portals that will never have one.
- Never a bank, never a payment account. That is PSD2 territory and licensed providers.
- Decide where the data will live and who may see it. A fixture from a real portal is real data — see [SECURITY.md](../SECURITY.md#before-you-commit-compiled-artifacts).

## `portal.yaml`

One file, written by hand, at `portals/<name>/portal.yaml`.

```yaml
apiVersion: portalc/v1          # required, exactly this
name: acme-partner              # required; must match the directory name
title: ACME Partner Portal      # optional, for humans; defaults to name
baseUrl: https://partners.acme.example   # required; ${VAR} and ${VAR:-default} are expanded
auth:
  mode: form                    # form, or human-session with --engine chrome
  loginPath: /login             # POST target; defaults to /login
  secrets:
    username: ACME_PORTAL_USER  # the *name* of the env var, never the value
    password: ACME_PORTAL_PASS
policy:
  readOnly: true
  allowedDomains:
    - partners.acme.example
  notes: >
    Own-account access under the 2026 partner agreement. Read-only.
flows:
  - list-orders
```

| Key | Required | Notes |
|---|---|---|
| `apiVersion` | yes | Must be `portalc/v1`. Anything else is rejected with exit 2 |
| `name` | yes | Keep it equal to the directory name; it is what you pass on the command line |
| `title` | no | Display only |
| `baseUrl` | yes | Trailing slashes are stripped. `${ACME_BASE_URL}` and `${ACME_BASE_URL:-https://…}` are expanded at load time; a missing variable with no default is an error, by design |
| `auth.mode` | no | `form` posts the credentials named below. `human-session` (Chrome engine) opens the page and waits for you to sign in, then inherits the session |
| `auth.loginPath` | no | Defaults to `/login` |
| `auth.secrets` | for a real portal | A map of `username` / `password` to environment variable names. Values are read at call time and never written anywhere |
| `policy.readOnly` | no | Defaults to `true`. **Enforced**: the login form is the only POST a read-only portal may make (exit 9) |
| `policy.allowedDomains` | no | Defaults to the host in `baseUrl`. **Enforced** on every request and every redirect, by both engines (exit 9) |
| `policy.notes` | no | Free text. Say which agreement permits this access; your future colleague will need it |
| `flows` | no | Names you intend to compile. The files under `flows/` are the source of truth |

`allowedDomains` and `readOnly` do two jobs: they make a portal's intended scope reviewable in a pull request, and they are enforced at request time. They constrain portalc, not the machine it runs on — see [SECURITY.md](../SECURITY.md#trust-boundaries).

## Secrets

Put the values in `.env` locally (it is gitignored) and in repository secrets in CI, under the same names:

```bash
ACME_PORTAL_USER=you@example.com
ACME_PORTAL_PASS=…
```

`portal.yaml` keeps only the names. If a variable is missing, portalc exits 3 and names it — `missing: ACME_PORTAL_PASS` — without printing anything it read.

## What v0.1 actually requires

`portalc explore` in v0.1 compiles one very specific shape. All three of these must hold:

1. **A form login that accepts fields literally named `username` and `password`,** POSTed to `auth.loginPath`, that sets a session cookie. A portal whose form uses `email`, `user[login]` or a CSRF token will not authenticate, and portalc will tell you it was sent back to the login form (exit 3).
2. **The table must carry `data-portalc="<marker>"`.** portalc selects the table by that attribute, and the bundled demo is the thing that ships it. A real portal's `<table class="grid">` will not be found, and you get `could not find the expected table` (exit 5).
3. **The path and the marker are not command-line options.** `explore` always reads `/invoices` looking for marker `invoices`. There is no flag for `/orders`.

So a real portal needs code, not configuration. The pieces are small and they are all in one place — `explore()` in `src/portalc/pipeline.py` takes `path` and `marker` arguments that the CLI never passes, and `extract_table()` in `src/portalc/extract.py` already matches the first top-level table when the marker is `None`. Exposing them properly (and choosing the schema name when there is no marker to derive it from) is a good first contribution.

Beyond that shape, the honest list of what is missing: pagination, multi-step navigation, downloads, JSON endpoints, and write flows. A JavaScript-rendered table is the one item off that list — `--engine chrome` reads the rendered DOM — but the three requirements above still apply to it.

## Once a flow is compiled

Everything after `explore` is portal-agnostic and works today:

```bash
portalc verify acme-partner            # contract test: headers, schema, checks
portalc build acme-partner             # typed client under generated/ts/
portalc run acme-partner list-orders --set month=2026-09 --json
portalc repair acme-partner --flow list-orders
```

Tune the non-schema assertions by hand in `portals/acme-partner/flows/list-orders.checks.json`:

```json
{
  "minRows": 1,
  "currencyIn": ["EUR"]
}
```

`minRows` is the one `verify` enforces today, and it guards the failure mode nothing else catches: the portal returns a perfectly valid empty table and your nightly reconciliation quietly balances to zero. `currencyIn` is written by `explore` when the table has a currency column and is not asserted yet — it is a placeholder, so do not rely on it.

Then commit the whole directory, add the credentials to CI secrets under the names from `portal.yaml`, and let `.github/workflows/portalc.yml` tell you at 03:00 when the partner changes something. That is the point of all of this.
