# portalc documentation

Start with [README.md](../README.md) for what portalc is and why it exists. These pages are the detail.

| Page | Read it when |
|---|---|
| [quickstart.md](quickstart.md) | You have just cloned portalc and want the demo to work, with the output you should see |
| [concepts.md](concepts.md) | You want to know what each artifact is for, and why compile time and run time are separate |
| [adding-a-portal.md](adding-a-portal.md) | You are pointing portalc at a portal of your own — `portal.yaml` reference, and what v0.1 cannot do |
| [troubleshooting.md](troubleshooting.md) | Something failed. Every error, every exit code, and the next command to try |

Contributor-facing pages live at the repository root: [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).

Everything here describes portalc v0.1. The run path — `run` and `verify` — is deterministic on either engine and never calls a model; only `explore` and `repair` can, and only when you pass `--agent`. Where a page says "not in v0.1", it means the code says so too and will tell you with an exit code.
