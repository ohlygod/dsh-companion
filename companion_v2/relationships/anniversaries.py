"""Relationship milestones the bot can create for itself during daily reflection."""

from __future__ import annotations

from datetime import date, datetime
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from companion_v2.execution.models import digest, stamp
from companion_v2.foundation.refs import ScopeKind


MIN_CONFIDENCE = 0.8
MAX_ACTIVE = 24
MIN_LABEL_CHARS = 2
MAX_LABEL_CHARS = 48
MAX_REASON_CHARS = 320
INSTRUCTION = (
    "anniversaries 是可选字段，最多 1 条。只有本轮用户原话明确支持、值得以后每年想起的关系里程碑才记录，"
    "例如第一次明确告白或确认关系、第一次真正见面、第一次共同完成重要约定；普通亲密聊天、一次情绪、"
    "日常问候、转述旧事、仅由 Bot 自己说出的话都不算。label 写 2–48 字的简短名称，reason 写 320 字以内"
    "的具体依据，confidence 为 0–1，只有达到 0.8 的确定度才提交，evidence_handle 必须引用支持这件事的当前用户原话 s 编号。"
    "日期由系统取该原话的发生日期，不由模型另填。用户只是回忆过去、引用更早日期或无法确定发生时间时不要创建；"
    "没有明确里程碑就填空数组，不要为了凑数创建。"
)


def owner_key(context) -> str:
    """Stable across private windows; one anniversary set per role/person pair."""
    return digest(["relationship-anniversaries.v1", context.scope.installation_id,
                   context.scope.bot_id, context.role_binding.role.subject_id,
                   context.actor.subject.subject_id, "private"])


def _clean_label(value):
    if type(value) is not str:
        raise ValueError("anniversary_label_invalid")
    label = " ".join(value.split())
    if (not MIN_LABEL_CHARS <= len(label) <= MAX_LABEL_CHARS
            or any(ord(character) < 32 for character in label)):
        raise ValueError("anniversary_label_invalid")
    return label


def _clean_reason(value):
    if type(value) is not str:
        raise ValueError("anniversary_reason_invalid")
    reason = " ".join(value.split())
    if not reason or len(reason) > MAX_REASON_CHARS or any(ord(character) < 32 for character in reason):
        raise ValueError("anniversary_reason_invalid")
    return reason


def validate_candidates(items, *, context, resolve_evidence, timezone_name, at):
    """Validate model candidates without accepting a model-supplied calendar date.

    The anniversary date is the user message's own occurrence date in the
    configured reflection timezone. This prevents a model from inventing a date
    from an old recollection or making the bot announce a false anniversary.
    """
    if context.scope.kind is not ScopeKind.PRIVATE:
        return [], []
    if items is None:
        return [], []
    if type(items) is not list or len(items) > 1:
        code = "anniversary_limit_per_run" if isinstance(items, list) else "invalid_anniversaries"
        return [], [{"item_id": "anniversary-1", "raw": items, "kind": "anniversary",
                     "problem": {"code": code, "path": "anniversaries"}}]
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Shanghai")
    accepted, rejected = [], []
    for index, raw in enumerate(items):
        item_id = "anniversary-" + str(index + 1)
        try:
            if type(raw) is not dict or set(raw) != {
                    "label", "reason", "confidence", "evidence_handle"}:
                raise ValueError("anniversary_shape_invalid")
            label = _clean_label(raw["label"])
            reason = _clean_reason(raw["reason"])
            confidence = raw["confidence"]
            if (type(confidence) not in (int, float) or not 0 <= confidence <= 1
                    or confidence < MIN_CONFIDENCE):
                raise ValueError("anniversary_confidence_invalid")
            handle = raw["evidence_handle"]
            if type(handle) is not str or not handle:
                raise ValueError("anniversary_evidence_invalid")
            evidence = resolve_evidence(handle)
            if evidence is None or getattr(evidence, "speaker_id", None) != context.actor.subject.subject_id:
                raise ValueError("anniversary_user_evidence_required")
            occurred_at = getattr(getattr(evidence, "source", None), "issued_at", None)
            if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
                raise ValueError("anniversary_evidence_time_invalid")
            if occurred_at > at:
                raise ValueError("anniversary_evidence_time_invalid")
            local = occurred_at.astimezone(zone)
            accepted.append({
                "item_id": "anniversary:" + digest([owner_key(context), label, local.isoformat()]),
                "label": label,
                "reason": reason,
                "confidence": float(confidence),
                "evidence_handle": handle,
                "occurred_at": stamp(occurred_at),
                "month": local.month,
                "day": local.day,
                "timezone": zone.key,
            })
        except (TypeError, ValueError, KeyError) as error:
            code = str(error) if str(error).startswith("anniversary_") else "anniversary_invalid"
            rejected.append({"item_id": item_id, "raw": raw, "kind": "anniversary",
                             "problem": {"code": code, "path": f"anniversaries[{index}]"}})
        except Exception:
            rejected.append({"item_id": item_id, "raw": raw, "kind": "anniversary",
                             "problem": {"code": "anniversary_evidence_unavailable",
                                         "path": f"anniversaries[{index}]"}})
    return accepted, rejected


def _active_rows(runtime, context):
    return runtime.read_all(
        "SELECT anniversary_id, label, month, day, timezone, confidence, source_run_id, created_at "
        "FROM relationship_anniversaries WHERE owner_key = ? AND state = 'active' "
        "ORDER BY created_at, anniversary_id", (owner_key(context),))


def remember(runtime, context, candidate, *, source_run_id, at):
    """Persist one already-validated anniversary; safe inside an outer reflection transaction."""
    if context.scope.kind is not ScopeKind.PRIVATE:
        return {"state": "rejected", "code": "anniversary_private_required"}
    owner = owner_key(context)
    label, month, day = candidate["label"], candidate["month"], candidate["day"]
    existing = runtime.read_one(
        "SELECT anniversary_id FROM relationship_anniversaries "
        "WHERE owner_key = ? AND state = 'active' AND (label = ? OR (month = ? AND day = ?)) limit 1",
        (owner, label, month, day))
    if existing is not None:
        return {"state": "duplicate", "anniversary_id": existing["anniversary_id"]}
    count = runtime.read_one(
        "SELECT COUNT(*) AS count FROM relationship_anniversaries WHERE owner_key = ? AND state = 'active'",
        (owner,))["count"]
    if count >= MAX_ACTIVE:
        return {"state": "limit", "code": "anniversary_limit"}
    anniversary_id = "anniversary:" + digest([owner, label, month, day])
    evidence = json.dumps({
        "evidence_handle": candidate["evidence_handle"],
        "occurred_at": candidate["occurred_at"],
        "speaker_id": context.actor.subject.subject_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with runtime.transaction() as connection:
        inserted = connection.execute(
            """INSERT OR IGNORE INTO relationship_anniversaries
               (anniversary_id, owner_key, role_id, subject_id, label, month, day, timezone,
                confidence, evidence_json, source_run_id, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (anniversary_id, owner, context.role_binding.role.subject_id,
             context.actor.subject.subject_id, label, month, day, candidate["timezone"],
             candidate["confidence"], evidence, source_run_id, stamp(at))).rowcount
    if not inserted:
        return {"state": "duplicate"}
    return {"state": "created", "anniversary_id": anniversary_id}


def _next_occurrence(local_day, month, day):
    for year in range(local_day.year, local_day.year + 5):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= local_day:
            return candidate
    return None


def list_active(runtime, context):
    """Return the automatic entries that the role can honestly report as remembered."""
    if context.scope.kind is not ScopeKind.PRIVATE:
        return ()
    return tuple({"anniversary_id": row["anniversary_id"], "label": row["label"],
                  "month": row["month"], "day": row["day"], "timezone": row["timezone"]}
                 for row in _active_rows(runtime, context))


def retract_by_label(runtime, context, label):
    """Retract one automatic milestone by its exact displayed name."""
    if context.scope.kind is not ScopeKind.PRIVATE or type(label) is not str:
        return ()
    normalized = " ".join(label.split())
    if not normalized:
        return ()
    owner = owner_key(context)
    matches = runtime.read_all(
        "SELECT anniversary_id FROM relationship_anniversaries "
        "WHERE owner_key = ? AND state = 'active' AND label = ?", (owner, normalized))
    if not matches:
        return ()
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE relationship_anniversaries SET state = 'retracted' "
            "WHERE owner_key = ? AND state = 'active' AND label = ?", (owner, normalized))
    return (normalized,)


def upcoming(runtime, context, at, *, timezone_name, lookahead_days):
    if type(lookahead_days) is not int or not 0 <= lookahead_days <= 30:
        return ()
    if context.scope.kind is not ScopeKind.PRIVATE:
        return ()
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Shanghai")
    local_day = at.astimezone(zone).date()
    rows = []
    for row in _active_rows(runtime, context):
        occurrence = _next_occurrence(local_day, row["month"], row["day"])
        if occurrence is None:
            continue
        days_until = (occurrence - local_day).days
        if days_until <= lookahead_days:
            rows.append({
                "basis": "auto_relationship_anniversary",
                "label": row["label"],
                "date": occurrence.isoformat(),
                "days_until": days_until,
                "calendar": "gregorian",
            })
    rows.sort(key=lambda item: (item["days_until"], item["label"]))
    return tuple(rows)


__all__ = ["INSTRUCTION", "MAX_ACTIVE", "MIN_CONFIDENCE", "list_active", "owner_key", "remember",
           "retract_by_label", "upcoming", "validate_candidates"]
