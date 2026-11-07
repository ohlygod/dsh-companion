"""DSH provider adapter: one model call per envelope, via a JSON bridge.

The core builds a provider-neutral wire (system + user JSON) and expects a JSON
reply of ``{action, text, claims}``. This module keeps that contract and hands
the actual generation to the DSH side of the bridge, so the plugin reuses
whatever model DSH already has configured — no second API key.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from companion_v2.adapters.astrbot.model_wire import build_wire, decode_text_response
from companion_v2.model_gateway.models import Amount, LocalProvider, ProviderResponse, ProviderBinding
from companion_v2.execution.models import digest

ModelRequest = Callable[[dict], Awaitable[str]]


def _flatten(wire) -> dict:
    """Render the core's wire as one host-agnostic request payload."""
    system_parts, user_parts = [], []
    for message in wire.messages:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
        else:
            text = content if isinstance(content, str) else ""
        (system_parts if role == "system" else user_parts).append(text)
    return {
        "system": "\n\n".join(part for part in system_parts if part),
        "user": "\n\n".join(part for part in user_parts if part),
        "tools": [tool.get("name") for tool in wire.tools],
    }


def make_provider(*, binding: ProviderBinding, task: ModelRequest,
                  maximum_output_tokens: int = 4096) -> LocalProvider:
    """Build the LocalProvider the core routes through."""

    def reserve(envelope) -> Amount:
        ceiling = max(int(envelope.max_output_tokens or 0), maximum_output_tokens)
        return Amount(ceiling * 2, maximum_output_tokens)

    async def generate(envelope) -> ProviderResponse:
        wire = build_wire(envelope)
        payload = _flatten(wire)
        payload["max_output_tokens"] = envelope.max_output_tokens
        raw = await task(payload)
        if not isinstance(raw, str):
            raise ValueError("host returned a non-text model result")
        try:
            draft, calls = decode_text_response(raw, wire)
        except Exception:
            import os, sys, traceback
            if os.environ.get("DSH_COMPANION_DEBUG"):
                traceback.print_exc()
                sys.stderr.write("[companion] raw model text head: %r\n" % raw[:400])
            raise
        if calls:
            return ProviderResponse("generated", tool_calls=calls)
        return ProviderResponse("generated", draft=draft)

    return LocalProvider(binding, ("text", "tools"), maximum_output_tokens, reserve, generate,
                         transport="dsh_llm")


class DeliverySink:
    """The core hands us finished reply parts; DSH decides where they surface."""

    def __init__(self, send: Callable[[dict], Awaitable[None]]) -> None:
        self._send = send
        self.seen: list[str] = []

    async def __call__(self, envelope) -> Any:
        from companion_v2.delivery.models import DeliveryReceipt
        self.seen.append(envelope.text)
        await self._send({
            "kind": "reply",
            "part_id": envelope.part_id,
            "text": envelope.text,
            "sequence": envelope.sequence,
        })
        return DeliveryReceipt(
            envelope.part_id, envelope.operation_id, envelope.context.key, envelope.payload_hash,
            envelope.sender, "delivered",
            "dsh:" + envelope.part_id, "dsh-" + digest([envelope.part_id])[:16],
            _now(),
        )


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
