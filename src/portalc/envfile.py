"""Minimal .env loader. Values stay in the process environment, never in artifacts."""

from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path, *, override: bool = False) -> None:
    import os

    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def load_workspace_env(root: Path) -> None:
    load_env_file(root / ".env")
