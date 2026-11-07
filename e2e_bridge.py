"""End-to-end bridge test: this script plays the DSH(Node) side.

It spawns bridge.py, answers the core's reverse model requests with a canned
JSON reply, and asserts the model's own text reaches the reply.

Usage:
    python e2e_bridge.py [python-executable]
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.argv[1] if len(sys.argv) > 1 else sys.executable
BRIDGE = HERE / "bridge.py"
DATA = HERE / ".e2e-data"

REPLY = json.dumps({
    "action": "text",
    "text": "在的，一直都在。",
    "claims": [{"id": "c1", "kind": "subjective", "statement": "在的", "evidence": None}],
}, ensure_ascii=False)

proc = subprocess.Popen(
    [PY, str(BRIDGE)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
    env={**os.environ,
         "DSH_COMPANION_DATA": str(DATA),
         "DSH_COMPANION_USER": "owner",
         "DSH_COMPANION_DEBUG": "1",
         "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)

model_calls = []
replies = []


def pump_stderr():
    for line in proc.stderr:
        print("[py-stderr]", line.rstrip())


threading.Thread(target=pump_stderr, daemon=True).start()


def send(payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


send({"id": "c1", "cmd": "chat", "text": "在吗", "conversation": "main",
      "prompt": "你是 YG 的私人陪伴角色，说话自然。"})

for line in proc.stdout:
    line = line.strip()
    if not line:
        continue
    print("[py]", line[:160])
    payload = json.loads(line)
    if payload.get("kind") == "ready":
        continue
    if payload.get("kind") == "model":
        model_calls.append(payload)
        send({"kind": "model", "rid": payload["rid"], "text": REPLY})
        continue
    if payload.get("kind") == "reply":
        replies.append(payload["text"])
        continue
    if payload.get("id") == "c1":
        print("=== chat result:", json.dumps(payload, ensure_ascii=False)[:400])
        break

proc.stdin.close()
proc.wait(timeout=30)
print("=== model calls:", len(model_calls))
if model_calls:
    print("=== system head:", model_calls[0]["system"][:120].replace("\n", " | "))
    print("=== user head:", model_calls[0]["user"][:160].replace("\n", " | "))
print("=== replies:", replies)
good = bool(replies) and "在的" in replies[0]
print("PASS" if good else "FAIL: reply was not the model's own text")
sys.exit(0 if good else 1)
