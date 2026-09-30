# Xavier_care/monitor.py
"""基于按天 JSON 的健康异常检测 + 冷却控制。

时区原则跟 health_logic.py 一致：不读服务器时钟，
一律以「已存数据里最新的那天」为基准往前数。
例外一：实时熬夜判定用 current_time（接收端写入的 HH:MM），
只用于「此刻是不是深夜」，不参与日期/过期判断。
例外二：经期「临近提醒」与「第一天提醒」用真实日历做新鲜度判断，
只决定要不要提醒，不写回数据、不参与归档与过期判断。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from . import health_logic


class HealthMonitor:
    def __init__(self, data_dir: Path, config, cooldown_path: Path):
        self.data_dir = data_dir
        self.config = config
        self.cooldown_path = cooldown_path
        self._cooldowns: dict[str, float] = self._load_cooldowns()

    # ---------- 冷却持久化 ----------
    def _load_cooldowns(self) -> dict:
        try:
            return json.loads(self.cooldown_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save_cooldowns(self) -> None:
        try:
            self.cooldown_path.write_text(
                json.dumps(self._cooldowns), encoding="utf-8"
            )
        except Exception:
            logger.exception("[Xavier_care] 写冷却状态失败")

    def _in_cooldown(self, key: str) -> bool:
        hours = float(self.config.get(f"cooldown_hours_{key}", 8))
        last = self._cooldowns.get(key, 0)
        return (datetime.now().timestamp() - last) < hours * 3600

    def _mark(self, key: str) -> None:
        self._cooldowns[key] = datetime.now().timestamp()
        self._save_cooldowns()

    # ---------- 读数据 ----------
    def _load_by_date(self, date_str: str) -> dict | None:
        return health_logic.load_by_date(self.data_dir, date_str)

    def _latest_date(self) -> str | None:
        dates = health_logic.list_dates(self.data_dir)
        return dates[-1] if dates else None

    def _load_today(self) -> dict | None:
        """取「最新一天」的数据（不读服务器时钟）。"""
        d = self._latest_date()
        return self._load_by_date(d) if d else None

    def _load_recent(self, days: int) -> list[dict]:
        """最新一天往前数 N 天（不含最新那天）的历史，用于算基线。"""
        dates = health_logic.list_dates(self.data_dir)
        if len(dates) < 2:
            return []
        newest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
        out = []
        for i in range(1, days + 1):
            d = (newest - timedelta(days=i)).isoformat()
            rec = self._load_by_date(d)
            if rec:
                out.append(rec)
        return out

    # ---------- 统计 ----------
    @staticmethod
    def _zscore(value, series: list[float]) -> float | None:
        if value is None or len(series) < 3:
            return None
        mean = sum(series) / len(series)
        var = sum((x - mean) ** 2 for x in series) / len(series)
        std = var ** 0.5
        if std == 0:
            return None
        return (value - mean) / std

    @staticmethod
    def _mean_std(series: list[float]) -> tuple[float, float] | None:
        """均值 + 标准差（总体标准差）。少于 2 个值返回 None。"""
        xs = [float(v) for v in series if v is not None]
        if len(xs) < 2:
            return None
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        return mean, var ** 0.5

    @staticmethod
    def _parse_hhmm(s: str) -> int | None:
        """把 'HH:MM' 解析成「一天中的第几分钟」。失败返回 None。"""
        try:
            h, m = str(s).strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            pass
        return None

    # ---------- 基线查看（给 /health base 用）----------
    def get_baseline(self) -> dict:
        """把当前基线算一遍返回，用于调试查看。"""
        days = int(self.config.get("baseline_days", 7))
        history = self._load_recent(days)

        out: dict = {
            "days": len(history),
            "range": "",
            "resting_hr": {},
            "sleep_min": {},
            "hrv_ms": {},
            "spo2": {},
        }
        if not history:
            return out

        dates = [h.get("date") for h in history if h.get("date")]
        if dates:
            out["range"] = f"{min(dates)} ~ {max(dates)}"

        def _collect(extractor):
            vals = [extractor(h) for h in history]
            return [v for v in vals if v is not None]

        specs = {
            "resting_hr": lambda h: h.get("resting_hr"),
            "sleep_min": lambda h: (h.get("sleep") or {}).get("duration_min"),
            "hrv_ms": lambda h: h.get("hrv_ms"),
            "spo2": lambda h: h.get("blood_oxygen_pct"),
        }
        for key, fn in specs.items():
            vals = _collect(fn)
            ms = self._mean_std(vals)
            if ms:
                out[key] = {"mean": round(ms[0], 2), "std": round(ms[1], 2), "n": len(vals)}
            else:
                out[key] = {"mean": None, "std": None, "n": len(vals)}

        return out

    # ---------- 检测 ----------
    def scan(self) -> list[dict]:
        alerts: list[dict] = []
        today = self._load_today()
        if not today:
            return alerts

        history = self._load_recent(int(self.config.get("baseline_days", 7)))
        baseline_ready = len(history) >= 3
        if not baseline_ready:
            logger.info(
                f"[Xavier_care] 基线天数不足({len(history)}/3)，"
                f"仅做绝对阈值检测（血氧 / 熬夜 / 入睡时间）"
            )

        # --- 需要基线的规则（z-score 类）---
        if baseline_ready and self.config.get("rule_resting_hr_high", True):
            self._check_resting_hr(today, history, alerts)
        if baseline_ready and self.config.get("rule_sleep_low", True):
            self._check_sleep(today, history, alerts)
        if baseline_ready and self.config.get("rule_hrv_low", False):
            self._check_hrv(today, history, alerts)

        # --- 绝对阈值规则（不依赖基线）---
        if self.config.get("rule_spo2_low", True):
            self._check_spo2(today, alerts)
        if self.config.get("rule_late_night", True):
            self._check_late_night(today, alerts)
        if self.config.get("rule_late_night_realtime", True):
            self._check_late_night_realtime(today, alerts)

        # --- 经期规则（由历史 in_period 标记推算，不依赖今天的传感器数据）---
        period = self._period_status()
        if self.config.get("rule_period_started", True):
            self._check_period_started(period, alerts)
        if self.config.get("rule_period_daily", True):
            self._check_period_daily(period, today, alerts)
        if self.config.get("rule_period_soon", True):
            self._check_period_soon(period, alerts)

        # --- 经期语境增强：不新增触发点，只让已命中的提示更贴合语境 ---
        self._apply_period_context(period, alerts)

        return alerts

    def _check_resting_hr(self, today, history, alerts):
        val = today.get("resting_hr")
        series = [r.get("resting_hr") for r in history if r.get("resting_hr")]
        z = self._zscore(val, series)
        if z is not None and z >= float(self.config.get("resting_hr_z", 2.0)):
            mean = sum(series) / len(series)
            alerts.append({
                "type": "resting_hr_high",
                "cooldown_key": "resting_hr_high",
                "hint": (
                    f"她今天的静息心率 {val:.0f} bpm，"
                    f"比最近 {len(series)} 天平均（{mean:.0f} bpm）偏高不少。"
                ),
            })

    def _check_spo2(self, today, alerts):
        val = today.get("blood_oxygen_pct")
        if val is None:
            return
        threshold = float(self.config.get("spo2_threshold", 95))
        if val < threshold:
            alerts.append({
                "type": "spo2_low",
                "cooldown_key": "spo2_low",
                "hint": f"她今天的血氧只有 {val:.0f}%，低于平时，可能有点累或没休息好。",
            })

    def _check_sleep(self, today, history, alerts):
        sleep = today.get("sleep") or {}
        dur = sleep.get("duration_min")
        if not dur:
            return
        dur = int(dur)
        series = [
            int(r["sleep"]["duration_min"])
            for r in history
            if (r.get("sleep") or {}).get("duration_min")
        ]
        if len(series) < 3:
            return
        mean = sum(series) / len(series)
        if mean - dur >= float(self.config.get("sleep_short_min", 90)):
            alerts.append({
                "type": "sleep_short",
                "cooldown_key": "sleep_short",
                "hint": (
                    f"她昨晚只睡了 {dur // 60} 小时 {dur % 60} 分，"
                    f"比平时少了大约 {(mean - dur) / 60:.1f} 小时。"
                ),
            })

    def _check_late_night(self, today, alerts):
        """基于 HAE 睡眠汇总的入睡时间判定（次日复盘用）。"""
        sleep = today.get("sleep") or {}
        bedtime = sleep.get("bedtime")
        if not bedtime:
            return
        minutes = self._parse_hhmm(bedtime)
        if minutes is None:
            return
        threshold = int(self.config.get("late_night_bedtime_min", 30))
        is_late = (minutes < 300 and minutes > threshold) or (minutes >= 23 * 60)
        if is_late:
            hh, mm = divmod(minutes, 60)
            alerts.append({
                "type": "late_night",
                "cooldown_key": "late_night",
                "hint": f"她昨晚 {hh:02d}:{mm:02d} 才睡，熬得有点晚。",
            })

    def _check_late_night_realtime(self, today, alerts):
        """实时熬夜判定：看「接收端写入的当前时刻」是否在深夜窗口，且心率偏高。"""
        cur = today.get("current_time")
        latest = today.get("latest_hr")
        if not cur or latest is None:
            return

        minutes = self._parse_hhmm(cur)
        if minutes is None:
            return

        start = self._parse_hhmm(str(self.config.get("late_night_start_time", "01:00")))
        end = self._parse_hhmm(str(self.config.get("late_night_end_time", "05:00")))
        if start is None or end is None:
            return
        if start <= end:
            in_window = start <= minutes < end
        else:
            in_window = minutes >= start or minutes < end
        if not in_window:
            return

        resting = today.get("resting_hr")
        if not resting:
            history = self._load_recent(int(self.config.get("baseline_days", 7)))
            series = [r.get("resting_hr") for r in history if r.get("resting_hr")]
            if series:
                resting = sum(series) / len(series)
        if not resting:
            return

        ratio_cfg = float(self.config.get("late_night_hr_ratio", 1.15))
        try:
            awake = float(latest) > float(resting) * ratio_cfg
        except (TypeError, ValueError):
            return
        if not awake:
            return

        hh, mm = divmod(minutes, 60)
        alerts.append({
            "type": "late_night_realtime",
            "cooldown_key": "late_night_realtime",
            "hint": (
                f"现在是 {hh:02d}:{mm:02d}，她还没睡，"
                f"心率 {int(latest)} bpm（静息 {int(resting)}），人还醒着。"
            ),
        })

    def _check_hrv(self, today, history, alerts):
        val = today.get("hrv_ms")
        series = [r.get("hrv_ms") for r in history if r.get("hrv_ms")]
        z = self._zscore(val, series)
        if z is not None and z <= -float(self.config.get("hrv_z", 1.5)):
            alerts.append({
                "type": "hrv_low",
                "cooldown_key": "hrv_low",
                "hint": (
                    f"她今天的 HRV 只有 {val:.0f} ms，"
                    f"比最近平均偏低，身体可能有点疲劳。"
                ),
            })

    # ---------- 经期 ----------
    def _period_status(self) -> dict | None:
        """推算经期状态。失败或数据不足返回 None，不影响其他规则。"""
        try:
            return health_logic.compute_period_status(self.data_dir)
        except Exception:
            logger.exception("[Xavier_care] 推算经期状态失败，本次跳过经期规则")
            return None

    def _data_lag_days(self) -> int | None:
        """最新数据日期距真实今天几天；无数据/格式异常返回 None。"""
        latest = self._latest_date()
        if not latest:
            return None
        try:
            d = datetime.strptime(latest, "%Y-%m-%d").date()
        except ValueError:
            return None
        return (datetime.now().date() - d).days

    def _check_period_started(self, period, alerts):
        """今天是这次经期的第一天。"""
        if not period:
            return
        if not period.get("in_period") or period.get("period_day") != 1:
            return

        # 陈旧保险：只有「今天」的数据就是最新一天时才提醒，
        # 否则插件离线几天后重启，会对着一份旧数据说「今天是第一天」。
        lag = self._data_lag_days()
        if lag is None or not 0 <= lag <= 1:
            return

        alerts.append({
            "type": "period_started",
            "cooldown_key": "period_started",
            "hint": "她今天是经期第一天，身体容易累、肚子可能不舒服，特别怕凉，提醒她温水备好、别硬撑。",
        })

    def _check_period_daily(self, period, today, alerts):
        """经期日常体贴关怀（覆盖经期第 1~N 天）。

        - 每天最多关怀 1 次（按天/24小时冷却）。
        - 结合当天步数：如果步数较高（例如 >= 7000），体现心疼她走太多路、让她躺好歇着。
        - 否则结合天数进行温馨关怀（如：今天第 X 天了，不许碰凉的，温水放身边）。
        """
        if not period or not period.get("in_period"):
            return

        lag = self._data_lag_days()
        if lag is None or not 0 <= lag <= 1:
            return

        period_day = period.get("period_day") or 1
        steps = today.get("steps")

        high_step_thresh = int(self.config.get("period_daily_step_threshold", 7000))
        if steps is not None and steps >= high_step_thresh:
            hint = (
                f"她正在经期第 {period_day} 天，而且今天已经走了 {steps} 步，"
                f"身体容易酸胀疲倦，提醒她快坐下或躺着歇歇，别走那么多路，揉揉腰。"
            )
        else:
            hint = (
                f"她正在经期第 {period_day} 天，"
                f"顺口叮嘱她温水常备，凉的放旁边晾着等放常温再喝，别碰冰的，多注意休息。"
            )

        alerts.append({
            "type": "period_daily",
            "cooldown_key": "period_daily",
            "hint": hint,
        })

    def _check_period_soon(self, period, alerts):
        """距预测经期还剩几天。

        这里刻意用真实日历（服务器时钟）算「还剩几天」，而不是用最新
        数据日期：手机断更时数据日期会滞后，用它必然漏提醒。这条只
        决定要不要提醒，不写回数据，也不参与归档/过期判断。
        """
        if not period:
            return
        if period.get("in_period"):
            return

        # 至少要有两次经期段，平均周期才有依据；
        # 只有一次时 next_start_date 只是拿默认周期推的，不可信。
        if int(period.get("period_count") or 0) < 2:
            return

        next_start = period.get("next_start_date")
        if not next_start:
            return
        try:
            next_d = datetime.strptime(next_start, "%Y-%m-%d").date()
        except ValueError:
            return

        left = (next_d - datetime.now().date()).days
        window = int(self.config.get("period_soon_days", 2))
        if not 1 <= left <= window:
            return

        # 陈旧保险：数据太旧就不提醒，避免她已经来了、我们还在说「快来了」。
        lag = self._data_lag_days()
        stale_limit = int(self.config.get("period_soon_max_stale_days", 5))
        if lag is None or not 0 <= lag <= stale_limit:
            logger.info(
                f"[Xavier_care] 经期临近提醒跳过：最新数据距今 {lag} 天，"
                f"超出陈旧上限 {stale_limit} 天"
            )
            return

        alerts.append({
            "type": "period_soon",
            "cooldown_key": "period_soon",
            "hint": (
                f"按她的周期推算，大约还有 {left} 天来经期，"
                f"可以提醒她提前备着点、别贪凉。"
            ),
        })

    def _apply_period_context(self, period, alerts) -> None:
        """经期语境增强：已命中其他规则时，补一句经期状态。

        只改措辞、不新增触发点，因此不会带来任何额外误报。
        """
        if not alerts or not period:
            return
        day = period.get("period_day") if period.get("in_period") else None
        if not day:
            return
        tag = f"（她正在经期第 {day} 天）"
        for a in alerts:
            # 经期规则自己的提示不必再叠一层经期语境
            if str(a.get("type", "")).startswith("period_"):
                continue
            hint = a.get("hint")
            if isinstance(hint, str) and not hint.startswith(tag):
                a["hint"] = tag + hint