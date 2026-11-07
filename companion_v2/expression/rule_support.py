"""Additional, source-bound observations of an existing expression form.

The original run and its evidence remain immutable. Later observations keep
only handles and their exact source window; raw conversation text is reread
under the current authorization and sample-review policy when displayed.
"""

import json

from companion_v2.execution.models import digest, encode


PREFIX = "expression.rule-support:"
MAX_RECENT_OBSERVATIONS = 64


def _key(signal_id):
    return PREFIX + digest(signal_id)


def observations(runtime, signal_id):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (_key(signal_id),))
    if row is None:
        return ()
    try:
        values = json.loads(row["value"])
        if type(values) is not list:
            return ()
        for item in values:
            if (type(item) is not dict or set(item) != {"window", "handles"}
                    or type(item["window"]) is not dict or type(item["handles"]) is not list
                    or set(item["window"]) != {"opened_at", "closed_at", "source_kind", "context_key"}
                    or any(type(value) is not str for value in item["window"].values())
                    or item["window"]["source_kind"] != "conversation_input"
                    or any(type(handle) is not str for handle in item["handles"])):
                return ()
        return tuple(values)
    except (TypeError, ValueError):
        return ()


def record(runtime, signal_id, *, window, handles, original_handles):
    """Keep genuinely new cited handles without changing the adopted rule."""
    if (type(window) is not dict or window.get("source_kind") != "conversation_input"
            or type(window.get("handles")) is not list or type(handles) not in (list, tuple)
            or any(handle not in window["handles"] for handle in handles)):
        return False
    previous = observations(runtime, signal_id)
    known = set(original_handles)
    known.update(handle for item in previous for handle in item["handles"])
    added = sorted(set(handles) - known)
    if not added:
        return False
    entry = {"window": {name: window[name] for name in
             ("opened_at", "closed_at", "source_kind", "context_key")}, "handles": added}
    with runtime.transaction() as connection:
        connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_key(signal_id), encode((*previous, entry)[-MAX_RECENT_OBSERVATIONS:])))
    return True
