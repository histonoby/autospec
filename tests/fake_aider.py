#!/usr/bin/env python3
"""Stand-in for the aider binary (§20): writes fixed content per call, driven by $FAKE_AIDER_PLAN.

Plan JSON: {"calls": [{"write": "<content>" | null, "exit": 0, "stdout": "..."}], "counter": "<path>",
            "argv_log": "<path>", "tamper": "<relative test path to modify>" (optional, per call)}
The editable files are the positional arguments after the --message value.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    plan = json.loads(Path(os.environ["FAKE_AIDER_PLAN"]).read_text(encoding="utf-8"))
    counter = Path(plan["counter"])
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    argv = sys.argv[1:]
    with open(plan["argv_log"], "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")
    calls = plan["calls"]
    call = calls[min(n, len(calls) - 1)]
    idx = argv.index("--message")
    editable = argv[idx + 2:]
    print(f"fake-aider call {n + 1} editable={editable}")
    Path(".aider.tags.cache.v4").mkdir(exist_ok=True)  # like the real aider's repo-map cache
    if call.get("stdout"):
        print(call["stdout"])
    if call.get("write") is not None and editable:
        Path(editable[0]).write_text(call["write"], encoding="utf-8")
    if call.get("tamper"):
        target = Path(call["tamper"])
        target.chmod(0o644)
        target.write_text(target.read_text() + "\n// tampered\n", encoding="utf-8")
    return int(call.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main())
