"""Where portalc artifacts live, and how portal.yaml is read.

Layout (this is the product: the integration is a reviewable part of your repo)

    portals/<portal>/portal.yaml
    portals/<portal>/flows/<flow>.flow.json
    portals/<portal>/flows/<flow>.checks.json
    portals/<portal>/schemas/<schema>.schema.json
    portals/<portal>/fixtures/<date>.<flow>.json
    portals/<portal>/evidence/<date>.<flow>.html|.txt
    portals/<portal>/CHANGELOG.md
    generated/ts/portal-<portal>/
    portalc.lock
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .policy import SECRETS, RequestPolicy, assert_no_secrets

API_VERSION = "portalc/v1"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(
    value: str,
    env: dict[str, str] | None = None,
    *,
    secret_names: Sequence[str] = (),
) -> str:
    """Expand ${VAR} and ${VAR:-default}.

    Expansion happens in memory, at call time. Anything expanded from a
    variable the portal declares under `auth.secrets` is registered with
    `policy.SECRETS`, which is what makes `write_json` able to refuse writing
    it back out: the raw `${VAR}` form is the only form that reaches disk.
    """
    source = env if env is not None else os.environ
    secret = {n for n in secret_names}

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in source:
            if name in secret:
                SECRETS.register(source[name])
            return source[name]
        if default is not None:
            return default
        raise ConfigError(
            f"environment variable {name} is not set",
            f"portal.yaml references ${{{name}}} but nothing in the environment provides it.",
            hint=f"export {name}=… or add it to your .env (never commit the value)",
        )

    return _ENV_RE.sub(repl, value)


def env_names(value: str) -> list[str]:
    return [m.group(1) for m in _ENV_RE.finditer(value)]


@dataclass
class Workspace:
    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "Workspace":
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / "portalc.lock").exists() or (candidate / "portals").is_dir():
                return cls(candidate)
            if (candidate / ".git").exists():
                return cls(candidate)
        return cls(cur)

    # -- paths -----------------------------------------------------------
    @property
    def portals_dir(self) -> Path:
        return self.root / "portals"

    @property
    def generated_dir(self) -> Path:
        return self.root / "generated"

    @property
    def lockfile_path(self) -> Path:
        return self.root / "portalc.lock"

    @property
    def state_dir(self) -> Path:
        return self.root / ".portalc"

    def portal_dir(self, name: str) -> Path:
        return self.portals_dir / name

    def list_portals(self) -> list[str]:
        if not self.portals_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.portals_dir.iterdir() if (p / "portal.yaml").is_file()
        )


@dataclass
class SecretRef:
    """A secret portalc knows the *name* of and never the value."""

    key: str
    env: str


@dataclass
class PortalConfig:
    name: str
    title: str
    base_url_raw: str
    auth: dict[str, Any]
    policy: dict[str, Any]
    flows: list[str]
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def base_url(self) -> str:
        return expand_env(self.base_url_raw, secret_names=self.secret_env_names).rstrip("/")

    @property
    def auth_mode(self) -> str:
        return self.auth.get("mode", "human-session")

    @property
    def secrets(self) -> list[SecretRef]:
        return [SecretRef(k, v) for k, v in (self.auth.get("secrets") or {}).items()]

    @property
    def secret_env_names(self) -> list[str]:
        return [ref.env for ref in self.secrets]

    @property
    def login_path(self) -> str:
        return str(self.auth.get("loginPath") or "/login")

    @property
    def allowed_domains(self) -> list[str]:
        return list(self.policy.get("allowedDomains") or [])

    @property
    def read_only(self) -> bool:
        return bool(self.policy.get("readOnly", True))

    @property
    def request_policy(self) -> RequestPolicy:
        """The parsed `policy:` block the engine enforces on every request."""
        return RequestPolicy.from_policy(
            self.policy,
            base_url=self.base_url,
            login_path=self.login_path,
        )

    def flow_path(self, flow: str) -> Path:
        return self.dir / "flows" / f"{flow}.flow.json"

    def checks_path(self, flow: str) -> Path:
        return self.dir / "flows" / f"{flow}.checks.json"

    def schema_path(self, schema: str) -> Path:
        return self.dir / "schemas" / f"{schema}.schema.json"

    def fixtures_dir(self) -> Path:
        return self.dir / "fixtures"

    def evidence_dir(self) -> Path:
        return self.dir / "evidence"

    def latest_fixture(self, flow: str) -> Path | None:
        d = self.fixtures_dir()
        if not d.is_dir():
            return None
        matches = sorted(d.glob(f"*.{flow}.json"))
        return matches[-1] if matches else None

    @classmethod
    def load(cls, workspace: Workspace, name: str) -> "PortalConfig":
        path = workspace.portal_dir(name) / "portal.yaml"
        if not path.is_file():
            known = workspace.list_portals()
            listing = ", ".join(known) if known else "none yet"
            raise ConfigError(
                f"portal {name!r} is not configured",
                [f"expected {path}", f"portals in this workspace: {listing}"],
                hint="check the name against `portals/`; v0.1 scaffolds the bundled demo with `portalc init --demo`",
            )
        return cls.from_file(path)

    @classmethod
    def from_file(cls, path: Path) -> "PortalConfig":
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML", str(exc)) from exc
        if doc.get("apiVersion") != API_VERSION:
            raise ConfigError(
                f"{path} has apiVersion {doc.get('apiVersion')!r}",
                f"portalc understands {API_VERSION}.",
                hint=f"set apiVersion: {API_VERSION}",
            )
        for required in ("name", "baseUrl"):
            if not doc.get(required):
                raise ConfigError(f"{path} is missing `{required}`")
        return cls(
            name=doc["name"],
            title=doc.get("title", doc["name"]),
            base_url_raw=str(doc["baseUrl"]),
            auth=doc.get("auth") or {},
            policy=doc.get("policy") or {},
            flows=list(doc.get("flows") or []),
            path=path,
            raw=doc,
        )

    def resolve_secrets(self) -> dict[str, str]:
        """Read secret values from the environment at call time. Never persisted."""
        from .errors import PortalAuthError

        out: dict[str, str] = {}
        missing: list[str] = []
        for ref in self.secrets:
            value = os.environ.get(ref.env)
            if value is None:
                missing.append(ref.env)
            else:
                out[ref.key] = value
                SECRETS.register(value)
        if missing:
            raise PortalAuthError(
                "portal credentials are not in the environment",
                [f"missing: {', '.join(missing)}"],
                hint="export them in your shell or CI secrets; portalc stores names, never values",
            )
        return out


def read_json(path: Path, what: str = "file") -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{what} not found", f"expected {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON", str(exc)) from exc


def write_json(path: Path, payload: Any) -> None:
    """Write a portalc artifact — after checking it holds no credential value.

    Every artifact portalc commits (flow, schema, checks, fixture, lockfile)
    goes through here, so this is the one place the no-secret guarantee has to
    hold.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert_no_secrets(text, where=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
