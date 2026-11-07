"""Read explicit legacy private permissions separately from relationship facts."""

PERMISSIONS = {"private_companion_enabled": "conversation.reply",
               "proactive_private_enabled": "proactive.contact"}


def private_authorization(key, user, configuration):
    participant = user["identity_subject_id"]
    configuration = configuration if isinstance(configuration, dict) else {}
    basic = configuration.get("basic_config", {})
    targets = basic.get("target_user_ids", configuration.get("target_user_ids", [])) \
        if isinstance(basic, dict) else []
    if not isinstance(targets, list):
        targets = []
    # Legacy aliases merge conversation data, never administrator identities.
    canonical = str(key) == participant
    owner = canonical and (participant in targets or user.get("relationship_role") == "owner")
    capabilities = user.get("unified_profile_capabilities")
    permissions = {}
    if isinstance(capabilities, dict) and capabilities.get("schema_version") == 1:
        permissions = {capability: capabilities[name] for name, capability in PERMISSIONS.items()
                       if type(capabilities.get(name)) is bool}
    return {"tier": "core" if owner else None, "permissions": permissions,
            "tier_sources": (["basic_config.target_user_ids"] if canonical and participant in targets else [])
                + (["users.relationship_role"] if canonical and user.get("relationship_role") == "owner" else [])}


def merge_authorization(first, second):
    permissions = {**first["permissions"], **second["permissions"]}
    conflicts = set(first.get("conflicts", ())) | set(second.get("conflicts", ()))
    conflicts.update(key for key, value in first["permissions"].items()
                     if key in second["permissions"] and value != second["permissions"][key])
    for key in conflicts:
        permissions.pop(key, None)
    return {"tier": first["tier"] or second["tier"], "permissions": permissions,
            "tier_sources": sorted(set(first["tier_sources"] + second["tier_sources"])),
            "conflicts": sorted(conflicts)}
