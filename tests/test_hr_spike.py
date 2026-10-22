import json
from pathlib import Path

import pytest

from astrbot_plugin_Xavier_care.monitor import HealthMonitor


def make_monitor(tmp_path, **cfg):
    base = {"hr_spike_enabled": True}
    base.update(cfg)
    return HealthMonitor(Path(tmp_path), base)


def write_day(tmp_path, points):
    (Path(tmp_path) / "2026-10-20.json").write_text(
        json.dumps({"date": "2026-10-20", "hr_timeline": points}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_spike_detected(tmp_path):
    write_day(tmp_path, [{"time": "09:00", "hr": 70}, {"time": "10:00", "hr": 108}])
    mon = make_monitor(tmp_path)
    alerts: list = []
    mon._check_hr_spike({"hr_timeline": [{"time": "09:00", "hr": 70}, {"time": "10:00", "hr": 108}]}, alerts)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "hr_spike"
    assert "38" in alerts[0]["hint"]


def test_slow_rise_ignored(tmp_path):
    mon = make_monitor(tmp_path)
    alerts: list = []
    mon._check_hr_spike({"hr_timeline": [{"time": "09:00", "hr": 70}, {"time": "12:00", "hr": 110}]}, alerts)
    assert alerts == []


def test_skip_when_late_night_already_there(tmp_path):
    mon = make_monitor(tmp_path)
    alerts = [{"type": "late_night_realtime", "hint": "x"}]
    mon._check_hr_spike({"hr_timeline": [{"time": "00:30", "hr": 60}, {"time": "01:00", "hr": 95}]}, alerts)
    assert len(alerts) == 1
