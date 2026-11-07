"""Safe V1 Reality Companion configuration conversion."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .native_extension_configuration import _leaves


REALITY_INTEGER_BOUNDS = {
    "camera.min_interval_seconds": (10, 3600),
    "camera.capture_timeout_seconds": (2, 20),
    "camera.analysis_timeout_seconds": (5, 90),
    "camera.proactive_min_tier": (1, 5),
    "camera.proactive_max_daily": (0, 10),
    "camera.proactive_cooldown_minutes": (10, 1440),
    "audio.default_playback_volume": (0, 100),
    "home.request_timeout_seconds": (1, 30),
    "health.ttl_seconds": (60, 604800),
    "mobile.session_ttl_hours": (1, 720),
    "mobile.location_ttl_seconds": (60, 86400),
    "mobile.amap_cache_ttl_seconds": (60, 604800),
    "mobile.amap_request_timeout_seconds": (1, 20),
    "mobile.telemetry_ttl_seconds": (60, 604800),
    "mobile.activity_ttl_seconds": (60, 86400),
}

REALITY_SETTING_DEFAULTS = {
    "camera.enabled": False,
    "camera.min_interval_seconds": 60,
    "camera.capture_timeout_seconds": 5,
    "camera.analysis_timeout_seconds": 25,
    "camera.proactive_min_tier": 4,
    "camera.proactive_max_daily": 1,
    "camera.proactive_cooldown_minutes": 240,
    "audio.default_playback_volume": 35,
    "home.request_timeout_seconds": 5,
    "health.ttl_seconds": 3600,
    "mobile.session_ttl_hours": 168,
    "mobile.location_ttl_seconds": 900,
    "mobile.amap_cache_ttl_seconds": 1800,
    "mobile.amap_request_timeout_seconds": 5,
    "mobile.telemetry_ttl_seconds": 3600,
    "mobile.activity_ttl_seconds": 900,
    "vision_provider_id": "",
    "timezone": "Asia/Shanghai",
}


@dataclass(frozen=True)
class RealityConfigurationMigration:
    changes: dict[str, Any]
    differences: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


def _value_at(config: Any, dotted_key: str, default=None):
    if isinstance(config, Mapping) and dotted_key in config:
        return config[dotted_key]
    current = config
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def reality_setting_value(config: Any, dotted_key: str, default=None):
    return _value_at(config, dotted_key, default)


def reality_settings_state_token(config: Any, fields=None) -> str:
    keys = tuple(sorted(fields or REALITY_SETTING_DEFAULTS))
    state = {key: _value_at(config, key, REALITY_SETTING_DEFAULTS.get(key)) for key in keys}
    encoded = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _boolean(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "enabled", "开启", "启用"}:
            return True
        if normalized in {"false", "0", "no", "off", "disabled", "关闭", "停用"}:
            return False
    return None


def _bounded_int(value, minimum, maximum):
    if type(value) is bool or not isinstance(value, (int, str)):
        return None
    try:
        text = str(value).strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return None
        converted = int(text)
    except (TypeError, ValueError):
        return None
    return converted if minimum <= converted <= maximum else None


def convert_reality_configuration(source) -> RealityConfigurationMigration:
    """Convert only portable settings; identities, credentials, and device IDs stay put."""
    if not isinstance(source, dict):
        return RealityConfigurationMigration({}, (), ("V1 Reality 配置不是可识别的对象，本次没有迁移设置。",))

    changes: dict[str, Any] = {}
    differences: list[dict[str, str]] = []
    missing = object()
    raw = _value_at(source, "camera.enabled", missing)
    if raw is not missing:
        boolean = _boolean(raw)
        if boolean is None:
            differences.append({"field": "reality.camera.enabled", "kind": "invalid_value",
                                "explain": "旧摄像头开关不可识别，未迁移。"})
        else:
            changes["camera.enabled"] = boolean

    for target, (minimum, maximum) in REALITY_INTEGER_BOUNDS.items():
        raw = _value_at(source, target, missing)
        if raw is missing:
            continue
        converted = _bounded_int(raw, minimum, maximum)
        if converted is None:
            differences.append({
                "field": f"reality.{target}",
                "kind": "invalid_value",
                "explain": "V1 数值超出 V2 支持范围或格式无效，已跳过此项。",
            })
        else:
            changes[target] = converted

    if "timezone" in source:
        timezone = source["timezone"]
        try:
            if type(timezone) is not str or not timezone.strip() or len(timezone.strip()) > 80:
                raise ValueError()
            ZoneInfo(timezone.strip())
        except (ValueError, ZoneInfoNotFoundError):
            differences.append({"field": "reality.timezone", "kind": "invalid_value",
                                "explain": "旧时区在当前环境不可用，已跳过此项。"})
        else:
            changes["timezone"] = timezone.strip()

    if "vision_provider_id" in source:
        provider = source["vision_provider_id"]
        if type(provider) is str and len(provider.strip()) <= 160:
            if provider.strip():
                changes["vision_provider_id"] = provider.strip()
        else:
            differences.append({"field": "reality.vision_provider_id", "kind": "invalid_value",
                                "explain": "旧视觉模型选择不符合新版字段要求，未迁移。"})

    for path, _ in _leaves(source):
        if path in REALITY_SETTING_DEFAULTS:
            continue
        if path == "camera.index":
            kind, explain = "device_mapping_required", "摄像头索引由当前设备扫描确定，旧索引不会直接复用。"
        elif path == "camera.proactive_curiosity_enabled":
            kind, explain = "v2_confirmation_required", "主动摄像头需按当前角色及管理员授权确认；其频率偏好可以接续。"
        elif path == "authorized_user_ids" or path.endswith(("allowed_user_id", "admin_only", "allowed_devices")):
            kind, explain = "identity_rebinding_required", "身份、管理员权限与设备授权按新版身份确认，未扩大采集或控制权限。"
        elif path == "enabled" or path.startswith(("home.", "health.", "mobile.", "mihome.")):
            kind, explain = "manual_review_required", "连接凭据、配对、设备或采集开关需在对应新版页面确认；本次不启动外部服务。"
        else:
            kind, explain = "unmapped", "当前没有核实过的同义消费入口，旧配置保留并列入核对。"
        differences.append({"field": "reality." + path, "kind": kind, "explain": explain})

    return RealityConfigurationMigration(
        changes=changes,
        differences=tuple(differences),
        warnings=(
            "迁移摄像头、音量、时区及超时和有效期偏好；视觉模型仅在当前宿主兼容时接续，已有非默认设置保留。",
            "设备索引、授权、配对凭据、采集和外部服务开关需核对；提醒与历史数据不会随配置复制。",
        ),
    )


# V1 把真正在用的东西存在运行库 reality_companion.json，而不是插件配置：
# 选中的音响输出、摄像头设备目录、按用户记录的摄像头授权。配置迁移只搬了
# 可移植偏好，这些一直留在旧目录里，所以升级后页面看起来“全没迁过来”。
RUNTIME_AUDIO_FIELDS = ("audio_output_device_id", "audio_output_device_name")
RUNTIME_CONSENT_FIELDS = ("confirmed", "version", "confirmed_at", "confirmation_text", "granted_capabilities")


def merge_legacy_runtime_state(current, legacy, *, observed_at=None):
    """Copy V1 runtime records that V2 has no explicit value for.

    只补空，不覆盖：新版已经存在（用户重新选过设备、重新确认过授权）的记录一律保留，
    并把被保留的项写进报告。V1 的审计与最近一次观察属于历史，不复制，避免旧结果
    看起来像刚刚发生。
    """
    if not isinstance(current, dict) or not isinstance(legacy, dict):
        return current, ()
    # 深拷贝：调用方可能直接把自己的状态字典传进来，补字段时不能顺手改掉它的嵌套对象。
    state = deepcopy(current)
    report = []

    touch = state.get("reality_touch")
    if not isinstance(touch, dict):
        touch = {}
        state["reality_touch"] = touch
    source_touch = legacy.get("reality_touch") if isinstance(legacy.get("reality_touch"), dict) else {}
    for key in (*RUNTIME_AUDIO_FIELDS, "playback_volume", "camera_device_catalog"):
        value = source_touch.get(key)
        if value in (None, "", {}, []):
            continue
        if key == "playback_volume" and not (type(value) is int and 0 <= value <= 100):
            continue
        if touch.get(key) not in (None, "", {}, []):
            report.append({"field": f"reality.{key}", "kind": "kept_current",
                           "explain": "新版已有记录，保留当前值。"})
            continue
        touch[key] = value
        report.append({"field": f"reality.{key}", "kind": "copied", "explain": "沿用旧版运行记录。"})
    if observed_at is not None and "audio_output_updated_at" in source_touch:
        touch.setdefault("audio_output_updated_at", source_touch["audio_output_updated_at"])

    users = state.get("users")
    if not isinstance(users, dict):
        users = {}
        state["users"] = users
    legacy_users = legacy.get("users") if isinstance(legacy.get("users"), dict) else {}
    for user_id, record in legacy_users.items():
        if not isinstance(record, dict) or not isinstance(user_id, str) or not user_id:
            continue
        target = users.get(user_id)
        if not isinstance(target, dict):
            target = {"user_id": user_id}
            users[user_id] = target
        consent = record.get("reality_touch_camera_consent")
        if isinstance(consent, dict) and consent.get("confirmed") is True:
            if isinstance(target.get("reality_touch_camera_consent"), dict):
                report.append({"field": f"reality.users.{user_id}.camera_consent", "kind": "kept_current",
                               "explain": "新版已有授权记录，保留当前确认。"})
            else:
                target["reality_touch_camera_consent"] = {
                    name: consent[name] for name in RUNTIME_CONSENT_FIELDS if name in consent}
                report.append({"field": f"reality.users.{user_id}.camera_consent", "kind": "copied",
                               "explain": "沿用旧版已确认的摄像头单帧授权。"})
        policy = record.get("reality_touch_camera_policy")
        if isinstance(policy, dict) and type(policy.get("enabled")) is bool:
            target_policy = target.get("reality_touch_camera_policy")
            if not isinstance(target_policy, dict):
                target_policy = {}
                target["reality_touch_camera_policy"] = target_policy
            if "enabled" not in target_policy:
                target_policy["enabled"] = policy["enabled"]
                report.append({"field": f"reality.users.{user_id}.camera_policy.enabled", "kind": "copied",
                               "explain": "沿用旧版的摄像头开关；最近观察与审计不复制。"})
    return state, tuple(report)
