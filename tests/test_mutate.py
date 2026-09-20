"""demo/mutate.py is the drift harness the README and CI both drive."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DEMO_PASS, DEMO_USER, ROOT

STATE = ROOT / "demo" / "portal" / ".state.json"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "demo" / "mutate.py"), *args],
        capture_output=True, text=True, timeout=30,
    )


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"columnLabels": {}, "mutations": []}


def live_headers(base_url: str) -> list[str]:
    from portalc.engine import HttpEngine

    engine = HttpEngine(base_url)
    engine.login(DEMO_USER, DEMO_PASS)
    return engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices").table.header_contract()


def test_a_clean_portal_reports_itself_unmutated(mutate):
    assert "unmutated" in run("status").stdout
    assert state()["mutations"] == []


def test_status_is_the_default_action(mutate):
    assert run().stdout == run("status").stdout


@pytest.mark.parametrize(
    "mutation,field,label",
    [("rename-total", "total_eur", "Amount (EUR)"), ("rename-issued", "issued_on", "Issue date")],
)
def test_each_mutation_relabels_one_column(mutate, mutation, field, label):
    result = run(mutation)
    assert result.returncode == 0
    assert "still returns 200" in result.stdout
    assert state()["columnLabels"][field] == label
    assert state()["mutations"] == [mutation]
    assert mutation in run("status").stdout


def test_mutations_stack_and_do_not_duplicate(mutate):
    run("rename-total")
    run("rename-issued")
    run("rename-total")
    assert state()["mutations"] == ["rename-total", "rename-issued"]
    assert len(state()["columnLabels"]) == 2


def test_reset_puts_every_label_back(mutate):
    run("rename-total")
    run("rename-issued")
    assert run("reset").returncode == 0
    assert state() == {"columnLabels": {}, "mutations": []}


def test_an_unknown_mutation_is_refused_and_lists_the_real_ones(mutate):
    result = run("delete-everything")
    assert result.returncode == 2
    assert "rename-total" in result.stdout and "rename-issued" in result.stdout
    assert state()["mutations"] == []


def test_the_mutation_actually_changes_the_page_the_portal_serves(demo_portal: str, mutate):
    assert "Total (EUR)" in live_headers(demo_portal)

    run("rename-total")
    after = live_headers(demo_portal)
    assert "Amount (EUR)" in after
    assert "Total (EUR)" not in after

    run("reset")
    assert "Total (EUR)" in live_headers(demo_portal)


def test_the_mutation_moves_no_data_only_the_label(demo_portal: str, mutate):
    from portalc.engine import HttpEngine

    def rows() -> list[dict]:
        engine = HttpEngine(demo_portal)
        engine.login(DEMO_USER, DEMO_PASS)
        return engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices").table.rows

    before = rows()
    run("rename-total")
    assert rows() == before


def test_the_state_file_is_local_runtime_state_and_stays_out_of_git():
    ignored = (ROOT / ".gitignore").read_text()
    assert "demo/portal/.state.json" in ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "demo/portal/.state.json"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert tracked.returncode != 0
