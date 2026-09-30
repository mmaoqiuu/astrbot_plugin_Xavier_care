"""Xavier_care 主动关怀推送闸门。

把「这一刻到底该不该发这条关怀」从 main.py 的轮询里抽出来，独立成一个
可以脱离 AstrBot 直接跑 pytest 的纯逻辑模块。

四道闸门，按顺序判定，任一条不过本轮就不发：
  1. 簇冷却   同一类异常在 N 小时内只推一条（取代 v1.8.0 的 9 个单规则冷却）
  2. 每日上限 每天最多推 daily_push_limit 条（经期簇不计入额度）
  3. 最小间隔 两条关怀之间至少隔 min_push_interval_minutes 分钟
  4. 对话互斥 最近 dialog_guard_minutes 分钟内有人机往来就不插播，
              避免「她刚说完话机器人立刻另起一句」以及由此引发的自相矛盾

设计取舍：
  - 只有推送「成功」才记账；被闸门拦下不记账、不消耗冷却，下个周期还能补推。
  - 时间统一用 time.time()（epoch 秒），跨天按本地日期重置每日计数。
  - 状态文件原子写入（临时文件 + os.replace），进程被强杀也不会写坏 JSON。
  - 任何一步判断出错都当作「放行」：宁可多发一条，也不要静默失效。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

# 异常类型 → 簇。新增检测规则时必须在这里登记，
# 否则会按 "other:<type>" 单独成簇（不会崩，但那份异常的冷却就独立了）。
CLUSTER_MAP: dict[str, str] = {
    # 睡眠：睡得晚、睡得少，本质是同一件事
    "sleep_short": "sleep",
    "late_night": "sleep",
    # 疲劳：身体恢复指标异常
    "hrv_low": "fatigue",
    "resting_hr_high": "fatigue",
    "spo2_low": "fatigue",
    # 夜间实时：对「此刻还没睡」的即时干预，与次日复盘性质不同，单独一簇
    "late_night_realtime": "night",
    # 经期：正在经期中的关怀
    "period_started": "period",
    "period_daily": "period",
    # 经期预测：倒计时提醒，容易连着好几天触发，冷却要给得长
    "period_soon": "forecast",
}

# 各簇默认冷却（小时），配置项 group_cooldown_hours_<簇名> 可覆盖。
DEFAULT_CLUSTER_COOLDOWN: dict[str, float] = {
    "sleep": 12.0,
    "fatigue": 12.0,
    "night": 2.0,
    "period": 20.0,
    "forecast": 168.0,
}

CLUSTER_LABEL: dict[str, str] = {
    "sleep": "睡眠",
    "fatigue": "疲劳",
    "night": "夜间实时",
    "period": "经期",
    "forecast": "经期预测",
}

# 这些簇的推送不占「每日上限」额度：她每天总该被惦记一下。
# 注意：这只是「额度豁免」；经期每天到底推不推这一条，由开关
# period_daily_care（在 monitor 层判定，见 monitor._check_period_daily 上游）决定。
UNMETERED_CLUSTERS: frozenset[str] = frozenset({"period"})

# 判定「机器人回复算不算对话活跃」的窗口（秒）：
# 只有紧跟在用户发言之后的回复才算，免得定时唤醒之类的主动消息把闸门永久锁死。
REPLY_IS_DIALOG_SECONDS = 300


def cluster_of(alert_type: str) -> str:
    """把异常类型映射到簇名，未知类型兜底成独立簇。"""
    return CLUSTER_MAP.get(alert_type) or f"other:{alert_type}"


class CareGuard:
    """关怀推送闸门。状态存在 plugin_data/<插件目录>/_care_guard.json。"""

    def __init__(self, data_dir: Path, config):
        self.data_dir = data_dir
        self.config = config
        self.state_path = Path(data_dir) / "_care_guard.json"
        self._state: dict = self._load()

    # ---------- 配置读取（都带兜底，配置缺失不影响运行） ----------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def cluster_cooldown_hours(self, cluster: str) -> float:
        default = DEFAULT_CLUSTER_COOLDOWN.get(cluster, 12.0)
        try:
            return float(self.config.get(f"group_cooldown_hours_{cluster}", default))
        except (TypeError, ValueError):
            return default

    # ---------- 状态持久化 ----------
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return self._load_legacy_cooldowns()
        except Exception:
            logger.exception("[Xavier_care] 读推送闸门状态失败，已按空状态继续")
            return {}

    def _load_legacy_cooldowns(self) -> dict:
        """从 v1.8.0 的 _cooldowns.json 平移一次：同簇取最近的时间戳。

        平移结果不落盘，等下一次真正推送时随 mark() 一起写入新文件，
        这样即便平移逻辑有问题也不会污染新状态。
        """
        try:
            raw = json.loads((self.data_dir / "_cooldowns.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("[Xavier_care] 读旧冷却文件失败，已跳过平移")
            return {}
        if not isinstance(raw, dict):
            return {}
        clusters: dict[str, float] = {}
        for key, ts in raw.items():
            try:
                stamp = float(ts)
            except (TypeError, ValueError):
                continue
            cluster = cluster_of(str(key))
            clusters[cluster] = max(clusters.get(cluster, 0.0), stamp)
        if not clusters:
            return {}
        logger.info(
            f"[Xavier_care] 已从旧冷却文件平移 {len(clusters)} 个簇冷却：{sorted(clusters)}"
        )
        return {"clusters": clusters, "last_push_ts": max(clusters.values())}

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except Exception:
            logger.exception("[Xavier_care] 写推送闸门状态失败（不影响本次推送）")

    @staticmethod
    def _date_key(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    # ---------- 活跃度记录 ----------
    def record_user_message(self, ts: float | None = None) -> None:
        """记录一次真实的用户发言（注入的伪消息不要传进来）。"""
        self._state["last_user_ts"] = float(ts if ts is not None else time.time())
        self._save()

    def record_bot_reply(self, ts: float | None = None) -> None:
        """记录一次机器人发送。只把「对话中的回复」算作活跃。"""
        now = float(ts if ts is not None else time.time())
        last_user = float(self._state.get("last_user_ts", 0) or 0)
        if not last_user or (now - last_user) > REPLY_IS_DIALOG_SECONDS:
            return
        self._state["last_reply_ts"] = now
        self._save()

    def _last_activity(self) -> float:
        return max(
            float(self._state.get("last_user_ts", 0) or 0),
            float(self._state.get("last_reply_ts", 0) or 0),
        )

    # ---------- 闸门判定 ----------
    def check(self, alert_type: str, now: float | None = None) -> tuple[bool, str]:
        """返回 (是否可以推送, 拦截原因)。"""
        if not self._cfg_bool("care_guard_enabled", True):
            return True, ""
        now = float(now if now is not None else time.time())
        try:
            return self._check_inner(alert_type, now)
        except Exception:
            # 判定逻辑出错时放行，避免关怀功能静默失效
            logger.exception("[Xavier_care] 推送闸门判定异常，本次放行")
            return True, ""

    def _check_inner(self, alert_type: str, now: float) -> tuple[bool, str]:
        cluster = cluster_of(alert_type)

        # 1. 簇冷却
        hours = self.cluster_cooldown_hours(cluster)
        last = float((self._state.get("clusters") or {}).get(cluster, 0) or 0)
        if hours > 0 and last and (now - last) < hours * 3600:
            left = hours - (now - last) / 3600
            return False, f"「{CLUSTER_LABEL.get(cluster, cluster)}」簇冷却中，还剩 {left:.1f} 小时"

        # 2. 每日上限（经期簇不占额度）
        if cluster not in UNMETERED_CLUSTERS:
            limit = self._cfg_int("daily_push_limit", 2)
            if limit > 0 and self._today_count(now) >= limit:
                return False, f"今天已经推了 {limit} 条，达每日上限"

        # 3. 最小间隔
        gap = self._cfg_int("min_push_interval_minutes", 180)
        last_push = float(self._state.get("last_push_ts", 0) or 0)
        if gap > 0 and last_push and (now - last_push) < gap * 60:
            wait = gap - (now - last_push) / 60
            return False, f"距上一条关怀不到 {gap} 分钟，还需等 {wait:.0f} 分钟"

        # 4. 对话互斥
        win = self._cfg_int("dialog_guard_minutes", 5)
        if win > 0:
            act = self._last_activity()
            if act and (now - act) < win * 60:
                return False, f"最近 {(now - act) / 60:.1f} 分钟有人在聊，先不插话"

        return True, ""

    def _today_count(self, now: float) -> int:
        daily = self._state.get("daily") or {}
        if daily.get("date") != self._date_key(now):
            return 0
        try:
            return int(daily.get("count", 0))
        except (TypeError, ValueError):
            return 0

    def mark(self, alert_type: str, now: float | None = None) -> None:
        """推送成功后记账：簇时间戳 + 当日计数 + 本次推送时间。"""
        now = float(now if now is not None else time.time())
        cluster = cluster_of(alert_type)
        today = self._date_key(now)

        clusters = self._state.setdefault("clusters", {})
        clusters[cluster] = now

        daily = self._state.get("daily") or {}
        if daily.get("date") != today:
            daily = {"date": today, "count": 0}
        if cluster not in UNMETERED_CLUSTERS:
            try:
                count = int(daily.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            daily["count"] = count + 1
        daily.setdefault("date", today)
        self._state["daily"] = daily

        self._state["last_push_ts"] = now
        self._state["last_alert_type"] = alert_type
        self._save()

    def is_cluster_cooling(self, alert_type: str, now: float | None = None) -> bool:
        """给 /health scan 展示用：这条异常当前是否处在簇冷却里。"""
        cluster = cluster_of(alert_type)
        hours = self.cluster_cooldown_hours(cluster)
        last = float((self._state.get("clusters") or {}).get(cluster, 0) or 0)
        now = float(now if now is not None else time.time())
        return bool(hours > 0 and last and (now - last) < hours * 3600)

    # ---------- 展示 ----------
    def status_lines(self, now: float | None = None) -> list[str]:
        """/health status 用的几行状态。"""
        now = float(now if now is not None else time.time())
        enabled = self._cfg_bool("care_guard_enabled", True)
        lines = [
            "[推送闸门]",
            f"  总开关   : {'✅ 开' if enabled else '❌ 关（不拦）'}",
            f"  每日上限 : {self._cfg_int('daily_push_limit', 2)} 条"
            f"（今天已推 {self._today_count(now)} 条，经期不占额度）",
            f"  最小间隔 : {self._cfg_int('min_push_interval_minutes', 180)} 分钟",
            f"  对话互斥 : 最近 {self._cfg_int('dialog_guard_minutes', 5)} 分钟有人聊就跳过",
        ]
        last_push = float(self._state.get("last_push_ts", 0) or 0)
        if last_push:
            lines.append(
                f"  上次推送 : {datetime.fromtimestamp(last_push):%m-%d %H:%M}"
                f"（{self._state.get('last_alert_type', '')}）"
            )
        else:
            lines.append("  上次推送 : 还没有记录")
        act = self._last_activity()
        if act:
            lines.append(f"  最近往来 : {datetime.fromtimestamp(act):%H:%M:%S}")
        return lines

    def cluster_status(self, now: float | None = None) -> str:
        """各簇冷却剩余，一屏看完。"""
        now = float(now if now is not None else time.time())
        parts: list[str] = []
        for cluster in DEFAULT_CLUSTER_COOLDOWN:
            hours = self.cluster_cooldown_hours(cluster)
            last = float((self._state.get("clusters") or {}).get(cluster, 0) or 0)
            label = CLUSTER_LABEL.get(cluster, cluster)
            if not last:
                parts.append(f"{label}({hours:.0f}h): 未推过")
                continue
            left = hours - (now - last) / 3600
            if left > 0:
                parts.append(f"{label}({hours:.0f}h): 还剩 {left:.1f}h")
            else:
                parts.append(f"{label}({hours:.0f}h): 已就绪")
        return "  ".join(parts)
