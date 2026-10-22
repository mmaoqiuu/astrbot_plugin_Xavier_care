from datetime import datetime

from astrbot_plugin_Xavier_care import mirror_logic as m

LIFE = {"base_hr": 46, "label": "日常在家"}


def test_night_snapshot_hides_stale_sleep():
    state = {
        "profile": {"sleep_min": 487},
        "current": {"hr": 46, "base_hr": 46, "peak_hr": 46},
        "points": [],
    }
    snap = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 1, 30), 25)
    assert snap["sleep_min"] is None
    prompt = m.build_prompt("沈星回", datetime(2026, 10, 22, 1, 30), [], LIFE, snap, 25)
    assert '"sleep_minutes": null' not in prompt
    assert "不输出 sleep_minutes" in prompt


def test_legacy_sleep_without_date_uses_today_state():
    state = {
        "date": "2026-10-22",
        "profile": {"sleep_min": 487},
        "last_generate_at": "2026-10-22 15:47:03",
        "current": {"hr": 46, "base_hr": 46, "peak_hr": 46},
        "points": [],
    }
    snap = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 17, 0), 25)
    assert snap["sleep_min"] == 487

    # 凌晨还没到生成窗口，旧值仍然隐藏
    early = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 1, 30), 25)
    assert early["sleep_min"] is None

    # 不是当天状态文件，不能拿来冒充今天
    state["date"] = "2026-10-21"
    old = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 17, 0), 25)
    assert old["sleep_min"] is None

    state = m.ensure_state({}, "u", "2026-10-22", "沈星回", LIFE)
    payload = {
        "heart_rate": 46,
        "resting_hr": 43,
        "sleep_minutes": 500,
        "emotion": "平静",
        "emotion_intensity": 0,
    }
    m.apply_llm_update(state, payload, LIFE, datetime(2026, 10, 22, 1, 30), 25, "沈星回")
    assert state["profile"].get("sleep_min") is None

    m.apply_llm_update(state, payload, LIFE, datetime(2026, 10, 22, 8, 0), 25, "沈星回")
    assert state["profile"]["sleep_min"] == 500
    assert state["profile"]["sleep_date"] == "2026-10-22"

    # 展示放宽到全天：当晚仍显示今早生成的数
    night = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 23, 0), 25)
    assert night["sleep_min"] == 500

    # 过了零点就属于昨天，不再冒充今夜
    next_day = m.public_snapshot(state, LIFE, datetime(2026, 10, 23, 1, 30), 25)
    assert next_day["sleep_min"] is None
