from datetime import datetime

from astrbot_plugin_Xavier_care import mirror_logic as m

LIFE = {"base_hr": 46, "label": "日常在家"}


def _state():
    return m.ensure_state({}, "u", "2026-10-22", "沈星回", LIFE)


def _parsed(hr: int, emotion: str = "平静", intensity: int = 1):
    return {
        "heart_rate": hr,
        "emotion": emotion,
        "emotion_intensity": intensity,
        "reason": "聊到今天的安排",
    }


def _seed_last_point(state, hm: str, hr: int):
    state["points"] = [
        {
            "time": hm,
            "hr": hr,
            "source": "data",
            "reason": "手机数据",
            "whisper": "",
        }
    ]
    state["current"] = {"hr": hr, "base_hr": 46, "source": "data"}


def test_calm_dialog_adds_point_when_clearly_higher():
    state = _state()
    _seed_last_point(state, "16:56", 50)

    state, result = m.apply_dialog_update(
        state, _parsed(68), LIFE, datetime(2026, 10, 22, 17, 8), 25
    )

    assert result == "calm_point"
    point = state["points"][-1]
    assert point["time"] == "17:08"
    assert point["hr"] == 68
    assert point["source"] == "dialog_calm"
    assert point["reason"] == "聊到今天的安排"

    snap = m.public_snapshot(state, LIFE, datetime(2026, 10, 22, 17, 9), 25)
    assert any(p["source"] == "dialog_calm" and p["hr"] == 68 for p in snap["points"])


def test_calm_dialog_skips_small_rise():
    state = _state()
    _seed_last_point(state, "16:56", 50)

    state, result = m.apply_dialog_update(
        state, _parsed(54), LIFE, datetime(2026, 10, 22, 17, 8), 25
    )

    assert result == "calm"
    assert len(state["points"]) == 1


def test_calm_dialog_skips_when_interval_too_short():
    state = _state()
    _seed_last_point(state, "17:04", 50)

    state, result = m.apply_dialog_update(
        state, _parsed(68), LIFE, datetime(2026, 10, 22, 17, 8), 25
    )

    assert result == "calm"
    assert len(state["points"]) == 1


def test_calm_dialog_does_not_overwrite_same_minute_point():
    state = _state()
    _seed_last_point(state, "17:08", 50)

    state, result = m.apply_dialog_update(
        state, _parsed(68), LIFE, datetime(2026, 10, 22, 17, 8), 25
    )

    assert result == "calm"
    assert state["points"][-1]["hr"] == 50
    assert state["current"]["hr"] == 68
