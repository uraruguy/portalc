"""What happens when the portal changes overnight but still returns 200."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from portalc.errors import PortalDriftError, VerificationFailed
from portalc.pipeline import repair, run_flow, verify
from portalc.workspace import Workspace


def schema_of(ws: Workspace) -> dict:
    return json.loads((ws.root / "portals/demo-marina/schemas/invoice.schema.json").read_text())


def flow_of(ws: Workspace) -> dict:
    return json.loads((ws.root / "portals/demo-marina/flows/list-invoices.flow.json").read_text())


def changelog_of(ws: Workspace) -> str:
    return (ws.root / "portals/demo-marina/CHANGELOG.md").read_text()


# -- explore leaves a reviewable artifact --------------------------------


def test_explore_writes_flow_schema_fixture_and_evidence(compiled: Workspace):
    portal = compiled.root / "portals" / "demo-marina"
    assert flow_of(compiled)["engine"] == "http"
    assert flow_of(compiled)["schema"] == "invoice"
    fixtures = list((portal / "fixtures").glob("*.list-invoices.json"))
    evidence = list((portal / "evidence").glob("*.list-invoices.html"))
    assert len(fixtures) == 1 and len(evidence) == 1
    assert json.loads(fixtures[0].read_text())[0]["currency"] == "EUR"
    assert "<table" in evidence[0].read_text()
    assert "compiled `list-invoices`" in changelog_of(compiled)


def test_the_flow_pins_the_live_column_titles_as_the_contract(compiled: Workspace):
    step = next(s for s in flow_of(compiled)["steps"] if s["action"] == "extract_table")
    assert step["headerContract"][0] == "Invoice no."
    assert "Total (EUR)" in step["headerContract"]
    assert step["query"] == {"month": "{{month}}"}


def test_compiling_twice_produces_the_same_schema(workspace: Path):
    from portalc.pipeline import explore

    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    first = schema_of(ws)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    assert schema_of(ws) == first


def test_a_preview_run_shows_the_schema_without_writing_it(workspace: Path):
    from portalc.pipeline import explore

    ws = Workspace(workspace)
    result = explore(ws, "demo-marina", "list-invoices", accept=False)
    assert result["preview"] is True
    assert result["rowCount"] >= 6
    assert not (ws.root / "portals/demo-marina/schemas/invoice.schema.json").exists()


# -- each mutation the demo ships ----------------------------------------


@pytest.mark.parametrize(
    "mutation,field,old,new",
    [
        ("rename-total", "total_eur", "Total (EUR)", "Amount (EUR)"),
        ("rename-issued", "issued_on", "Issued", "Issue date"),
    ],
)
def test_a_renamed_column_fails_verify_then_repairs(compiled: Workspace, mutate, mutation, field, old, new):
    mutate(mutation)

    with pytest.raises(VerificationFailed) as caught:
        verify(compiled, "demo-marina", "list-invoices")
    assert field in caught.value.title
    assert old in caught.value.title and new in caught.value.title

    patch = repair(compiled, "demo-marina", "list-invoices")
    assert patch["kind"] == "rename"
    assert patch["confirmations"] == 2
    assert f"- {field}: {old}" in patch["diff"]
    assert f"+ {field}: {new}" in patch["diff"]
    assert verify(compiled, "demo-marina", "list-invoices")["ok"]
    assert schema_of(compiled)["x-portalc"]["headers"][field] == new


def test_two_renames_at_once_are_patched_in_one_go(compiled: Workspace, mutate):
    mutate("rename-total")
    mutate("rename-issued")

    with pytest.raises(VerificationFailed):
        verify(compiled, "demo-marina", "list-invoices")

    patch = repair(compiled, "demo-marina", "list-invoices")
    assert patch["version"] == "0.1.1"
    assert "+ total_eur: Amount (EUR)" in patch["diff"]
    assert "+ issued_on: Issue date" in patch["diff"]
    headers = schema_of(compiled)["x-portalc"]["headers"]
    assert headers["total_eur"] == "Amount (EUR)"
    assert headers["issued_on"] == "Issue date"
    assert verify(compiled, "demo-marina", "list-invoices")["ok"]


def test_a_rename_never_changes_a_field_name_so_callers_keep_compiling(compiled: Workspace, mutate):
    before = sorted(schema_of(compiled)["items"]["properties"])
    mutate("rename-total")
    repair(compiled, "demo-marina", "list-invoices")
    assert sorted(schema_of(compiled)["items"]["properties"]) == before
    assert sorted(schema_of(compiled)["items"]["required"]) == before


def test_repair_rewrites_the_header_contract_in_the_flow_file_too(compiled: Workspace, mutate):
    mutate("rename-total")
    repair(compiled, "demo-marina", "list-invoices")
    step = next(s for s in flow_of(compiled)["steps"] if s["action"] == "extract_table")
    assert "Amount (EUR)" in step["headerContract"]
    assert "Total (EUR)" not in step["headerContract"]


def test_repair_writes_a_reviewable_pr_body_and_a_changelog_line(compiled: Workspace, mutate):
    mutate("rename-total")
    patch = repair(compiled, "demo-marina", "list-invoices")

    body = Path(patch["prPath"]).read_text()
    assert "column title rename" in body
    assert "'Total (EUR)' → 'Amount (EUR)'" in body
    assert "confirmations: 2/2" in body
    assert "never auto-merge" in body
    assert "repair `list-invoices` (patch 0.1.1)" in changelog_of(compiled)


def test_verify_is_green_again_after_the_portal_is_put_back(compiled: Workspace, mutate):
    mutate("rename-total")
    with pytest.raises(VerificationFailed):
        verify(compiled, "demo-marina", "list-invoices")
    mutate("reset")
    assert verify(compiled, "demo-marina", "list-invoices")["ok"]


# -- drift that repair must refuse ---------------------------------------


def test_repair_refuses_when_a_field_disappeared_rather_than_moved(compiled: Workspace):
    path = compiled.root / "portals/demo-marina/schemas/invoice.schema.json"
    schema = json.loads(path.read_text())
    schema["x-portalc"]["headers"]["mooring_ref"] = "Mooring ref"
    schema["items"]["properties"]["mooring_ref"] = {"type": "string"}
    path.write_text(json.dumps(schema, indent=2))

    with pytest.raises(PortalDriftError) as caught:
        repair(compiled, "demo-marina", "list-invoices")
    assert caught.value.exit_code == 6
    assert "cannot auto-patch" in caught.value.title
    assert "mooring_ref" in caught.value.data["expected"]
    assert "mooring_ref" not in caught.value.data["live"]


def test_a_missing_field_fails_verify_and_names_it(compiled: Workspace):
    path = compiled.root / "portals/demo-marina/schemas/invoice.schema.json"
    schema = json.loads(path.read_text())
    schema["x-portalc"]["headers"]["mooring_ref"] = "Mooring ref"
    path.write_text(json.dumps(schema, indent=2))

    with pytest.raises(VerificationFailed) as caught:
        verify(compiled, "demo-marina", "list-invoices")
    assert "mooring_ref" in caught.value.title


# -- what a failing verify hands back ------------------------------------


def test_a_failed_verify_carries_the_per_flow_report_the_cli_prints(compiled: Workspace, mutate):
    mutate("rename-total")
    with pytest.raises(VerificationFailed) as caught:
        verify(compiled, "demo-marina")
    payload = caught.value.data
    assert payload["ok"] is False
    report = payload["reports"][0]
    assert (report["portal"], report["flow"], report["ok"]) == ("demo-marina", "list-invoices", False)
    assert any(f.check == "headers.renamed" for f in report["findings"])
    assert caught.value.exit_code == 8
    assert "portalc repair" in caught.value.hint


def test_verify_without_a_portal_covers_every_compiled_flow(compiled: Workspace):
    payload = verify(compiled)
    assert payload["ok"]
    assert [(r["portal"], r["flow"]) for r in payload["reports"]] == [("demo-marina", "list-invoices")]


def test_verify_checks_min_rows_from_the_checks_file(compiled: Workspace):
    checks = compiled.root / "portals/demo-marina/flows/list-invoices.checks.json"
    assert json.loads(checks.read_text()) == {"minRows": 1, "currencyIn": ["EUR"]}
    checks.write_text(json.dumps({"minRows": 500}))
    with pytest.raises(VerificationFailed) as caught:
        verify(compiled, "demo-marina", "list-invoices")
    assert "minRows 500" in caught.value.title


# -- run refuses to hand back rows that broke the contract ---------------


def test_run_returns_typed_rows_for_a_good_month(compiled: Workspace):
    result = run_flow(compiled, "demo-marina", "list-invoices", params={"month": "2026-09"})
    assert result["ok"]
    assert len(result["rows"]) >= 6
    row = result["rows"][0]
    assert isinstance(row["total_eur"], float)
    assert row["currency"] == "EUR"
    assert row["issued_on"].startswith("2026-09")


def test_run_refuses_the_placeholder_row_of_an_empty_month(compiled: Workspace):
    result = run_flow(compiled, "demo-marina", "list-invoices", params={"month": "2026-13"})
    assert not result["ok"]
    assert any(f.check == "schema" and not f.ok for f in result["findings"])


def test_run_reports_drift_without_validating_rows(compiled: Workspace, mutate):
    mutate("rename-total")
    result = run_flow(compiled, "demo-marina", "list-invoices", validate=False)
    assert not result["ok"]
    assert [f.check for f in result["findings"]] == ["headers.renamed"]
