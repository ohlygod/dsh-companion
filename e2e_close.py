"""Shutdown-path test: chat, then close stdin, and check for a clean exit.

Node's process.exit() kills the child mid-flight, which is not how DSH disposes a
plugin. This script closes the pipe the polite way and prints whatever the
bridge says while shutting down.

Usage:
    python e2e_close.py [python-executable]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.argv[1] if len(sys.argv) > 1 else sys.executable
BRIDGE = HERE / "bridge.py"
DATA = HERE / ".close-data"

REPLY = json.dumps({
    "action": "text",
    "text": "在的，一直都在。",
    "claims": [{"id": "c1", "kind": "subjective", "statement": "在的", "evidence": None}],
}, ensure_ascii=False)

proc = subprocess.Popen(
    [PY, str(BRIDGE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
    env={**os.environ,
         "DSH_COMPANION_DATA": str(DATA),
         "DSH_COMPANION_USER": "owner", "DSH_COMPANION_DEBUG": "1",
         "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)

proc.stdin.write(json.dumps({"id": "c1", "cmd": "chat", "text": "在吗", "conversation": "main",
                             "prompt": "你是 YG 的私人陪伴角色。"}, ensure_ascii=False) + "\n")
proc.stdin.flush()

got = None
for line in proc.stdout:
    line = line.strip()
    if not line:
        continue
    payload = json.loads(line)
    if payload.get("kind") == "model":
        proc.stdin.write(json.dumps({"kind": "model", "rid": payload["rid"], "text": REPLY},
                                    ensure_ascii=False) + "\n")
        proc.stdin.flush()
        continue
    if payload.get("kind") == "reply":
        got = payload["text"]
        continue
    if payload.get("id") == "c1":
        break

print("reply:", got)
proc.stdin.close()
code = proc.wait(timeout=60)
tail = proc.stderr.read()
print("exit code:", code)
print("--- stderr tail ---")
print("\n".join(tail.splitlines()[-25:]) if tail else "(empty)")
clean = code == 0 and "Traceback" not in tail
print("CLEAN SHUTDOWN" if clean else "SHUTDOWN ISSUE")
sys.exit(0 if clean else 1)
