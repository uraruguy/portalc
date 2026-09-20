# Security policy

portalc holds the keys to portals that have no API, which is exactly the kind of tool that deserves a boring, explicit security story. This page is that story, plus how to tell us when it is wrong.

## Reporting a vulnerability

Use GitHub's private reporting: **[Security → Report a vulnerability](https://github.com/uraruguy/portalc/security/advisories/new)** on this repository. Please do not open a public issue for anything that could expose someone's credentials or portal data.

Include the version or commit, what you did, what happened, and how bad you think it is. A minimal reproduction against the bundled demo portal is ideal, because it proves the problem without involving anyone's real account.

We will acknowledge your report, tell you what we found, and agree a disclosure date with you before anything is published. portalc is pre-1.0 and maintained on a best-effort basis; fixes land on `main` and are noted in the release entry. If you would like credit in the advisory, say so.

## Supported versions

Only `main` and the most recent release are supported. There are no backported fixes before 1.0.

## How portalc handles credentials

This is the part to read before you point portalc at a real portal.

- **Files hold names, never values.** `portal.yaml` records the *name* of the environment variable that holds each secret, under `auth.secrets`. Values are read from the process environment at call time and are not written back to disk.
- **`.env` is yours and is ignored by git.** `.gitignore` ignores `.env` and `.env.*` and tracks only `.env.example`. Never move a real value into `.env.example`, and never commit a `.env`.
- **Secrets are not written into artifacts.** Schemas, flows, fixtures, evidence files and `portalc.lock` contain structure and data, not credentials. A missing variable is reported by name — `missing: ACME_PORTAL_PASS` — and the value is never echoed.
- **The generated TypeScript client never sees a secret.** It spawns `portalc run` and reads JSON from stdout, which is the reason it works that way: a package in `node_modules` cannot smuggle out a credential it was never given.
- **In CI, use repository secrets** with the same variable names as `portal.yaml`, and give the job nothing else.

## Before you commit compiled artifacts

`portalc explore` writes a fixture and an evidence file from a page you were signed in to. For the bundled demo that is synthetic data. For a real portal it is **your own business data, and possibly someone else's**.

- Read the fixture and the evidence file before the first commit, and redact what does not belong in a repository.
- Evidence is deliberately trimmed to the table region, which is not the same as being safe to publish.
- Do not commit copies of a third-party portal's pages beyond what you need as review evidence.
- If your repository is public, assume every fixture in it is public.

## Trust boundaries in v0.1

Stated plainly, so nothing here surprises you:

- **The HTTP engine performs one POST — the login — and then GETs.** It has no code path that submits any other form, so a compiled flow cannot modify a portal.
- **`policy.readOnly` and `policy.allowedDomains` are declarations, not a sandbox.** They exist so a reviewer can see what a portal is allowed to do, and they are not yet enforced in code. Do not rely on them to stop a future write flow or a redirect to another host.
- **`portalc run` executes the compiled flow in your workspace.** Treat `portals/` as code: a flow file that arrives in a pull request can change which URL you sign in to, so review it like you would review a script.
- **The bundled demo is not a secure server.** It binds `127.0.0.1` by default, its credentials are public on purpose, and it exists to be broken by `demo/mutate.py`. Do not expose it, and do not copy its login handling into anything real.
- **`portalc doctor` prints paths, not contents.** It is safe to paste into an issue; command output that includes rows from a real portal is not.

## Out of scope

- The bundled demo portal's published credentials (`skipper@demo-marina.test` / `demo-portal-password`) and its deliberately weak session handling.
- Anything that requires pointing portalc at a site you do not have an account on, or at a bank or payment account. That is against the terms in [README.md](README.md#legal-and-scope), and portalc is not the right tool — use PSD2.
- Missing features described as not implemented in v0.1, such as the Chrome engine. Gaps are tracked in [CONTRIBUTING.md](CONTRIBUTING.md#scope-so-you-do-not-waste-a-weekend).
