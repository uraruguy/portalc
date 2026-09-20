from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

MUTATE = ROOT / "demo" / "mutate.py"
DEMO_USER = "skipper@demo-marina.test"
DEMO_PASS = "demo-portal-password"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str) -> None:
    import urllib.request

    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=0.3) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"demo portal did not start at {url}")


@pytest.fixture
def demo_portal() -> Iterator[str]:
    subprocess.check_call([sys.executable, str(MUTATE), "reset"])
    port = _free_port()
    script = ROOT / "demo" / "portal" / "server.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait(url + "/healthz")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.check_call([sys.executable, str(MUTATE), "reset"])


@pytest.fixture
def workspace(tmp_path: Path, demo_portal: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MARINA_USER", DEMO_USER)
    monkeypatch.setenv("DEMO_MARINA_PASS", DEMO_PASS)
    os.environ.pop("PORTALC_QUIET", None)
    monkeypatch.setenv("PORTALC_QUIET", "1")
    from portalc.pipeline import init_demo
    from portalc.workspace import Workspace

    init_demo(Workspace(tmp_path), base_url=demo_portal, force=True)
    return tmp_path


@pytest.fixture
def compiled(workspace: Path):
    """A workspace with `list-invoices` already compiled — the post-explore state."""
    from portalc.pipeline import explore
    from portalc.workspace import Workspace

    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    return ws


@pytest.fixture
def mutate() -> Iterator[Callable[[str], None]]:
    """Apply a demo-portal mutation. The portal state file is shared, so always reset around it."""

    def apply(name: str) -> None:
        subprocess.check_call([sys.executable, str(MUTATE), name])

    apply("reset")
    yield apply
    apply("reset")


@dataclass
class CliResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def json(self):
        return json.loads(self.stdout)


@pytest.fixture
def cli() -> Callable[..., CliResult]:
    """Run the CLI the way a user does, but against this checkout rather than any installed copy."""

    def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> CliResult:
        child_env = {
            k: v for k, v in os.environ.items() if k not in {"PORTALC_QUIET", "PYTHONPATH"}
        }
        child_env["PYTHONPATH"] = str(ROOT / "src")
        child_env["COLUMNS"] = "200"  # rich wraps at the terminal width; keep assertions stable
        child_env.setdefault("DEMO_MARINA_USER", DEMO_USER)
        child_env.setdefault("DEMO_MARINA_PASS", DEMO_PASS)
        child_env.update(env or {})
        proc = subprocess.run(
            [sys.executable, "-m", "portalc", *args],
            cwd=str(cwd) if cwd else None,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return CliResult(list(args), proc.returncode, proc.stdout, proc.stderr)

    return run
