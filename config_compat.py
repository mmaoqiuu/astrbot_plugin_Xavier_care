"""配置分组兼容层。

面板上的配置现在是嵌套结构（基础 / 接收 / 闸门 / 规则 / 聊天 / 镜像 / 维护），
但业务代码仍沿用原来的平铺键名。这个模块负责两件事：

1. 把旧版平铺配置迁移进新分组，迁移前留一份备份；
2. 提供一个只读优先、写入回分组的代理，让 ``config.get("check_interval")``
   这类旧写法继续可用。

迁移只执行一次，靠顶层 ``_config_version`` 标记。旧键在 schema 里保留为
invisible，避免 AstrBot 在加载时把旧版自定义值当成未知键删掉。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 分组 -> 该组包含的键。顺序同时决定配置面板里的字段顺序。
GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "basic": (
        "monitor_enabled",
        "care_in_chat_enabled",
        "check_interval",
        "quiet_hours_start",
        "quiet_hours_end",
        "fallback_care",
    ),
    "receive": (
        "listen_port",
        "auth_token",
        "push_target_umo",
        "retention_days",
    ),
    "guard": (
        "care_guard_enabled",
        "daily_push_limit",
        "min_push_interval_minutes",
        "dialog_guard_minutes",
        "group_cooldown_hours_sleep",
        "group_cooldown_hours_fatigue",
        "group_cooldown_hours_night",
        "group_cooldown_hours_period",
        "group_cooldown_hours_forecast",
        "group_cooldown_hours_hr",
    ),
    "rules": (
        "baseline_days",
        "rule_resting_hr_high",
        "resting_hr_z",
        "rule_sleep_low",
        "sleep_short_min",
        "rule_hrv_low",
        "hrv_z",
        "rule_spo2_low",
        "spo2_threshold",
        "hr_spike_enabled",
        "rule_late_night",
        "late_night_bedtime_min",
        "rule_late_night_realtime",
        "late_night_start_time",
        "late_night_end_time",
        "late_night_hr_ratio",
        "rule_period_started",
        "period_daily_care",
        "period_daily_step_threshold",
        "rule_period_soon",
        "period_soon_days",
        "period_soon_max_stale_days",
    ),
    "chat": (
        "chat_hint_max_items",
        "chat_hint_ttl_hours",
        "chat_hint_dedup_push",
    ),
    "mirror": (
        "mirror_enabled",
        "mirror_name",
        "mirror_target_umo",
        "mirror_decay_minutes",
        "mirror_emotion_gap_minutes",
        "mirror_daily_emotion_points",
        "mirror_chat_cooldown_minutes",
        "mirror_auto_rest_hours",
        "mirror_auto_rest_recent_guard_minutes",
        "mirror_data_resonance_enabled",
        "mirror_data_whisper_enabled",
        "mirror_data_debounce_minutes",
        "mirror_data_whisper_cooldown_minutes",
    ),
    "misc": ("care_log_enabled",),
}

KEY_TO_PATH: dict[str, tuple[str, str]] = {
    key: (group, key)
    for group, keys in GROUP_FIELDS.items()
    for key in keys
}

# 旧键名 -> 新键名。只用于兼容读取和一次性迁移。
ALIASES: dict[str, str] = {
    "quiet_start": "quiet_hours_start",
    "quiet_end": "quiet_hours_end",
    "rule_period_daily": "period_daily_care",
}

# 旧版默认值。迁移时只复制「和默认值不同」的项，避免把旧默认值覆盖到
# 用户刚在新分组里改过的值上。
DEFAULTS: dict[str, Any] = {
    "monitor_enabled": True,
    "care_in_chat_enabled": True,
    "check_interval": 900,
    "quiet_hours_start": 23,
    "quiet_hours_end": 7,
    "fallback_care": "怎么又不舒服了？过来，跟我说说。",
    "listen_port": 8787,
    "auth_token": "",
    "push_target_umo": "",
    "retention_days": 180,
    "care_guard_enabled": True,
    "daily_push_limit": 2,
    "min_push_interval_minutes": 180,
    "dialog_guard_minutes": 5,
    "group_cooldown_hours_sleep": 12.0,
    "group_cooldown_hours_fatigue": 12.0,
    "group_cooldown_hours_night": 2.0,
    "group_cooldown_hours_period": 20.0,
    "group_cooldown_hours_forecast": 168.0,
    "group_cooldown_hours_hr": 12.0,
    "baseline_days": 7,
    "rule_resting_hr_high": True,
    "resting_hr_z": 2.0,
    "rule_sleep_low": True,
    "sleep_short_min": 90,
    "rule_hrv_low": False,
    "hrv_z": 1.5,
    "rule_spo2_low": True,
    "spo2_threshold": 95,
    "hr_spike_enabled": False,
    "rule_late_night": True,
    "late_night_bedtime_min": 30,
    "rule_late_night_realtime": True,
    "late_night_start_time": "01:00",
    "late_night_end_time": "05:00",
    "late_night_hr_ratio": 1.15,
    "rule_period_started": True,
    "period_daily_care": True,
    "period_daily_step_threshold": 7000,
    "rule_period_soon": True,
    "period_soon_days": 2,
    "period_soon_max_stale_days": 5,
    "chat_hint_max_items": 2,
    "chat_hint_ttl_hours": 6.0,
    "chat_hint_dedup_push": True,
    "mirror_enabled": True,
    "mirror_name": "沈星回",
    "mirror_target_umo": "sxh:FriendMessage:3800806724",
    "mirror_decay_minutes": 25,
    "mirror_emotion_gap_minutes": 5,
    "mirror_daily_emotion_points": 20,
    "mirror_chat_cooldown_minutes": 20,
    "mirror_auto_rest_hours": 2,
    "mirror_auto_rest_recent_guard_minutes": 30,
    "mirror_data_resonance_enabled": True,
    "mirror_data_whisper_enabled": False,
    "mirror_data_debounce_minutes": 15,
    "mirror_data_whisper_cooldown_minutes": 40,
    "care_log_enabled": True,
    # 旧别名默认值
    "quiet_start": 23,
    "quiet_end": 7,
    "rule_period_daily": True,
}

CONFIG_VERSION = "2"


def _nested_default(key: str) -> Any:
    target = ALIASES.get(key, key)
    return DEFAULTS.get(target)


def _backup_config(raw: Any) -> None:
    """迁移前把旧配置文件复制一份；失败不影响启动。"""
    try:
        path = Path(str(getattr(raw, "config_path", "") or ""))
        if not path.is_file():
            return
        backup = path.with_name(path.name + ".v1.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    except Exception:
        logger.warning("[Xavier_care] 配置备份失败，继续迁移", exc_info=True)


def _copy_legacy_values(raw: Any) -> None:
    for legacy_key, target_key in {**{k: k for k in KEY_TO_PATH}, **ALIASES}.items():
        if legacy_key not in raw:
            continue
        value = raw.get(legacy_key)
        if value is None or value == DEFAULTS.get(legacy_key):
            continue

        group, subkey = KEY_TO_PATH.get(target_key, ("", ""))
        if not group:
            continue
        section = raw.get(group)
        if not isinstance(section, dict):
            section = {}
            raw[group] = section

        # 新分组里已经是非默认值，说明用户已经改过，优先保留新值。
        if subkey in section and section[subkey] != _nested_default(target_key):
            continue
        section[subkey] = value


def migrate_and_wrap(raw: Any) -> "GroupedConfig":
    """迁移旧配置并返回兼容代理；重复调用不会重复迁移。"""
    if str(raw.get("_config_version") or "") != CONFIG_VERSION:
        # 全新配置或旧值都等于新默认值时，没有可迁移内容，不留无意义备份。
        changed = any(
            key in raw and raw.get(key) != value
            for key, value in DEFAULTS.items()
        )
        if changed:
            _backup_config(raw)
        _copy_legacy_values(raw)
        raw["_config_version"] = CONFIG_VERSION
        try:
            raw.save_config()
        except Exception:
            logger.warning("[Xavier_care] 配置迁移写回失败，本次仍按新结构运行", exc_info=True)
    return GroupedConfig(raw)


class GroupedConfig:
    """把嵌套配置伪装成原来的平铺配置。"""

    def __init__(self, raw: Any):
        object.__setattr__(self, "_raw", raw)

    def _lookup(self, key: str) -> tuple[Any, bool]:
        target = ALIASES.get(key, key)
        path = KEY_TO_PATH.get(target)
        if path:
            group, subkey = path
            section = self._raw.get(group)
            if isinstance(section, dict) and subkey in section:
                return section[subkey], True
        if target in self._raw:
            return self._raw[target], True
        if key in self._raw:
            return self._raw[key], True
        return None, False

    def get(self, key: str, default: Any = None) -> Any:
        value, found = self._lookup(key)
        return value if found else default

    def __getitem__(self, key: str) -> Any:
        value, found = self._lookup(key)
        if not found:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        target = ALIASES.get(key, key)
        path = KEY_TO_PATH.get(target)
        if not path:
            self._raw[key] = value
            return
        group, subkey = path
        section = self._raw.get(group)
        if not isinstance(section, dict):
            section = {}
            self._raw[group] = section
        section[subkey] = value

    def __contains__(self, key: str) -> bool:
        _, found = self._lookup(str(key))
        return found

    def __iter__(self):
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def keys(self):
        return self._raw.keys()

    def items(self):
        return self._raw.items()

    def values(self):
        return self._raw.values()

    def update(self, *args, **kwargs) -> None:
        self._raw.update(*args, **kwargs)

    def setdefault(self, key: str, default: Any = None) -> Any:
        value, found = self._lookup(key)
        if found:
            return value
        self[key] = default
        return default

    def save_config(self, *args, **kwargs):
        return self._raw.save_config(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def __repr__(self) -> str:
        return f"GroupedConfig({self._raw!r})"
