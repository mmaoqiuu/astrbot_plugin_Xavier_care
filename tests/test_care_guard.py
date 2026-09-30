"""care_guard 推送闸门的单元测试。

只依赖标准库 + pytest，不需要 AstrBot 运行时。
运行： python -m pytest tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from care_guard import CareGuard, cluster_of  # noqa: E402


class FakeConfig(dict):
    """够用的配置桩：CareGuard 只用 .get(key, default)。"""

    def get(self, key, default=None):  # noqa: D102
        return dict.get(self, key, default)


def make_guard(tmp_path: Path, **cfg) -> CareGuard:
    """默认关掉最小间隔与对话互斥，免得它们掩盖被测闸门本身。"""
    base = {"min_push_interval_minutes": 0, "dialog_guard_minutes": 0}
    base.update(cfg)
    return CareGuard(data_dir=tmp_path, config=FakeConfig(**base))


T0 = 1_780_000_000.0  # 固定时间基准，避免测试抖动


# ---------- 簇映射 ----------
def test_cluster_of_known_and_unknown():
    assert cluster_of("sleep_short") == "sleep"
    assert cluster_of("late_night") == "sleep"      # 同一簇
    assert cluster_of("spo2_low") == "fatigue"
    assert cluster_of("period_soon") == "forecast"
    assert cluster_of("brand_new_rule") == "other:brand_new_rule"


# ---------- 簇冷却 ----------
def test_same_cluster_blocked_other_cluster_allowed(tmp_path):
    g = make_guard(tmp_path)
    assert g.check("sleep_short", now=T0)[0] is True
    g.mark("sleep_short", now=T0)

    ok, reason = g.check("late_night", now=T0 + 60)
    assert ok is False and "冷却" in reason        # 同簇被拦
    assert g.check("spo2_low", now=T0 + 60)[0] is True  # 他簇不受影响
    assert g.check("late_night", now=T0 + 13 * 3600)[0] is True  # 12h 后放行


def test_night_cluster_short_cooldown(tmp_path):
    g = make_guard(tmp_path)
    g.mark("late_night_realtime", now=T0)
    assert g.check("late_night_realtime", now=T0 + 3600)[0] is False   # 1h 内
    assert g.check("late_night_realtime", now=T0 + 3 * 3600)[0] is True  # 2h 后


def test_cluster_hours_read_from_config(tmp_path):
    g = make_guard(tmp_path, group_cooldown_hours_sleep=1)
    g.mark("sleep_short", now=T0)
    assert g.check("late_night", now=T0 + 1800)[0] is False
    assert g.check("late_night", now=T0 + 4000)[0] is True


# ---------- 每日上限 ----------
def test_daily_limit_blocks_after_quota(tmp_path):
    g = make_guard(tmp_path, daily_push_limit=1)
    g.mark("sleep_short", now=T0)
    ok, reason = g.check("spo2_low", now=T0 + 60)
    assert ok is False and "上限" in reason
    # 跨天自动重置
    assert g.check("spo2_low", now=T0 + 24 * 3600)[0] is True


def test_period_push_is_unmetered(tmp_path):
    g = make_guard(tmp_path, daily_push_limit=1)
    g.mark("period_daily", now=T0)
    assert g.check("spo2_low", now=T0 + 60)[0] is True   # 经期不占额度
    g.mark("spo2_low", now=T0 + 60)
    assert g.check("hrv_low", now=T0 + 120)[0] is False  # 但非经期要占


# ---------- 最小间隔 ----------
def test_min_push_interval(tmp_path):
    g = make_guard(tmp_path, min_push_interval_minutes=180)
    g.mark("sleep_short", now=T0)
    ok, reason = g.check("spo2_low", now=T0 + 60 * 60)
    assert ok is False and "分钟" in reason
    assert g.check("spo2_low", now=T0 + 200 * 60)[0] is True


# ---------- 对话互斥 ----------
def test_dialog_guard_blocks_after_user_message(tmp_path):
    g = make_guard(tmp_path, dialog_guard_minutes=5)
    g.record_user_message(ts=T0)
    ok, reason = g.check("sleep_short", now=T0 + 60)
    assert ok is False and "聊" in reason
    assert g.check("sleep_short", now=T0 + 6 * 60)[0] is True


def test_bot_reply_only_counts_inside_dialog(tmp_path):
    g = make_guard(tmp_path, dialog_guard_minutes=5)
    # 她说话后 60 秒内机器人回复 → 算对话活跃
    g.record_user_message(ts=T0)
    g.record_bot_reply(ts=T0 + 60)
    assert g.check("sleep_short", now=T0 + 120)[0] is False
    # 半小时后（定时唤醒之类的主动消息）→ 不算对话活跃，闸门照常放行
    g2 = make_guard(tmp_path / "b", dialog_guard_minutes=5)
    g2.record_user_message(ts=T0)
    g2.record_bot_reply(ts=T0 + 1800)
    assert g2.check("sleep_short", now=T0 + 1860)[0] is True


def test_guard_switch_off_means_allow_all(tmp_path):
    g = make_guard(tmp_path, care_guard_enabled=False)
    g.mark("sleep_short", now=T0)
    g.record_user_message(ts=T0)
    assert g.check("sleep_short", now=T0)[0] is True


# ---------- 持久化与健壮性 ----------
def test_state_persists_across_instances(tmp_path):
    g1 = make_guard(tmp_path)
    g1.mark("sleep_short", now=T0)
    g2 = make_guard(tmp_path)
    assert g2.check("sleep_short", now=T0 + 60)[0] is False


def test_broken_state_file_does_not_crash(tmp_path):
    (tmp_path / "_care_guard.json").write_text("{ not json", encoding="utf-8")
    g = make_guard(tmp_path)
    assert g.check("sleep_short", now=T0)[0] is True


def test_legacy_cooldown_file_is_migrated(tmp_path):
    """v1.8.0 的单规则冷却文件要能平移成簇冷却。"""
    (tmp_path / "_cooldowns.json").write_text(
        json.dumps(
            {"sleep_short": T0, "late_night": T0 - 3600, "spo2_low": T0 - 10 * 3600}
        ),
        encoding="utf-8",
    )
    g = make_guard(tmp_path)
    assert g.check("late_night", now=T0 + 60)[0] is False      # 睡眠簇：取最近时间戳
    assert g.check("resting_hr_high", now=T0 + 60)[0] is False  # 疲劳簇：继承 spo2 时间戳
    assert g.check("resting_hr_high", now=T0 + 13 * 3600)[0] is True


def test_cluster_status_shows_remaining(tmp_path):
    g = make_guard(tmp_path)
    g.mark("spo2_low", now=T0)
    assert "疲劳(12h): 还剩 11.0h" in g.cluster_status(now=T0 + 3600)


def test_status_lines_render(tmp_path):
    g = make_guard(tmp_path)
    g.mark("spo2_low", now=T0)
    text = "\n".join(g.status_lines(now=T0 + 60))
    assert "推送闸门" in text
    assert "疲劳" in g.cluster_status()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
