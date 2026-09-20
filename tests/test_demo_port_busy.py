from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_busy_port_is_a_named_error_not_a_traceback():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        proc = subprocess.run(
            [sys.executable, "-m", "portalc", "demo", "--port", str(port)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 5, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "already in use" in combined
    assert "Traceback" not in combined
    assert "OSError" not in combined
