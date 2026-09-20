"""The five-minute onboarding in the README, run end to end through the CLI.

Every command here is copied from the README. If this file goes red, the first
thing a new user does with portalc is broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DEMO_PASS, DEMO_USER, MUTATE

PORTAL = "demo-marina"
FLOW = "list-invoices"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=portalc test", "-c", "user.email=test@portalc.invalid", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout


@pytest.fixture
def onboarding(cli, tmp_path: Path, demo_portal: str, mutate):
    """A bare directory plus a running demo portal — where the README starts."""
    assert cli("init", "--demo", "--base-url", demo_portal, cwd=tmp_path).code == 0
    return tmp_path


def test_the_readme_walkthrough_ends_green(cli, tmp_path: Path, demo_portal: str, mutate):
    assert cli("doctor", cwd=tmp_path).code == 0
    assert cli("init", "--demo", "--base-url", demo_portal, cwd=tmp_path).code == 0

    explored = cli("explore", "--yes", cwd=tmp_path)
    assert explored.code == 0
    assert "2/2 identical" in explored.stdout

    assert cli("verify", cwd=tmp_path).code == 0
    assert cli("build", PORTAL, cwd=tmp_path).code == 0

    rows = cli("run", PORTAL, FLOW, "--json", cwd=tmp_path).json()
    assert len(rows) >= 6

    subprocess.check_call([sys.executable, str(MUTATE), "rename-total"])

    drifted = cli("verify", cwd=tmp_path)
    assert drifted.code == 8
    assert "Total (EUR)" in drifted.output and "Amount (EUR)" in drifted.output

    repaired = cli("repair", PORTAL, cwd=tmp_path)
    assert repaired.code == 0
    assert "confirmations 2/2" in repaired.stdout

    assert cli("verify", cwd=tmp_path).code == 0
    assert cli("run", PORTAL, FLOW, "--json", cwd=tmp_path).json() == rows


def test_the_drift_and_the_patch_are_an_ordinary_reviewable_diff(cli, onboarding: Path):
    root = onboarding
    (root / ".gitignore").write_text(".env\n")
    assert cli("explore", "--yes", cwd=root).code == 0
    assert cli("build", PORTAL, cwd=root).code == 0
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "compile demo-marina")

    subprocess.check_call([sys.executable, str(MUTATE), "rename-total"])
    assert cli("repair", PORTAL, cwd=root).code == 0

    changed = git(root, "diff", "--name-only").split()
    assert f"portals/{PORTAL}/schemas/invoice.schema.json" in changed
    assert f"portals/{PORTAL}/flows/{FLOW}.flow.json" in changed
    assert f"portals/{PORTAL}/CHANGELOG.md" in changed
    assert "portalc.lock" in changed

    schema_diff = git(root, "diff", "--", f"portals/{PORTAL}/schemas/invoice.schema.json").splitlines()
    removed = [l for l in schema_diff if l.startswith("-") and not l.startswith("---")]
    added = [l for l in schema_diff if l.startswith("+") and not l.startswith("+++")]
    assert len(removed) == len(added) == 1
    assert "Total (EUR)" in removed[0]
    assert "Amount (EUR)" in added[0]


def test_the_typed_client_is_regenerated_with_the_patched_version(cli, onboarding: Path):
    root = onboarding
    cli("explore", "--yes", cwd=root)
    cli("build", PORTAL, cwd=root)
    package = root / "generated" / "ts" / f"portal-{PORTAL}"
    assert json.loads((package / "package.json").read_text())["version"] == "0.1.0"

    subprocess.check_call([sys.executable, str(MUTATE), "rename-total"])
    cli("repair", PORTAL, cwd=root)
    assert json.loads((package / "package.json").read_text())["version"] == "0.1.1"
    assert "  total_eur: number;" in (package / "types.ts").read_text()


def test_the_compiled_artifacts_never_contain_a_credential(cli, onboarding: Path):
    root = onboarding
    cli("explore", "--yes", cwd=root)
    cli("build", PORTAL, cwd=root)

    committable = [
        *(root / "portals").rglob("*"),
        *(root / "generated").rglob("*"),
        root / "portalc.lock",
    ]
    for path in committable:
        if not path.is_file():
            continue
        body = path.read_text(errors="replace")
        assert DEMO_PASS not in body, f"{path} contains a password"
        assert "marina_session" not in body, f"{path} contains a session cookie"


def test_portal_yaml_names_the_environment_variables_instead_of_the_values(onboarding: Path):
    body = (onboarding / "portals" / PORTAL / "portal.yaml").read_text()
    assert "username: DEMO_MARINA_USER" in body
    assert "password: DEMO_MARINA_PASS" in body
    assert DEMO_PASS not in body
    assert DEMO_USER not in body


def test_init_scaffolds_the_ci_job_that_catches_drift_overnight(onboarding: Path):
    workflow = (onboarding / ".github" / "workflows" / "portalc.yml").read_text()
    assert "schedule:" in workflow
    assert "portalc verify --all" in workflow
    assert "DEMO_MARINA_PASS" in workflow


def test_the_evidence_file_keeps_the_page_that_justified_the_schema(cli, onboarding: Path):
    cli("explore", "--yes", cwd=onboarding)
    evidence = next((onboarding / "portals" / PORTAL / "evidence").glob(f"*.{FLOW}.html"))
    body = evidence.read_text()
    assert body.startswith("<table")
    assert "Total (EUR)" in body
    assert "<style>" not in body


def test_the_fixture_is_the_rows_as_compiled(cli, onboarding: Path):
    cli("explore", "--yes", cwd=onboarding)
    fixture = next((onboarding / "portals" / PORTAL / "fixtures").glob(f"*.{FLOW}.json"))
    rows = json.loads(fixture.read_text())
    assert rows == cli("run", PORTAL, FLOW, "--json", cwd=onboarding).json()
