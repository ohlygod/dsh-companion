"""Optional local meal-time opportunities for the original private conversation."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest, stamp

from .configuration import local_time
from .contact_models import ContactIntent
from .models import EvidenceSnapshot, Opportunity, Thought, ThoughtKind


WINDOW = timedelta(minutes=45)
MEAL_ANGLES = (
    "可以从角色自己刚吃到、想吃或正在准备的一点点具体细节说起，也可以完全不聊食物。",
    "可以分享角色刚忙完一段、准备歇一会儿的感受，邀请对方按自己的节奏接话。",
    "可以接回原对话里仍有意义的话题，把饭点只当作角色自己的时间背景。",
    "如果最近没有新内容、对方刚回过或此刻显得打扰，直接让本次机会沉默。",
    "可以只说一句角色自己的日常观察，不要求对方交代日程或饮食。",
)


class MealContactEvents:
    def __init__(self, contacts):
        self.contacts = contacts
        self.runtime = contacts.runtime

    def _window(self, preferences):
        """How long the meal slot stays open, from the configured follow-up width.

        Defaults to the original hard-coded 45 minutes, so an installation that
        never touches the field behaves exactly as before.
        """
        minutes = int(preferences.values.get("meal_care_followup_minutes") or 45)
        return timedelta(minutes=minutes)

    @staticmethod
    def _daily_plan(context, day):
        """Stable day-level cadence: some days skip, some keep one slot, some both."""
        roll = int(digest(["meal-cadence-v2", context.key, day.isoformat()])[:8], 16) % 100
        if roll < 15:
            return ()
        if roll < 65:
            return ("lunch",) if roll % 2 else ("dinner",)
        return ("lunch", "dinner")

    @staticmethod
    def _opening(context, day, wall_time, zone, name):
        """Float the slot deterministically around its configured reference time."""
        base = datetime.combine(day, wall_time, tzinfo=zone)
        offset = int(digest(["meal-jitter-v2", context.key, day.isoformat(), name])[:8], 16) % 61 - 20
        return base + timedelta(minutes=offset)

    @staticmethod
    def _angle(context, day, name):
        index = int(digest(["meal-angle-v2", context.key, day.isoformat(), name])[:8], 16)
        return MEAL_ANGLES[index % len(MEAL_ANGLES)]

    def _slots(self, context, preferences, at):
        if not preferences.values["meal_checkin_enabled"] or preferences.reason("meal"):
            return ()
        zone = ZoneInfo(preferences.values["quiet_timezone"])
        day = at.astimezone(zone).date()
        window = self._window(preferences)
        allowed = self._daily_plan(context, day)
        slots = []
        for name, field, label in (("lunch", "meal_lunch_time", "午餐"),
                                   ("dinner", "meal_dinner_time", "晚餐")):
            if name not in allowed:
                continue
            wall_time = local_time(preferences.values[field])
            resolved_local = self._opening(context, day, wall_time, zone, name)
            if resolved_local.date() != day:
                continue
            opened = resolved_local.astimezone(timezone.utc)
            if opened <= at < opened + window:
                opportunity = Opportunity("meal-" + digest([context.key, day.isoformat(), name]),
                    context.scope, opened, opened + window)
                marker = {"slot": name, "local_date": day.isoformat(),
                          "opened_at": stamp(opportunity.opened_at), "expires_at": stamp(opportunity.expires_at)}
                slots.append((opportunity, marker, label))
        return tuple(slots)

    def select(self, context, preferences, at):
        for opportunity, marker, label in self._slots(context, preferences, at):
            day = date.fromisoformat(marker["local_date"])
            name = marker["slot"]
            task_id = self.contacts.task_id(context, opportunity)
            if self.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id = ?", (task_id,)) or self.runtime.read_one(
                    "SELECT 1 FROM proactive_contacts WHERE task_id = ?", (task_id,)):
                continue
            angle = self._angle(context, day, name)
            thought = Thought("meal-checkin-" + digest(marker), opportunity, ThoughtKind.CONVERSATION,
                f"按当前免打扰时区，现在接近角色自己的{label}时间，但这不是对对方的三餐打卡。{angle}"
                "不要默认询问对方吃没吃、不要提醒按时吃饭，也不要把饭点说成对方已经用餐的事实。"
                "只把饭点当作角色自己的生活背景，没有自然话头就跳过。",
                EvidenceSnapshot(context.scope, at), 1.0)
            return opportunity, (ContactIntent("meal", thought),), marker
        return None

    def validate(self, context, marker, at):
        if type(marker) is not dict or set(marker) != {"slot", "local_date", "opened_at", "expires_at"}:
            raise TaskError("contact_meal_invalid", "meal opportunity requires its original local slot")
        preferences = self.contacts.preferences.read(context, observed_at=at).unwrap()
        if not any(candidate == marker for _, candidate, _ in self._slots(context, preferences, at)):
            raise TaskError("contact_meal_expired", "the original meal slot is no longer current")
