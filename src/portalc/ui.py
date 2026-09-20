"""Terminal output for portalc.

One console, one vocabulary. Steps are numbered, results are green or red,
money is always shown when an LLM ran, and errors lead with the field that
broke rather than with a Python traceback.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Iterator, Sequence

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .errors import PortalcError

_console = Console(stderr=False, highlight=False, soft_wrap=False)
_err_console = Console(stderr=True, highlight=False)

OK = "[green]✓[/green]"
BAD = "[red]✗[/red]"
WARN = "[yellow]![/yellow]"
DOT = "[dim]·[/dim]"


def console() -> Console:
    return _console


def quiet() -> bool:
    return os.environ.get("PORTALC_QUIET") == "1"


def banner(command: str, subtitle: str = "") -> None:
    if quiet():
        return
    text = Text("portalc ", style="bold")
    text.append(command, style="bold cyan")
    if subtitle:
        text.append(f"  {subtitle}", style="dim")
    _console.print(text)


def step(message: str) -> None:
    if not quiet():
        _console.print(f"  [dim]→[/dim] {message}")


def ok(message: str) -> None:
    if not quiet():
        _console.print(f"  {OK} {message}")


def bad(message: str) -> None:
    _console.print(f"  {BAD} {message}")


def warn(message: str) -> None:
    if not quiet():
        _console.print(f"  {WARN} {message}")


def info(message: str) -> None:
    if not quiet():
        _console.print(f"  [dim]{message}[/dim]")


def blank() -> None:
    if not quiet():
        _console.print()


def rule(title: str = "") -> None:
    if not quiet():
        _console.print(Rule(title, style="dim"))


@contextlib.contextmanager
def spinner(message: str) -> Iterator[None]:
    """Progress that degrades to a plain line when output is not a TTY."""
    if quiet() or not _console.is_terminal:
        _console.print(f"  [dim]→[/dim] {message}") if not quiet() else None
        yield
        return
    with _console.status(f"[dim]{message}[/dim]", spinner="dots"):
        yield


def schema_table(title: str, fields: Sequence[dict[str, Any]]) -> None:
    """The review table shown after explore: field, type, required, sample."""
    if quiet():
        return
    table = Table(title=title, title_justify="left", title_style="bold", box=None, pad_edge=False)
    table.add_column("field", style="cyan", no_wrap=True)
    table.add_column("type", style="magenta", no_wrap=True)
    table.add_column("required", no_wrap=True)
    table.add_column("sample", style="dim", overflow="fold")
    for f in fields:
        table.add_row(
            f["name"],
            f.get("type", "string"),
            "[green]yes[/green]" if f.get("required") else "[dim]no[/dim]",
            str(f.get("sample", ""))[:44],
        )
    _console.print(table)


def findings_table(findings: Sequence[Any]) -> None:
    if quiet():
        return
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    table.add_column(" ", no_wrap=True, width=1)
    table.add_column("check", style="cyan", no_wrap=True)
    table.add_column("result", overflow="fold")
    for f in findings:
        mark = OK if f.ok else (WARN if f.severity == "warn" else BAD)
        table.add_row(mark, f.check, f.message)
    _console.print(table)


def cost_note(agent_usd: float | None, browser_seconds: float, engine: str) -> None:
    """Always say what a run cost. Silence about cost is how tools lose trust."""
    if quiet():
        return
    browser_usd = browser_seconds / 3600 * 0.02  # hosted browser reference rate
    if agent_usd is None:
        _console.print(
            f"  [dim]cost  no LLM call (engine: {engine})  ·  browser {browser_seconds:.0f}s "
            f"≈ ${browser_usd:.4f} if run on hosted infrastructure[/dim]"
        )
    else:
        _console.print(
            f"  [dim]cost  agent ≈ ${agent_usd:.2f} ({engine})  ·  browser {browser_seconds:.0f}s "
            f"≈ ${browser_usd:.4f}[/dim]"
        )


def next_steps(steps: Sequence[str], title: str = "Next") -> None:
    if quiet() or not steps:
        return
    body = Group(*[Text.from_markup(f"[dim]{i + 1}.[/dim] {s}") for i, s in enumerate(steps)])
    _console.print(Panel(body, title=title, title_align="left", border_style="dim", padding=(0, 1)))


def diff_block(lines: Sequence[str]) -> None:
    if quiet():
        return
    for line in lines:
        if line.startswith("+"):
            _console.print(f"  [green]{line}[/green]")
        elif line.startswith("-"):
            _console.print(f"  [red]{line}[/red]")
        else:
            _console.print(f"  [dim]{line}[/dim]")


def render_error(exc: BaseException, debug: bool = False) -> int:
    """Print a failure the way a developer tool should, and return an exit code."""
    if isinstance(exc, PortalcError):
        _err_console.print()
        _err_console.print(f"[bold red]✗ {exc.title}[/bold red]")
        for line in exc.detail:
            _err_console.print(f"  {line}")
        if exc.hint:
            _err_console.print(f"\n  [bold]try[/bold]  {exc.hint}")
        if debug:
            _err_console.print_exception()
        _err_console.print()
        return exc.exit_code

    _err_console.print()
    _err_console.print("[bold red]✗ portalc hit an unexpected error[/bold red]")
    _err_console.print(f"  {type(exc).__name__}: {exc}")
    _err_console.print(
        "\n  [bold]try[/bold]  rerun with --debug for the traceback, then open an issue with it"
    )
    if debug:
        _err_console.print_exception()
    _err_console.print()
    return 70


def print_json(payload: Any) -> None:
    import json

    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n")
    sys.stdout.flush()
