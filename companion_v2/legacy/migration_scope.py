"""Decide which V1 windows and personas a role can absorb without guessing.

The old model is "personas ↔ windows"; the new one is "roles (many personas) ↔
windows". A window's identity does not change across that model, so a window
whose platform, account and native id survive untouched can be grouped under
the role automatically. Anything else is returned as ``needs_review`` with the
reason, never guessed: writing a window into the wrong person's role is the one
failure this migration must not make.

These helpers are pure: no reads, no writes, no clock. The batch endpoints and
the review page both use them so the preview and the import cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass


# Reasons are stable codes; the page maps them to operator-facing text.
AUTO_SAME_ID = "same_platform_account_and_id"
REVIEW_GROUP_MISSING = "target_group_missing"
REVIEW_PLATFORM_DIFFERS = "target_platform_differs"
REVIEW_ACCOUNT_DIFFERS = "target_account_differs"
REVIEW_ROLE_MISSING = "target_group_role_missing"
REVIEW_AMBIGUOUS = "multiple_target_candidates"
REVIEW_PERSONA_UNASSIGNED = "persona_not_assigned_to_role"
REVIEW_PERSONA_OTHER_ROLE = "persona_assigned_to_other_role"


@dataclass(frozen=True)
class WindowMapping:
    source_id: str
    target_id: str | None
    auto: bool
    reason: str

    def document(self) -> dict:
        return {"source_id": self.source_id, "target_id": self.target_id,
                "auto": self.auto, "reason": self.reason}


def _platform(value):
    return str(value or "").strip()


def map_groups(source_groups, target_groups, *, platform_id, account_id):
    """Map V1 group ids onto V2 group targets without inventing a destination.

    ``source_groups`` are the ids the V1 persona actually has data for.
    ``target_groups`` are group targets of the current bot/account, each a
    mapping with ``group_id``, ``platform_id``, ``account_id`` and an optional
    ``has_role`` flag (a target without a role cannot receive data).
    """
    platform, account = _platform(platform_id), _platform(account_id)
    candidates = {}
    for target in target_groups or ():
        if not isinstance(target, dict):
            continue
        group_id = _platform(target.get("group_id"))
        if not group_id:
            continue
        candidates.setdefault(group_id, []).append(target)
    result = []
    for item in source_groups or ():
        source_id = _platform(item.get("source_group_id") if isinstance(item, dict) else item)
        if not source_id:
            continue
        matches = candidates.get(source_id, ())
        if not matches:
            result.append(WindowMapping(source_id, None, False, REVIEW_GROUP_MISSING))
            continue
        if len(matches) > 1:
            # Two windows can share a group number on different accounts; only
            # an exact platform+account match disambiguates them.
            matches = tuple(target for target in matches
                            if _platform(target.get("platform_id")) == platform
                            and _platform(target.get("account_id")) == account)
            if len(matches) != 1:
                result.append(WindowMapping(source_id, None, False, REVIEW_AMBIGUOUS))
                continue
        target = matches[0]
        if platform and _platform(target.get("platform_id")) != platform:
            result.append(WindowMapping(source_id, None, False, REVIEW_PLATFORM_DIFFERS))
            continue
        if account and _platform(target.get("account_id")) != account:
            result.append(WindowMapping(source_id, None, False, REVIEW_ACCOUNT_DIFFERS))
            continue
        if target.get("has_role") is False:
            result.append(WindowMapping(source_id, None, False, REVIEW_ROLE_MISSING))
            continue
        result.append(WindowMapping(source_id, _platform(target.get("target_id")) or source_id,
                                    True, AUTO_SAME_ID))
    return tuple(result)


def map_personas(source_personas, persona_roles, *, role_id):
    """Assign V1 personas to the role that already claims them, or ask.

    ``persona_roles`` is the installation's confirmed persona → role mapping.
    A source persona is automatic only when that mapping points at this role;
    an unmapped or differently-owned persona is never dragged in.
    """
    owner = _platform(role_id)
    result = []
    mapped_roles = persona_roles or {}
    for item in source_personas or ():
        persona_id = _platform(item.get("persona_id") if isinstance(item, dict) else item)
        # The main profile has no id; it is still a persona with its own role.
        if not persona_id and "" not in mapped_roles:
            continue
        assigned = _platform(mapped_roles.get(persona_id))
        if not assigned:
            result.append(WindowMapping(persona_id, None, False, REVIEW_PERSONA_UNASSIGNED))
        elif owner and assigned != owner:
            result.append(WindowMapping(persona_id, None, False, REVIEW_PERSONA_OTHER_ROLE))
        else:
            result.append(WindowMapping(persona_id, owner or assigned, True, AUTO_SAME_ID))
    return tuple(result)


def needs_review(mappings):
    return tuple(item for item in mappings if not item.auto)


def _group_counts(plan):
    """Collect what each V1 group actually holds, so the preview totals match."""
    counts = {}
    def entry(group_id):
        return counts.setdefault(_platform(group_id), {"members": 0, "edges": 0,
            "knowledge_entries": 0, "member_profiles": 0})
    for item in (plan or {}).get("group_networks") or ():
        if not isinstance(item, dict):
            continue
        record = entry(item.get("source_group_id"))
        if record is None:
            continue
        record["members"] = max(record["members"], int(item.get("member_count") or item.get("members") or 0))
        record["edges"] = max(record["edges"], int(item.get("edge_count") or item.get("edges") or 0))
    worldbook = (plan or {}).get("worldbook") or {}
    for item in worldbook.get("groups") or ():
        if isinstance(item, dict):
            record = entry(item.get("source_group_id"))
            if record is not None:
                record["knowledge_entries"] += int(item.get("entry_count") or 0)
    for item in worldbook.get("member_profile_groups") or ():
        if isinstance(item, dict):
            record = entry(item.get("source_group_id"))
            if record is not None:
                record["member_profiles"] += int(item.get("member_count") or 0)
    counts.pop("", None)
    return counts


def build_scope(plans, target_groups, persona_roles, *, role_id, platform_id, account_id):
    """Compose the review document a role-driven migration shows before writing.

    ``plans`` is one ``(source_persona_id, plan_document)`` pair per V1 persona.
    Groups are planned per persona because the importer is addressed by a
    (persona, group) pair: merging them first would let one persona's group
    stand in for another's. Nothing here reads or writes storage: the page and
    the batch importer call this same function, so what was reviewed is what
    gets applied.
    """
    from companion_v2.execution.models import digest

    entries = []
    for item in plans or ():
        if isinstance(item, dict):
            persona_id, plan = item.get("persona_id"), item.get("document")
        else:
            persona_id, plan = item
        persona_id = _platform(persona_id)
        entries.append((persona_id, plan or {}))
    personas = map_personas([{"persona_id": persona_id} for persona_id, _ in entries],
                            persona_roles, role_id=role_id)
    persona_by_id = {item.source_id: item for item in personas}
    groups, review, contacts, rejected = [], [], 0, 0
    for persona_id, plan in entries:
        counts = _group_counts(plan)
        mapped = map_groups(list(counts), target_groups,
                            platform_id=platform_id, account_id=account_id)
        owner = persona_by_id.get(persona_id)
        for item in mapped:
            persona_auto = bool(owner and owner.auto)
            auto = persona_auto and item.auto
            reason = item.reason if not item.auto else (AUTO_SAME_ID if persona_auto
                                                        else owner.reason)
            entry = {"persona_id": persona_id, "source_id": item.source_id,
                     "target_id": item.target_id if auto else None, "auto": auto,
                     "reason": reason, "counts": counts.get(item.source_id, {})}
            groups.append(entry)
            if not auto:
                review.append({"kind": "group", "persona_id": persona_id, **{
                    key: entry[key] for key in ("source_id", "target_id", "reason")}})
        private = plan.get("private_contacts") or {}
        contacts += int(private.get("importable") or private.get("total") or private.get("count") or 0)
        rejected += len(private.get("rejected") or ())
    review = [{"kind": "persona", **item.document()} for item in personas if not item.auto] + review
    document = {
        "role_id": _platform(role_id),
        "personas": [item.document() for item in personas],
        "groups": groups,
        "private": {"contacts": contacts, "rejected": rejected},
        "review": review,
        "counts": {
            "personas": len(personas),
            "personas_auto": sum(1 for item in personas if item.auto),
            "groups": len(groups),
            "groups_auto": sum(1 for item in groups if item["auto"]),
            "review": len(review),
        },
    }
    document["token"] = digest(["legacy-role-scope.v2", document])
    return document
