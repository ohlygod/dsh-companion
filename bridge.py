"""Companion bridge: Python side of the DSH plugin.

Protocol (both directions are one JSON object per line, UTF-8):

  Node -> Python   {"id":"1","cmd":"chat","text":"在吗","conversation":"main","prompt":"..."}
  Python -> Node   {"id":"1","ok":true,"replies":["..."]}

  Python -> Node   {"kind":"model","rid":"m1","system":"...","user":"..."}
  Node -> Python   {"kind":"model","rid":"m1","text":"{...json reply...}"}

The core (companion_v2) is untouched: this file only supplies the ports the
core says it trusts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Optional local deps (regex, lunardate). They are NOT shipped in this repo:
# pip install them into the interpreter you point the plugin at. The core falls
# back to its own paths when they are absent, but segmentation needs `regex`.
_VENDOR = ROOT / ".vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

from companion_v2.adapters.astrbot.application import AstrBotCompanionHost  # noqa: E402
from companion_v2.configuration.registry import FieldRegistry  # noqa: E402
from companion_v2.conversation.flow import ConversationPolicy  # noqa: E402
from companion_v2.conversation.messages import REPLY_KIND  # noqa: E402
from companion_v2.conversation.reply_settings import configuration_fields as reply_fields  # noqa: E402
from companion_v2.conversation.segmentation import SegmentationPolicy  # noqa: E402
from companion_v2.delivery.models import LocalSender  # noqa: E402
from companion_v2.execution.models import Lane, ProviderBinding, SupervisorLimits  # noqa: E402
from companion_v2.identity_policy.models import AuthorizationTier  # noqa: E402
from companion_v2.foundation.refs import SubjectKind, SubjectRef  # noqa: E402
from companion_v2.identity_policy.object_access import ObjectAccessPolicy  # noqa: E402
from companion_v2.identity_policy.personas import (  # noqa: E402
    ConversationRoute, PersonaBinding, PersonaDirectory, PersonaSelector, RuntimeIdentity, SelectorKind,
)
from companion_v2.identity_policy.repository import PersistentIdentityDirectory  # noqa: E402
from companion_v2.model_gateway.models import Amount, BudgetPolicy, ModelTaskProfile  # noqa: E402
from companion_v2.model_gateway.routing import configuration_fields  # noqa: E402
from companion_v2.storage.runtime import StorageRuntime  # noqa: E402

from dsh_host.ports import DshEventPort, build_route  # noqa: E402
from dsh_host.provider import make_provider  # noqa: E402

INSTALLATION = "dsh-companion"
TASKS = ("text", "image", "voice", "video")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Bridge:
    def __init__(self, data_root: Path, user_id: str) -> None:
        self.data_root = data_root
        self.user_id = user_id
        self.runtime: StorageRuntime | None = None
        self.hosts: dict[str, AstrBotCompanionHost] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.write_lock = asyncio.Lock()
        self._seq = 0

    # ---------- transport ----------
    async def emit(self, payload: dict) -> None:
        async with self.write_lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def ask_model(self, system: str, user: str, max_tokens: int) -> str:
        self._seq += 1
        rid = "m%d" % self._seq
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[rid] = future
        await self.emit({"kind": "model", "rid": rid, "system": system, "user": user,
                         "max_tokens": max_tokens})
        try:
            return await asyncio.wait_for(future, timeout=180)
        finally:
            self.pending.pop(rid, None)

    def resolve_model(self, rid: str, text: str) -> None:
        """Called from the reader thread once the host answers a model request."""
        future = self.pending.get(rid)
        if future is not None and not future.done():
            future.set_result(text)
    def open_runtime(self) -> StorageRuntime:
        if self.runtime is None:
            self.data_root.mkdir(parents=True, exist_ok=True)
            self.runtime = StorageRuntime.open(data_root=self.data_root, installation_id=INSTALLATION)
        return self.runtime

    async def host_for(self, conversation: str, prompt: str) -> AstrBotCompanionHost:
        key = conversation or "main"
        if key in self.hosts:
            return self.hosts[key]
        runtime = self.open_runtime()
        route = build_route(key, self.user_id)
        now = now_utc

        identities = PersistentIdentityDirectory(runtime)
        policy = ObjectAccessPolicy(runtime)
        if identities.resolve_authenticated(route.principal, observed_at=now()).failure is not None:
            identities.bind(binding_id=self.user_id, principal=route.principal, subject=route.user,
                            valid_from=now(), expected_revision=identities.revision)
        policy.set_tier(route.user, AuthorizationTier.CORE,
                        expected_revision=policy.revision, observed_at=now())
        existing = {(g.capability, g.scope) for g in identities.grants_for_subject(route.user)}
        for capability in TASKS:
            if (capability, route.scope) in existing:
                continue
            identities.issue_grant(
                grant_id="grant-%s-%s" % (key, capability), subject=route.user, scope=route.scope,
                capability=capability, tier=AuthorizationTier.STANDARD, valid_from=now(),
                expires_at=None, expected_revision=identities.revision)

        personas = PersonaDirectory(runtime)
        personas.register_route(ConversationRoute(route.scope, user=route.user), observed_at=now())
        if personas.resolve(route.scope, observed_at=now()).failure is not None:
            personas.set_binding(
                PersonaBinding("companion", PersonaSelector(INSTALLATION, SelectorKind.BOT, bot_id="dsh"),
                               SubjectRef(INSTALLATION, SubjectKind.BOT, "companion"),
                               0, now(), "dsh", "admin"),
                expected_revision=0, observed_at=now())

        captured: list[str] = []

        async def sink(envelope):
            from companion_v2.delivery.models import DeliveryReceipt
            captured.append(envelope.text)
            await self.emit({"kind": "reply", "conversation": key, "part": envelope.part_id,
                             "sequence": envelope.sequence, "text": envelope.text})
            return DeliveryReceipt(
                envelope.part_id, envelope.operation_id, envelope.context.key, envelope.payload_hash,
                envelope.sender, "delivered", "dsh:" + envelope.part_id,
                "dsh-" + envelope.part_id, now())

        async def model_task(payload: dict) -> str:
            return await self.ask_model(payload["system"], payload["user"],
                                        int(payload.get("max_output_tokens") or 2048))

        profiles = (ModelTaskProfile(REPLY_KIND, Lane.FOREGROUND, "dsh", "补充任务提示",
                                     max_output_tokens=4096),)
        provider = make_provider(
            binding=ProviderBinding("model.text", "dsh", "1", "dsh-llm"),
            task=model_task, maximum_output_tokens=4096)
        sender = LocalSender(ProviderBinding("message.delivery", "dsh", "1", "dsh-local"), sink)
        segmentation = SegmentationPolicy(enabled=False)
        registry = FieldRegistry(version="dsh.v1", fields=configuration_fields(profiles))
        registry = FieldRegistry(version="dsh.v1", fields=(
            *registry.fields.values(),
            *tuple(f for f in reply_fields(segmentation, False, 0, None)
                   if f.field_id not in registry.fields)))

        ports = DshEventPort(route)
        host = AstrBotCompanionHost(
            runtime, event_port=ports, identity_port=ports, role_port=ports,
            runtime_identity=RuntimeIdentity("companion", "dsh-1", "dsh", "dsh-host"),
            registry=registry, profiles=profiles, providers=(provider,),
            budget=BudgetPolicy(*(Amount(2_000_000, 2_000_000) for _ in range(5))),
            sender=sender, limits=SupervisorLimits(4, 1, 64, 256),
            policy=ConversationPolicy(0, 0, 180, 0), owns_runtime=False, now=now,
            segmentation=segmentation, split=lambda text: tuple(text.splitlines(keepends=True)))

        await host.application.start(expected_task_generation=0, expected_model_generation=0)
        host._dsh_captured = captured  # type: ignore[attr-defined]
        self.hosts[key] = host
        return host

    async def close(self) -> None:
        for host in list(self.hosts.values()):
            try:
                await host.application.close()
            except Exception:
                traceback.print_exc()
        self.hosts.clear()
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    # ---------- commands ----------
    async def cmd_chat(self, req: dict) -> dict:
        conversation = req.get("conversation") or "main"
        prompt = req.get("prompt") or ""
        host = await self.host_for(conversation, prompt)
        captured: list[str] = host._dsh_captured  # type: ignore[attr-defined]
        before = len(captured)
        result = host.receive(
            {"id": req.get("event_id") or ("evt-%d" % (self._seq + 1)),
             "text": req.get("text") or "", "prompt": prompt},
            capability="text")
        if not result.is_success:
            return {"ok": False, "error": result.failure.code, "reason": result.failure.reason}
        batch = result.unwrap()
        for _ in range(int(req.get("wait_ms") or 120000) // 50):
            await asyncio.sleep(0.05)
            state = host.application.flow.row(batch)["state"]
            if state in ("complete", "blocked", "closed") or len(captured) > before:
                if state in ("complete", "blocked", "closed"):
                    break
                if len(captured) > before:
                    break
        state = host.application.flow.row(batch)["state"]
        return {"ok": True, "batch": batch, "state": state, "replies": captured[before:]}

    async def cmd_status(self, req: dict) -> dict:
        conversation = req.get("conversation") or "main"
        host = self.hosts.get(conversation)
        if host is None:
            return {"ok": True, "conversation": conversation, "started": False}
        app = host.application
        return {"ok": True, "conversation": conversation, "started": True,
                "state": app.state, "failure": app.failure_code,
                "batches": [dict(row) for row in app.runtime.read_all(
                    "SELECT batch_id, state, due_at, reason FROM conversation_flows "
                    "ORDER BY sequence DESC LIMIT 5")]}

    async def cmd_history(self, req: dict) -> dict:
        host = await self.host_for(req.get("conversation") or "main", "")
        rows = host.application.runtime.read_all(
            "SELECT batch_id, state, created_at FROM conversation_batches "
            "ORDER BY created_at DESC LIMIT 20")
        return {"ok": True, "batches": [dict(row) for row in rows]}

    async def cmd_reset(self, req: dict) -> dict:
        key = req.get("conversation") or "main"
        host = self.hosts.pop(key, None)
        if host is not None:
            await host.application.close()
        return {"ok": True, "reset": key}


async def main() -> int:
    data_root = Path(os.environ.get("DSH_COMPANION_DATA")
                     or (Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / "companion"))
    user_id = os.environ.get("DSH_COMPANION_USER") or "owner"
    bridge = Bridge(data_root, user_id)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("kind") == "model":
                # Model answers must land even while a command is running:
                # the command is what is waiting for them.
                rid = str(payload.get("rid"))
                text = str(payload.get("text") or "")
                loop.call_soon_threadsafe(bridge.resolve_model, rid, text)
                continue
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        loop.call_soon_threadsafe(queue.put_nowait, {"cmd": "__eof__"})

    import threading
    threading.Thread(target=reader, daemon=True).start()
    await bridge.emit({"kind": "ready", "data": str(data_root)})

    try:
        while True:
            request = await queue.get()
            command = request.get("cmd")
            request_id = request.get("id")
            if command == "__eof__":
                break
            handler = {
                "chat": bridge.cmd_chat,
                "status": bridge.cmd_status,
                "history": bridge.cmd_history,
                "reset": bridge.cmd_reset,
                "ping": lambda req: asyncio.sleep(0, result={"ok": True, "pong": True}),
            }.get(command)
            if handler is None:
                await bridge.emit({"id": request_id, "ok": False, "error": "unknown_command",
                                   "reason": str(command)})
                continue
            try:
                result = await handler(request)
            except Exception as error:
                traceback.print_exc()
                result = {"ok": False, "error": type(error).__name__, "reason": str(error)[:500]}
            await bridge.emit({"id": request_id, **result})
    finally:
        await bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
