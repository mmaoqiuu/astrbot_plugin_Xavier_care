"""chat_care 聊天内自然关怀的单元测试。

只依赖标准库 + pytest，不需要 AstrBot 运行时。
运行： python -m pytest tests -q
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_care import (  # noqa: E402
    ChatCareInjector,
    build_hint,
    date_key,
    load_state,
    mentioned_types_today,
    period_context_line,
    pick_points,
    prune_mentioned,
    pushed_types_today,
    save_state,
)

T0 = 1_780_000_000.0  # 固定时间基准（2026-05-30 附近），避免测试抖动


class FakeConfig(dict):
    """够用的配置桩：本模块只用 .get(key, default)。"""

    def get(self, key, default=None):  # noqa: D102
        return dict.get(self, key, default)


def alerts(*pairs) -> list[dict]:
    return [{"type": t, "hint": h} for t, h in pairs]


SLEEP = ("sleep_short", "她昨晚只睡了 4 小时 20 分，比平时少了不少。")
SPO2 = ("spo2_low", "她今天的血氧只有 93%，可能有点累。")


# ---------- 语境行 ----------
def test_period_context_line_in_period():
    line = period_context_line({"in_period": True, "period_day": 3})
    assert "经期第 3 天" in line


def test_period_context_line_soon():
    line = period_context_line({"in_period": False, "days_to_next": 2}, soon_days=2)
    assert "还有 2 天" in line


def test_period_context_line_empty_when_far_or_missing():
    assert period_context_line({"in_period": False, "days_to_next": 9}) == ""
    assert period_context_line(None) == ""
    assert period_context_line({}) == ""


# ---------- 要点筛选 ----------
def test_pick_points_basic_and_cap():
    pts = pick_points(alerts(SLEEP, SPO2), {}, now=T0, max_items=2)
    assert [p["type"] for p in pts] == ["sleep_short", "spo2_low"]
    assert len(pick_points(alerts(SLEEP, SPO2), {}, now=T0, max_items=1)) == 1


def test_pick_points_skip_pushed_today():
    pts = pick_points(alerts(SLEEP, SPO2), {}, pushed={"sleep_short"}, now=T0)
    assert [p["type"] for p in pts] == ["spo2_low"]


def test_pick_points_ttl_silence():
    mentioned = {"sleep_short": T0 - 3600, "spo2_low": T0 - 7 * 3600}
    pts = pick_points(alerts(SLEEP, SPO2), mentioned, now=T0, ttl_hours=6)
    assert [p["type"] for p in pts] == ["spo2_low"]


def test_pick_points_excludes_period_types():
    """经期类型由语境行覆盖，不再单独列点，避免同一天数说两遍。"""
    a = alerts(
        ("period_started", "她今天是经期第一天。"),
        ("period_daily", "她正在经期第 2 天。"),
        ("period_soon", "距预测经期还有 1 天。"),
        SPO2,
    )
    assert [p["type"] for p in pick_points(a, {}, now=T0)] == ["spo2_low"]


def test_pick_points_ignores_malformed_items():
    a = [
        {"type": "", "hint": "x"},
        {"type": "spo2_low"},
        "nope",
        {"type": "spo2_low", "hint": SPO2[1]},
    ]
    assert [p["type"] for p in pick_points(a, {}, now=T0)] == ["spo2_low"]


# ---------- 整体拼装 ----------
def test_build_hint_empty_when_nothing_to_say():
    text, used = build_hint([], {"in_period": False, "days_to_next": 20}, now=T0)
    assert text == "" and used == []


def test_build_hint_period_only_has_context_but_no_used():
    text, used = build_hint([], {"in_period": True, "period_day": 1}, now=T0)
    assert "经期第 1 天" in text
    assert used == []          # 语境行不记账、不挡主动推送
    assert "可以完全不提" in text


def test_build_hint_lists_points_with_soft_wording():
    text, used = build_hint(
        alerts(SLEEP, SPO2),
        {"in_period": False, "days_to_next": 30},
        now=T0,
        max_items=2,
    )
    assert used == ["sleep_short", "spo2_low"]
    assert "我监测到" in text   # 反面提示词仍在，防止角色播报
    assert "4 小时 20 分" in text


# ---------- 状态文件 ----------
def test_state_roundtrip_and_bad_file(tmp_path: Path):
    p = tmp_path / "chat_hint_state.json"
    assert load_state(p) == {"mentioned": {}}          # 不存在
    assert save_state(p, {"mentioned": {"sleep_short": T0}}) is True
    assert load_state(p)["mentioned"] == {"sleep_short": T0}
    p.write_text("这不是 json", encoding="utf-8")
    assert load_state(p) == {"mentioned": {}}          # 坏文件当空处理


def test_prune_keeps_today_and_fresh():
    today_fresh = time.time()
    old = today_fresh - 30 * 3600
    kept = prune_mentioned(
        {"a": today_fresh - 3600, "b": old, "c": "bad"},
        now=today_fresh,
        ttl_hours=1,
    )
    assert "b" not in kept and "c" not in kept
    assert "a" in kept


def test_mentioned_types_today_only_today():
    now = time.time()
    state = {"mentioned": {"sleep_short": now, "spo2_low": now - 2 * 86400}}
    assert mentioned_types_today(state, now=now) == {"sleep_short"}


# ---------- 关怀日志解析（反向去重的依据） ----------
def test_pushed_types_today_parsing(tmp_path: Path):
    log = tmp_path / "care_log.jsonl"
    today = date_key()
    lines = [
        json.dumps({"ts": f"{today} 09:00:00", "type": "sleep_short", "ok": True}),
        json.dumps({"ts": f"{today} 10:00:00", "type": "spo2_low", "ok": False}),
        json.dumps({"ts": "2020-01-01 09:00:00", "type": "late_night", "ok": True}),
        "{坏行}",
    ]
    log.write_text("\n".join(lines), encoding="utf-8")
    assert pushed_types_today(log) == {"sleep_short"}
    assert pushed_types_today(tmp_path / "missing.jsonl") == set()


# ---------- 注入器（含记账 / 反向去重） ----------
def test_injector_records_and_silences_next_round(tmp_path: Path):
    inj = ChatCareInjector(data_dir=tmp_path, config=FakeConfig())
    cfg = {"in_period": True, "period_day": 2}

    text, used = inj.render(alerts(SLEEP, SPO2), cfg)
    assert "经期第 2 天" in text and used == ["sleep_short", "spo2_low"]
    # 记账：今天这两个要点算「已提过」，供反向去重查询
    assert inj.mentioned_types_today() == {"sleep_short", "spo2_low"}

    # 第二轮：要点进静默期，只剩语境行
    text2, used2 = inj.render(alerts(SLEEP, SPO2), cfg)
    assert used2 == [] and "经期第 2 天" in text2


def test_injector_respects_pushed_today(tmp_path: Path):
    log = tmp_path / "care_log.jsonl"
    log.write_text(
        json.dumps(
            {"ts": f"{date_key()} 08:00:00", "type": "sleep_short", "ok": True}
        ),
        encoding="utf-8",
    )
    inj = ChatCareInjector(data_dir=tmp_path, config=FakeConfig())
    text, used = inj.render(alerts(SLEEP, SPO2), {})
    assert used == ["spo2_low"]          # 推过的不再念叨
    assert "4 小时 20 分" not in text


def test_injector_empty_when_nothing(tmp_path: Path):
    inj = ChatCareInjector(data_dir=tmp_path, config=FakeConfig())
    text, used = inj.render([], {})
    assert (text, used) == ("", [])
