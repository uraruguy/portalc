from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str) -> None:
    import urllib.request

    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=0.3) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"demo portal did not start at {url}")


@pytest.fixture
def demo_portal() -> Iterator[str]:
    mutate = ROOT / "demo" / "mutate.py"
    subprocess.check_call([sys.executable, str(mutate), "reset"])
    port = _free_port()
    script = ROOT / "demo" / "portal" / "server.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait(url + "/healthz")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.check_call([sys.executable, str(mutate), "reset"])


@pytest.fixture
def workspace(tmp_path: Path, demo_portal: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MARINA_USER", "skipper@demo-marina.test")
    monkeypatch.setenv("DEMO_MARINA_PASS", "demo-portal-password")
    os.environ.pop("PORTALC_QUIET", None)
    monkeypatch.setenv("PORTALC_QUIET", "1")
    from portalc.pipeline import init_demo
    from portalc.workspace import Workspace

    init_demo(Workspace(tmp_path), base_url=demo_portal, force=True)
    return tmp_path
