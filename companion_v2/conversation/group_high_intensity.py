"""V2-native high-intensity group closeout.

V1 counted the role's delivered turns inside a short window and, once the
threshold was reached, kept the group quiet for a cooldown.  V2 already exposes
the same window/count/cooldown values through the air guard; this module adds
the opt-in *hard* part without reintroducing V1's separate merge queue.  The
generic group turn/batch path remains responsible for merging.

Only explicit addresses and verified follow-ups bypass the closeout.  Questions,
interest words, cold-group openings, repeats and behaviour guesses all stay
quiet while it is active.
"""

from datetime import datetime, timedelta

BYPASS_REASONS = frozenset((
    "mention", "native_wakeup", "quoted_reply", "native_quote", "direct_word",
    "active_follow_up", "reply_follow_up",
))


def state(rounds, at, *, window_seconds, threshold, cooldown_seconds):
    """Return the closeout state for already-returned role turns.

    ``rounds`` are timestamps of confirmed delivered turns.  A burst is
    ``threshold`` turns whose first and last timestamps fit in the window.
    Recomputing from bounded history makes the state replay-safe and leaves no
    separate mutable cooldown row to get stale.
    """
    if type(at) is not datetime or at.tzinfo is None:
        raise ValueError("high-intensity group closeout needs an aware time")
    if type(window_seconds) is not int or window_seconds < 1:
        raise ValueError("high-intensity window is invalid")
    if type(threshold) is not int or threshold < 2:
        raise ValueError("high-intensity threshold is invalid")
    if type(cooldown_seconds) is not int or cooldown_seconds < 0:
        raise ValueError("high-intensity cooldown is invalid")
    eligible = sorted(when for when in rounds if isinstance(when, datetime) and when <= at)
    until = None
    for end in range(threshold - 1, len(eligible)):
        burst = eligible[end - threshold + 1:end + 1]
        if (burst[-1] - burst[0]).total_seconds() <= window_seconds:
            candidate = burst[-1] + timedelta(seconds=cooldown_seconds)
            until = candidate if until is None else max(until, candidate)
    active = until is not None and at < until
    start = at - timedelta(seconds=window_seconds)
    return {
        "active": active,
        "recent_wakeups": sum(when >= start for when in eligible),
        "threshold": threshold,
        "window_seconds": window_seconds,
        "cooldown_seconds": cooldown_seconds,
        "until": until.isoformat() if active and until is not None else "",
        "remaining_seconds": max(0, round((until - at).total_seconds())) if active and until is not None else 0,
    }


def allows(reason, closeout):
    """Whether a candidate reason can still speak during the closeout."""
    return not closeout.get("active") or reason in BYPASS_REASONS
