"""The optional LLM step — and the only place portalc lets a model near a portal.

Three rules, enforced here rather than merely documented:

1. **Two phases only.** `explore` and `repair` may call an agent. `run` and
   `verify` may not: a model that grades its own homework is not a test.
2. **It proposes, it does not decide.** The agent returns *where* to look. The
   deterministic engine then has to reproduce that twice, and the result still
   has to satisfy the JSON Schema, before anything is written to disk.
3. **It never sees a secret.** The human signs in (`await_human_login`), the
   agent inherits the session over the same CDP connection.

`browser-use` is an optional extra (`pip install -e ".[agent]"`). The `runner`
argument is the seam: portalc's own tests inject a stub runner so the
proposal → deterministic-confirmation plumbing is covered without an API key,
and the browser-use runner below is the only part that needs the real library.
"""

from __future__ import annotations

import importlib.util
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import BrowserUnavailableError, UsageError

#: A runner receives a task and a context, and returns the proposal as a dict.
AgentRunner = Callable[[str, dict[str, Any]], dict[str, Any]]

PROPOSAL_FIELDS = {
    "path": "URL path the data lives on, relative to the portal base URL",
    "marker": "value of the table's data-portalc attribute, or null if it has none",
    "query": "query-string parameters needed to reach that data",
    "notes": "one sentence a human will read in the pull request",
}


@dataclass
class FlowProposal:
    """Where the agent thinks the flow's data is. Unverified by construction."""

    path: str
    marker: str | None = None
    query: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "marker": self.marker,
            "query": dict(self.query),
            "notes": self.notes,
            "usd": self.usd,
        }


def browser_use_available() -> bool:
    return importlib.util.find_spec("browser_use") is not None


def require_browser_use() -> None:
    if not browser_use_available():
        raise BrowserUnavailableError(
            "browser-use is not installed",
            "the agent is an optional extra; explore and repair work deterministically without it",
            hint='pip install -e ".[agent]"   and export the API key for your model',
        )


def _check_host_allowed(base_url: str, allowed_domains: list[str] | tuple[str, ...]) -> None:
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if allowed_domains and host not in set(allowed_domains):
        raise UsageError(
            f"the agent is not allowed to visit {host!r}",
            f"policy.allowedDomains in portal.yaml lists: {', '.join(allowed_domains)}",
            hint="add the host to policy.allowedDomains if you really have the right to access it",
        )


def explore_task(base_url: str, flow: str, params: dict[str, str]) -> str:
    wanted = ", ".join(f"{k}={v}" for k, v in params.items()) or "no parameters"
    return (
        f"You are compiling the read-only flow {flow!r} on {base_url}.\n"
        "Someone has already signed in for you in this browser — never type or ask for credentials, "
        "and never submit a form that changes data.\n"
        f"Find the page that lists the data this flow is about ({wanted}) and report where it is.\n"
        "Return the path, the table's data-portalc marker if it has one, the query parameters needed, "
        "and one sentence of notes. Do not extract the rows: deterministic code does that."
    )


def repair_task(base_url: str, flow: str, failure: str, previous_path: str) -> str:
    return (
        f"The compiled read-only flow {flow!r} on {base_url} used to read its table from "
        f"{previous_path!r} and now fails with: {failure}\n"
        "Someone has already signed in for you in this browser — never type credentials, "
        "never submit a form that changes data.\n"
        "Find where that same data lives now and report the path, the table's data-portalc marker "
        "if it has one, the query parameters, and one sentence describing what the portal changed."
    )


def propose(
    task: str,
    *,
    base_url: str,
    cdp_url: str | None = None,
    allowed_domains: list[str] | tuple[str, ...] = (),
    runner: AgentRunner | None = None,
    max_steps: int = 25,
    model: str | None = None,
) -> FlowProposal:
    """Run one narrow agent task and return its proposal. Never writes anything."""
    _check_host_allowed(base_url, allowed_domains)
    run = runner or browser_use_runner()
    payload = run(
        task,
        {
            "cdp_url": cdp_url,
            "base_url": base_url,
            "max_steps": max_steps,
            "model": model,
            "fields": PROPOSAL_FIELDS,
        },
    )
    if not isinstance(payload, dict) or not payload.get("path"):
        raise BrowserUnavailableError(
            "the agent returned no usable proposal",
            f"got: {payload!r}",
            hint="re-run with --debug, or repair by hand: portalc explore --engine chrome --yes",
        )
    query = payload.get("query") or {}
    if not isinstance(query, dict):
        query = {}
    return FlowProposal(
        path=str(payload["path"]),
        marker=(str(payload["marker"]) if payload.get("marker") else None),
        query={str(k): str(v) for k, v in query.items()},
        notes=str(payload.get("notes") or ""),
        usd=(float(payload["usd"]) if payload.get("usd") is not None else None),
    )


def browser_use_runner() -> AgentRunner:
    """The real runner: a browser-use agent on the CDP connection portalc opened.

    Kept to a single function so the dependency stays optional and replaceable —
    the browser layer is a plug-in, not the product. It is exercised only when
    `--agent` is passed and `browser-use` is installed; everything else in
    portalc is covered without it.
    """

    def run(task: str, ctx: dict[str, Any]) -> dict[str, Any]:
        require_browser_use()
        import asyncio

        from browser_use import Agent, Browser  # type: ignore[import-not-found]
        from pydantic import BaseModel, Field  # type: ignore[import-not-found]

        class Proposal(BaseModel):
            path: str = Field(description=PROPOSAL_FIELDS["path"])
            marker: str | None = Field(default=None, description=PROPOSAL_FIELDS["marker"])
            query: dict[str, str] = Field(default_factory=dict, description=PROPOSAL_FIELDS["query"])
            notes: str = Field(default="", description=PROPOSAL_FIELDS["notes"])

        async def drive() -> Any:
            browser = Browser(cdp_url=ctx.get("cdp_url")) if ctx.get("cdp_url") else Browser()
            kwargs: dict[str, Any] = {
                "task": task,
                "browser": browser,
                "output_model_schema": Proposal,
                # never let a password end up in a screenshot sent to a model
                "use_vision": False,
            }
            if ctx.get("model"):
                kwargs["llm"] = ctx["model"]
            agent = Agent(**kwargs)
            return await agent.run(max_steps=int(ctx.get("max_steps") or 25))

        history = asyncio.run(drive())
        raw = history.final_result()
        if raw is None:
            return {}
        proposal = raw if isinstance(raw, Proposal) else Proposal.model_validate_json(raw)
        usd = getattr(getattr(history, "usage", None), "total_cost_usd", None)
        return {**proposal.model_dump(), "usd": usd}

    return run
