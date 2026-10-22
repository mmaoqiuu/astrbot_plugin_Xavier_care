from datetime import datetime

from astrbot_plugin_Xavier_care import mirror_logic as m

LIFE = {"base_hr": 46, "label": "日常在家"}


def _state():
    return m.ensure_state({}, "u", "2026-10-22", "沈星回", LIFE)


def test_empty_whisper_never_pushes():
    state = _state()
    now = datetime(2026, 10, 22, 9, 0)
    _, result = m.apply_data_update(
        state, LIFE, now, 96, whisper="", emotion="运动", reason="她在走路"
    )
    assert result == "point"
    assert (state.get("points") or [])[-1]["hr"] == 50
    assert (state.get("points") or [])[-1]["user_hr"] == 96
    assert not state.get("last_data_whisper_at")


def test_data_whisper_can_be_disabled():
    state = _state()
    _, result = m.apply_data_update(
        state,
        LIFE,
        datetime(2026, 10, 22, 17, 0),
        76,
        whisper="今天走得挺多的，好好坐着歇会儿。",
        emotion="亲昵",
        reason="状态平稳",
        allow_whisper=False,
    )
    assert result == "point"
    assert not state.get("last_data_whisper_at")
    assert (state.get("points") or [])[-1]["whisper"] == ""


def test_whisper_respects_cooldown():
    state = _state()
    now = datetime(2026, 10, 22, 9, 0)
    _, result = m.apply_data_update(
        state, LIFE, now, 96, whisper="走慢点", emotion="运动", reason="她在走路"
    )
    assert result == "point_whisper"
    assert state.get("last_data_whisper_at")

    _, result = m.apply_data_update(
        state,
        LIFE,
        datetime(2026, 10, 22, 9, 1),
        97,
        whisper="又走快了",
        emotion="运动",
        reason="还在走",
        debounce_minutes=0,
        whisper_cooldown_minutes=40,
    )
    assert result == "point"


def test_data_point_updates_heart_rate_with_resonance_mapping():
    state = _state()
    _, _ = m.apply_data_update(
        state, LIFE, datetime(2026, 10, 22, 10, 0), 83, whisper=""
    )
    assert state["current"]["hr"] == 48
    assert (state.get("points") or [])[-1]["hr"] == 48
    assert (state.get("points") or [])[-1]["user_hr"] == 83
    assert state["last_data_hr"] == 83
    assert state["current"]["reason"]


def test_legacy_raw_points_are_remapped_once():
    state = _state()
    state["hr_mapping_version"] = 0
    state["current"] = {"hr": 83, "peak_hr": 96, "source": "data", "base_hr": 46}
    state["last_data_hr"] = 83
    state["points"] = [
        {"time": "10:00", "hr": 83, "source": "data", "whisper": "她心跳有点快"},
        {"time": "10:10", "hr": 90, "source": "dialog", "whisper": ""},
    ]
    m.migrate_legacy_mapping(state, LIFE)
    assert state["current"]["hr"] == 48
    assert state["points"][0]["hr"] == 48
    assert state["points"][0]["user_hr"] == 83
    assert state["points"][1]["hr"] == 90
    m.migrate_legacy_mapping(state, LIFE)
    assert state["points"][0]["hr"] == 48
    assert state["hr_mapping_version"] == 3


def test_v1_state_leftover_raw_data_point_is_fixed():
    state = _state()
    state["hr_mapping_version"] = 1
    state["points"] = [
        {"time": "07:05", "hr": 80, "source": "data", "whisper": ""},
    ]
    m.migrate_legacy_mapping(state, LIFE)
    assert state["points"][0]["user_hr"] == 80
    assert state["points"][0]["hr"] == 48


def test_out_of_character_whisper_is_sanitized():
    state = _state()
    _, result = m.apply_data_update(
        state,
        LIFE,
        datetime(2026, 10, 22, 10, 0),
        83,
        whisper="我的心跳只有58，因为你",
        emotion="平静",
        reason="她坐着",
    )
    point = (state.get("points") or [])[-1]
    assert "我的心跳" not in point["whisper"]
    assert point["whisper"] == ""
    assert result == "point"
    assert not state.get("last_data_whisper_at")


def test_public_snapshot_exposes_user_hr_for_sync_card():
    state = _state()
    m.apply_data_update(
        state, LIFE, datetime(2026, 10, 22, 10, 0), 83, whisper=""
    )
    snap = m.public_snapshot(
        state, LIFE, datetime(2026, 10, 22, 10, 1), 25
    )
    data_points = [p for p in snap["points"] if p["source"] == "data"]
    assert data_points
    assert data_points[-1]["user_hr"] == 83
    assert data_points[-1]["hr"] != 83
    assert snap["current_hr"] == data_points[-1]["hr"]
    assert snap["current_hr"] < 83
