"""`portalc doctor` has one job: say whether this machine can finish the demo."""

from __future__ import annotations

from pathlib import Path

import pytest

from portalc.errors import ConfigError
from portalc.pipeline import doctor
from portalc.workspace import Workspace


def by_check(ws: Workspace) -> dict:
    return {f.check: f for f in doctor(ws)}


def test_doctor_reports_on_every_prerequisite(tmp_path: Path):
    assert set(by_check(Workspace(tmp_path))) == {
        "python", "jsonschema", "chrome", "browser-use", "node", "demo", "workspace",
    }


def test_a_machine_that_can_run_portalc_has_no_failures(tmp_path: Path):
    assert [f.check for f in doctor(Workspace(tmp_path)) if not f.ok] == []


def test_python_and_jsonschema_are_the_hard_requirements(tmp_path: Path):
    findings = by_check(Workspace(tmp_path))
    assert "needs >=3.10" in findings["python"].message
    assert findings["jsonschema"].message == "installed"


def test_a_missing_browser_is_a_warning_not_a_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("portalc.pipeline.shutil.which", lambda name: None)
    findings = by_check(Workspace(tmp_path))
    for check in ("chrome", "node"):
        assert findings[check].ok is True
        assert findings[check].severity == "warn"
    assert "HTTP engine does not need it" in findings["chrome"].message


def test_browser_use_is_optional_even_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "browser_use", None)
    finding = by_check(Workspace(tmp_path))["browser-use"]
    assert finding.ok is True
    assert finding.severity in {"info", "error"}


def test_a_checkout_without_the_bundled_demo_is_a_real_failure(tmp_path: Path, monkeypatch):
    def missing():
        raise ConfigError("bundled demo portal is not on disk")

    monkeypatch.setattr("portalc.pipeline.demo_server_script", missing)
    finding = by_check(Workspace(tmp_path))["demo"]
    assert finding.ok is False
    assert "demo/portal/server.py" in finding.message


def test_doctor_finds_the_bundled_demo_server(tmp_path: Path):
    assert by_check(Workspace(tmp_path))["demo"].message.endswith("demo/portal/server.py")


def test_the_workspace_line_names_the_root_and_its_portals(tmp_path: Path):
    assert "(none)" in by_check(Workspace(tmp_path))["workspace"].message


def test_the_workspace_line_lists_a_compiled_portal(compiled: Workspace):
    message = by_check(compiled)["workspace"].message
    assert str(compiled.root) in message
    assert "demo-marina" in message


def test_doctor_needs_no_portal_and_no_network(tmp_path: Path):
    """It is the first command in the README, so it must work before anything is running."""
    assert doctor(Workspace(tmp_path / "does-not-exist"))
