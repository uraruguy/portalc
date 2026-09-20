#!/usr/bin/env python3
"""Change the demo portal the way a real portal changes overnight.

    python3 demo/mutate.py rename-total   # "Total (EUR)" -> "Amount (EUR)"
    python3 demo/mutate.py reset          # put it back
    python3 demo/mutate.py status

The mutation is a pure presentation change: same data, same position, new
column header. No HTTP status changes, nothing throws. That is exactly the
failure mode portalc is built to catch — a portal that keeps returning 200
while quietly meaning something different.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / "portal" / ".state.json"

MUTATIONS = {
    "rename-total": {
        "describe": '"Total (EUR)" column header renamed to "Amount (EUR)"',
        "columnLabels": {"total_eur": "Amount (EUR)"},
    },
    "rename-issued": {
        "describe": '"Issued" column header renamed to "Issue date"',
        "columnLabels": {"issued_on": "Issue date"},
    },
}


def read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"columnLabels": {}, "mutations": []}


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    state = read_state()

    if action == "status":
        active = state.get("mutations", [])
        if not active:
            print("demo portal: unmutated (original column headers)")
        else:
            for name in active:
                print(f"demo portal: {name} — {MUTATIONS[name]['describe']}")
        return 0

    if action == "reset":
        write_state({"columnLabels": {}, "mutations": []})
        print("demo portal reset to original column headers")
        return 0

    if action not in MUTATIONS:
        print(f"unknown mutation {action!r}; available: {', '.join(MUTATIONS)}, reset, status")
        return 2

    mutation = MUTATIONS[action]
    state.setdefault("columnLabels", {}).update(mutation["columnLabels"])
    mutations = state.setdefault("mutations", [])
    if action not in mutations:
        mutations.append(action)
    write_state(state)
    print(f"demo portal mutated: {mutation['describe']}")
    print("the portal still returns 200 and still renders every row — nothing looks broken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
