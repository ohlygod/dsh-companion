"""DSH host ports for companion_v2.

This module replaces ``companion_v2.adapters.astrbot``: the same core, but the
four ports the core trusts are filled by DSH instead of AstrBot.

Two directions are bridged over the same NDJSON pipe:

* Node -> Python  requests (chat / history / state / memory ...)
* Python -> Node  one reverse request per model call, so DSH's own configured
  model answers. The core never learns where that model lives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal
from companion_v2.identity_policy.personas import ConversationRoute

INSTALLATION = "dsh-companion"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Route:
    """One DSH conversation, already authenticated by the host."""

    scope: ScopeRef
    user: SubjectRef
    principal: AuthenticatedHostPrincipal


class DshEventPort:
    """Everything the core is allowed to trust about one DSH turn.

    The host supplies the identity; message text is never used to derive it.
    """

    def __init__(self, route: Route) -> None:
        self.route = route

    def event_id(self, event: dict) -> str:
        return str(event["id"])

    def scope(self, event: dict) -> ScopeRef:
        return self.route.scope

    def audience(self, event: dict) -> AudienceRef:
        return AudienceRef(self.route.scope, (self.route.user,))

    def payload(self, event: dict) -> str:
        return str(event.get("text") or "")

    def payload_hash(self, event: dict) -> str:
        return hashlib.sha256(self.payload(event).encode("utf-8")).hexdigest()

    def received_at(self, event: dict) -> datetime:
        return now_utc()

    def conversation_route(self, event: dict) -> ConversationRoute:
        return ConversationRoute(self.route.scope, user=self.route.user)

    def authenticated_principal(self, event: dict) -> Outcome:
        return Outcome.success(self.route.principal)

    def role_instruction(self, event: dict, context: Any) -> str:
        return str(event.get("prompt") or "")


def build_route(conversation_id: str, user_id: str = "owner") -> Route:
    """Scope one DSH conversation. Session identity never comes from content."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in conversation_id)[:48] or "default"
    scope = ScopeRef(
        INSTALLATION, "dsh", ScopeKind.PRIVATE,
        native_scope_id=safe, bot_id="dsh", account_id="dsh-local",
        conversation_id=safe, session_id=safe,
    )
    principal = AuthenticatedHostPrincipal(
        INSTALLATION, "dsh-host", "dsh", user_id, "dsh", "dsh-local")
    user = SubjectRef(INSTALLATION, SubjectKind.HUMAN, user_id)
    return Route(scope=scope, user=user, principal=principal)
