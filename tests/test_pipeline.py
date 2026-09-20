from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from portalc.errors import VerificationFailed
from portalc.pipeline import build, explore, repair, run_flow, verify
from portalc.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def test_explore_is_deterministic_and_verify_passes(workspace: Path):
    ws = Workspace(workspace)
    result = explore(ws, "demo-marina", "list-invoices", accept=True)
    assert result["rowCount"] >= 6
    assert result["dryRuns"] == 2
    names = [c["name"] for c in result["columns"]]
    assert "total_eur" in names
    payload = verify(ws, "demo-marina", "list-invoices")
    assert payload["ok"]
    built = build(ws, "demo-marina")
    assert (Path(built["package"]) / "index.ts").is_file()
    ran = run_flow(ws, "demo-marina", "list-invoices", params={"month": "2026-09"})
    assert ran["ok"]
    assert ran["rows"][0]["currency"] == "EUR"


def test_rename_is_caught_then_repaired(workspace: Path):
    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    verify(ws, "demo-marina", "list-invoices")

    subprocess.check_call([sys.executable, str(ROOT / "demo" / "mutate.py"), "rename-total"])
    with pytest.raises(VerificationFailed) as caught:
        verify(ws, "demo-marina", "list-invoices")
    assert "total_eur" in caught.value.title or "Total" in str(caught.value.detail)

    patched = repair(ws, "demo-marina", "list-invoices")
    assert patched["kind"] == "rename"
    assert patched["confirmations"] == 2
    assert any("Amount (EUR)" in line for line in patched["diff"])
    payload = verify(ws, "demo-marina", "list-invoices")
    assert payload["ok"]

    schema = json.loads((workspace / "portals" / "demo-marina" / "schemas" / "invoice.schema.json").read_text())
    assert schema["x-portalc"]["headers"]["total_eur"] == "Amount (EUR)"
    assert "total_eur" in schema["items"]["properties"]
