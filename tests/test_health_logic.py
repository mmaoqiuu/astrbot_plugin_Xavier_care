from datetime import datetime

import pytest

from astrbot_plugin_Xavier_care.health_logic import (
    InvalidPayloadError,
    extract_date,
    normalize_payload,
)


def test_extract_date_falls_back_to_today():
    """date 缺失或非法时兜底成今天，不抛异常。

    当前契约：宁可把这条归到当天，也不让手机端偶发的格式问题丢掉整条数据。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    assert extract_date({"date": "2026/09/11"}) == today
    assert extract_date({"date": "2026-13-40"}) == today
    assert extract_date({}) == today


def test_extract_date_keeps_valid_date():
    assert extract_date({"date": "2026-09-11"}) == "2026-09-11"


def test_extract_date_rejects_non_dict():
    with pytest.raises(InvalidPayloadError):
        extract_date("not a dict")


def test_normalize_payload_invalid_date_falls_back():
    records = normalize_payload({"date": "not-a-date", "steps": 100})
    assert len(records) == 1
    assert records[0]["date"] == datetime.now().strftime("%Y-%m-%d")
    assert records[0]["steps"] == 100


def test_normalize_payload_rejects_non_dict():
    with pytest.raises(InvalidPayloadError):
        normalize_payload(["nope"])
