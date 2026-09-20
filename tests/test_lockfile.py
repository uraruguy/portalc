from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from portalc.lockfile import (
    bump,
    load_lock,
    record_green,
    save_lock,
    sha256_file,
    sha256_text,
)
from portalc.pipeline import build, repair, verify
from portalc.workspace import Workspace

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.mark.parametrize(
    "version,level,expected",
    [
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.9", "patch", "0.1.10"),
        ("0.1.3", "minor", "0.2.0"),
        ("1.4.3", "major", "2.0.0"),
        ("1.4.3", "anything-else", "1.4.4"),
    ],
)
def test_bump(version, level, expected):
    assert bump(version, level) == expected


def test_an_absent_lockfile_reads_as_an_empty_one(tmp_path: Path):
    assert load_lock(Workspace(tmp_path)) == {"apiVersion": "portalc/v1", "portals": {}}


def test_the_lockfile_round_trips_as_readable_json(tmp_path: Path):
    ws = Workspace(tmp_path)
    save_lock(ws, {"apiVersion": "portalc/v1", "portals": {"acme": {"version": "0.2.0"}}})
    assert ws.lockfile_path.read_text().endswith("\n")
    assert load_lock(ws)["portals"]["acme"]["version"] == "0.2.0"


def test_file_and_text_hashes_agree(tmp_path: Path):
    path = tmp_path / "x.json"
    path.write_text('{"a":1}')
    assert sha256_file(path) == sha256_text('{"a":1}')


def test_record_green_pins_the_hash_of_the_files_it_blessed(tmp_path: Path):
    ws = Workspace(tmp_path)
    flow = tmp_path / "list-invoices.flow.json"
    schema = tmp_path / "invoice.schema.json"
    flow.write_text('{"name":"list-invoices"}')
    schema.write_text('{"title":"Invoice"}')

    lock = record_green(
        ws, "acme", "list-invoices",
        schema_name="invoice", version="0.1.0", flow_path=flow, schema_path=schema,
    )
    entry = lock["portals"]["acme"]["flows"]["list-invoices"]
    assert entry["schema"] == "invoice"
    assert entry["flowSha256"] == sha256_file(flow)
    assert entry["schemaSha256"] == sha256_file(schema)
    assert RFC3339.match(entry["lastGreenAt"])
    assert json.loads(ws.lockfile_path.read_text()) == lock


def test_recording_again_after_an_edit_moves_the_hash(tmp_path: Path):
    ws = Workspace(tmp_path)
    flow = tmp_path / "f.json"
    schema = tmp_path / "s.json"
    flow.write_text("{}")
    schema.write_text("{}")
    args = dict(schema_name="invoice", version="0.1.0", flow_path=flow, schema_path=schema)
    first = record_green(ws, "acme", "list-invoices", **args)["portals"]["acme"]["flows"]["list-invoices"]["flowSha256"]

    flow.write_text('{"changed":true}')
    second = record_green(ws, "acme", "list-invoices", **args)["portals"]["acme"]["flows"]["list-invoices"]["flowSha256"]
    assert first != second
    assert second == sha256_file(flow)


def test_two_portals_and_two_flows_live_side_by_side(tmp_path: Path):
    ws = Workspace(tmp_path)
    f = tmp_path / "f.json"
    f.write_text("{}")
    for portal, flow in [("acme", "list-invoices"), ("acme", "list-berths"), ("beta", "list-invoices")]:
        record_green(ws, portal, flow, schema_name="x", version="0.1.0", flow_path=f, schema_path=f)
    lock = load_lock(ws)
    assert sorted(lock["portals"]) == ["acme", "beta"]
    assert sorted(lock["portals"]["acme"]["flows"]) == ["list-berths", "list-invoices"]


# -- the lockfile as the pipeline maintains it ---------------------------


def test_explore_records_the_compiled_flow(compiled: Workspace):
    entry = load_lock(compiled)["portals"]["demo-marina"]
    assert entry["version"] == "0.1.0"
    flow = entry["flows"]["list-invoices"]
    assert flow["schema"] == "invoice"
    assert flow["flowSha256"] == sha256_file(compiled.root / "portals/demo-marina/flows/list-invoices.flow.json")
    assert flow["schemaSha256"] == sha256_file(compiled.root / "portals/demo-marina/schemas/invoice.schema.json")


def test_a_green_verify_leaves_the_lockfile_matching_the_files_on_disk(compiled: Workspace):
    verify(compiled, "demo-marina", "list-invoices")
    flow = load_lock(compiled)["portals"]["demo-marina"]["flows"]["list-invoices"]
    assert flow["flowSha256"] == sha256_file(compiled.root / "portals/demo-marina/flows/list-invoices.flow.json")
    assert RFC3339.match(flow["lastGreenAt"])


def test_a_repair_is_a_patch_bump_and_rehashes_both_artifacts(compiled: Workspace, mutate):
    before = load_lock(compiled)["portals"]["demo-marina"]["flows"]["list-invoices"]
    mutate("rename-total")
    repair(compiled, "demo-marina", "list-invoices")

    entry = load_lock(compiled)["portals"]["demo-marina"]
    assert entry["version"] == "0.1.1"
    after = entry["flows"]["list-invoices"]
    assert after["flowSha256"] != before["flowSha256"]
    assert after["schemaSha256"] != before["schemaSha256"]
    assert after["flowSha256"] == sha256_file(compiled.root / "portals/demo-marina/flows/list-invoices.flow.json")


def test_the_generated_package_version_follows_the_lockfile(compiled: Workspace, mutate):
    assert build(compiled, "demo-marina")["version"] == "0.1.0"
    mutate("rename-total")
    repair(compiled, "demo-marina", "list-invoices")
    pkg = Path(build(compiled, "demo-marina")["package"])
    assert json.loads((pkg / "package.json").read_text())["version"] == "0.1.1"
