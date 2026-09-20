from __future__ import annotations

import json
from pathlib import Path

import pytest

from portalc.envfile import load_env_file, load_workspace_env
from portalc.errors import ConfigError, PortalAuthError
from portalc.workspace import (
    API_VERSION,
    PortalConfig,
    Workspace,
    env_names,
    expand_env,
    read_json,
    write_json,
)

PORTAL_YAML = """\
apiVersion: portalc/v1
name: acme
title: Acme Partner Portal
baseUrl: ${ACME_BASE_URL:-https://portal.acme.test}
auth:
  mode: form
  loginPath: /sign-in
  secrets:
    username: ACME_USER
    password: ACME_PASS
policy:
  readOnly: true
  allowedDomains:
    - portal.acme.test
flows:
  - list-invoices
"""


def write_portal(root: Path, body: str = PORTAL_YAML, name: str = "acme") -> Path:
    path = root / "portals" / name / "portal.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# -- env expansion -------------------------------------------------------


def test_expand_env_substitutes_a_named_variable():
    assert expand_env("${HOST}/x", {"HOST": "https://a.test"}) == "https://a.test/x"


def test_expand_env_falls_back_to_the_inline_default():
    assert expand_env("${HOST:-https://b.test}", {}) == "https://b.test"


def test_a_variable_with_no_value_and_no_default_is_a_config_error():
    with pytest.raises(ConfigError) as caught:
        expand_env("${NOPE}", {})
    assert "NOPE" in caught.value.title
    assert caught.value.exit_code == 2


def test_env_names_lists_what_a_portal_needs_without_reading_the_values():
    assert env_names("${A}/${B:-x}") == ["A", "B"]


# -- portal.yaml ---------------------------------------------------------


def test_portal_config_reads_auth_policy_and_flows(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ACME_BASE_URL", raising=False)
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    assert cfg.name == "acme"
    assert cfg.base_url == "https://portal.acme.test"
    assert cfg.auth_mode == "form"
    assert cfg.read_only is True
    assert cfg.allowed_domains == ["portal.acme.test"]
    assert cfg.flows == ["list-invoices"]


def test_base_url_comes_from_the_environment_at_call_time(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ACME_BASE_URL", "http://127.0.0.1:8787/")
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    assert cfg.base_url == "http://127.0.0.1:8787"


def test_portal_yaml_stores_secret_names_never_secret_values(tmp_path: Path):
    path = write_portal(tmp_path)
    cfg = PortalConfig.from_file(path)
    assert {s.key: s.env for s in cfg.secrets} == {"username": "ACME_USER", "password": "ACME_PASS"}
    assert "hunter2" not in path.read_text()


def test_resolve_secrets_reads_the_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ACME_USER", "ops@acme.test")
    monkeypatch.setenv("ACME_PASS", "hunter2")
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    assert cfg.resolve_secrets() == {"username": "ops@acme.test", "password": "hunter2"}


def test_missing_credentials_are_an_auth_error_that_names_the_variable(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ACME_USER", raising=False)
    monkeypatch.delenv("ACME_PASS", raising=False)
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    with pytest.raises(PortalAuthError) as caught:
        cfg.resolve_secrets()
    assert caught.value.exit_code == 3
    assert "ACME_USER" in " ".join(caught.value.detail)


def test_an_unknown_apiversion_is_refused(tmp_path: Path):
    body = PORTAL_YAML.replace(API_VERSION, "portalc/v99")
    with pytest.raises(ConfigError) as caught:
        PortalConfig.from_file(write_portal(tmp_path, body))
    assert "portalc/v99" in caught.value.title


def test_a_portal_without_a_base_url_is_refused(tmp_path: Path):
    body = "\n".join(l for l in PORTAL_YAML.splitlines() if not l.startswith("baseUrl"))
    with pytest.raises(ConfigError) as caught:
        PortalConfig.from_file(write_portal(tmp_path, body))
    assert "baseUrl" in caught.value.title


def test_broken_yaml_is_reported_as_config_not_as_a_traceback(tmp_path: Path):
    with pytest.raises(ConfigError):
        PortalConfig.from_file(write_portal(tmp_path, "apiVersion: [unclosed\n"))


def test_loading_an_unknown_portal_lists_the_known_ones(tmp_path: Path):
    write_portal(tmp_path)
    with pytest.raises(ConfigError) as caught:
        PortalConfig.load(Workspace(tmp_path), "missing")
    assert "acme" in " ".join(caught.value.detail)
    assert caught.value.hint


def test_portal_paths_follow_the_documented_layout(tmp_path: Path):
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    root = tmp_path / "portals" / "acme"
    assert cfg.flow_path("list-invoices") == root / "flows" / "list-invoices.flow.json"
    assert cfg.checks_path("list-invoices") == root / "flows" / "list-invoices.checks.json"
    assert cfg.schema_path("invoice") == root / "schemas" / "invoice.schema.json"
    assert cfg.fixtures_dir() == root / "fixtures"
    assert cfg.evidence_dir() == root / "evidence"


def test_latest_fixture_picks_the_newest_dated_file(tmp_path: Path):
    cfg = PortalConfig.from_file(write_portal(tmp_path))
    cfg.fixtures_dir().mkdir(parents=True)
    for day in ("2026-09-18", "2026-09-20", "2026-09-19"):
        (cfg.fixtures_dir() / f"{day}.list-invoices.json").write_text("[]")
    assert cfg.latest_fixture("list-invoices").name == "2026-09-20.list-invoices.json"
    assert cfg.latest_fixture("other") is None


# -- workspace discovery -------------------------------------------------


def test_discover_walks_up_to_the_lockfile(tmp_path: Path):
    (tmp_path / "portalc.lock").write_text("{}")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert Workspace.discover(deep).root == tmp_path


def test_discover_accepts_a_portals_directory_as_the_root(tmp_path: Path):
    (tmp_path / "portals").mkdir()
    assert Workspace.discover(tmp_path / "portals").root == tmp_path


def test_list_portals_only_counts_directories_with_a_portal_yaml(tmp_path: Path):
    write_portal(tmp_path, name="acme")
    write_portal(tmp_path, PORTAL_YAML.replace("acme", "beta"), name="beta")
    (tmp_path / "portals" / "scratch").mkdir()
    assert Workspace(tmp_path).list_portals() == ["acme", "beta"]


def test_a_workspace_with_no_portals_lists_nothing(tmp_path: Path):
    assert Workspace(tmp_path).list_portals() == []


# -- json helpers --------------------------------------------------------


def test_write_json_creates_parents_and_ends_with_a_newline(tmp_path: Path):
    path = tmp_path / "deep" / "x.json"
    write_json(path, {"a": 1})
    assert path.read_text().endswith("}\n")
    assert read_json(path) == {"a": 1}


def test_reading_a_missing_file_says_what_was_expected(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        read_json(tmp_path / "nope.json", "flow")
    assert "flow" in caught.value.title


def test_reading_invalid_json_is_a_config_error(tmp_path: Path):
    path = tmp_path / "x.json"
    path.write_text("{oops")
    with pytest.raises(ConfigError):
        read_json(path)


def test_write_json_keeps_non_ascii_readable(tmp_path: Path):
    path = tmp_path / "x.json"
    write_json(path, {"vessel": "Gulet Anima Maris · Šibenik"})
    assert "Šibenik" in path.read_text()
    assert json.loads(path.read_text())["vessel"].endswith("Šibenik")


# -- .env ----------------------------------------------------------------


def test_env_file_values_are_loaded_into_the_process(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PORTALC_TEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text("# comment\n\nPORTALC_TEST_TOKEN = 'abc123'\n")
    load_workspace_env(tmp_path)
    import os

    assert os.environ["PORTALC_TEST_TOKEN"] == "abc123"


def test_an_env_file_never_overrides_what_the_shell_already_set(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PORTALC_TEST_TOKEN", "from-shell")
    (tmp_path / ".env").write_text("PORTALC_TEST_TOKEN=from-file\n")
    load_env_file(tmp_path / ".env")
    import os

    assert os.environ["PORTALC_TEST_TOKEN"] == "from-shell"
    load_env_file(tmp_path / ".env", override=True)
    assert os.environ["PORTALC_TEST_TOKEN"] == "from-file"


def test_a_missing_env_file_is_not_an_error(tmp_path: Path):
    load_workspace_env(tmp_path / "nowhere")
