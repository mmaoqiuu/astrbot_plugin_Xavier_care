"""经期每日关怀开关（period_daily_care）的配置解析测试。

契约：
- 新键 period_daily_care 未设置（None）时，回落到 v1.8.0 的旧键 rule_period_daily；
- 两个键都没设置时默认开启（她每天总该被惦记一下）；
- 新键一旦显式设置，就以新键为准（旧键被忽略）；
- 读取异常时按「开启」处理，绝不让开关把关怀整条吞掉。
"""

from pathlib import Path

from astrbot_plugin_Xavier_care.monitor import HealthMonitor


class FakeConfig(dict):
    """够用的配置替身：get(key, default) 语义与面板配置一致。"""

    def get(self, key, default=None):
        return dict.get(self, key, default)


def make_monitor(**cfg) -> HealthMonitor:
    # 只验开关解析，不读盘：data_dir 给个工作区外的占位路径即可。
    return HealthMonitor(data_dir=Path("."), config=FakeConfig(**cfg))


def test_default_enabled():
    assert make_monitor()._period_daily_care_enabled() is True


def test_new_key_can_turn_off():
    assert make_monitor(period_daily_care=False)._period_daily_care_enabled() is False


def test_legacy_key_still_honored():
    """老配置只写了 rule_period_daily 时仍然生效。"""
    assert make_monitor(rule_period_daily=False)._period_daily_care_enabled() is False


def test_new_key_wins_over_legacy():
    assert (
        make_monitor(period_daily_care=True, rule_period_daily=False)
        ._period_daily_care_enabled()
        is True
    )
