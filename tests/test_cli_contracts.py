"""Exit codes and workspace safety — the two things callers depend on."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from portalc.pipeline import explore, init_demo
from portalc.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def _portalc(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "portalc", *args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )


def test_run_exits_6_on_drift_while_verify_exits_8(workspace: Path):
    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    assert _portalc(workspace, "run", "demo-marina", "list-invoices", "--json").returncode == 0

    subprocess.check_call([sys.executable, str(ROOT / "demo" / "mutate.py"), "rename-total"])
    drifted = _portalc(workspace, "run", "demo-marina", "list-invoices", "--json")
    assert drifted.returncode == 6, drifted.stderr
    assert "total_eur" in drifted.stderr
    assert _portalc(workspace, "verify", "demo-marina").returncode == 8


def test_init_writes_a_gitignore_that_ignores_env(tmp_path: Path):
    result = init_demo(Workspace(tmp_path), base_url="http://127.0.0.1:8787")
    assert result["gitignoreUpdated"]
    assert (tmp_path / ".env").is_file()
    assert ".env" in (tmp_path / ".gitignore").read_text().splitlines()


def test_init_appends_env_to_an_existing_gitignore(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n")
    init_demo(Workspace(tmp_path), base_url="http://127.0.0.1:8787")
    lines = gitignore.read_text().splitlines()
    assert "node_modules/" in lines
    assert ".env" in lines


def test_init_leaves_an_existing_env_entry_alone(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".env*\n")
    result = init_demo(Workspace(tmp_path), base_url="http://127.0.0.1:8787")
    assert not result["gitignoreUpdated"]
    assert gitignore.read_text() == ".env*\n"
