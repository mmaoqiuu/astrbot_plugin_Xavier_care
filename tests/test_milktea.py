import json

from astrbot_plugin_Xavier_care.milktea import (
    canonical_brand,
    load_day,
    month_summary,
    normalize_entry,
)


def test_canonical_brand_merges_koi_aliases():
    assert canonical_brand("KOI") == "KOI"
    assert canonical_brand("KOI Thé") == "KOI"
    assert canonical_brand("koi thé") == "KOI"
    assert canonical_brand("霸王茶姬") == "霸王茶姬"


def test_normalize_entry_stores_canonical_brand():
    entry = normalize_entry({"brand": "KOI Thé", "name": "饼干奶茶"})
    assert entry is not None
    assert entry["brand"] == "KOI"


def test_load_day_canonicalizes_old_records_without_rewriting(tmp_path):
    target = tmp_path / "2026-10-01.json"
    target.write_text(
        json.dumps({"milktea": [{"brand": "KOI Thé", "name": "饼干奶茶"}]}),
        encoding="utf-8",
    )

    items = load_day(tmp_path, "2026-10-01")
    assert items[0]["brand"] == "KOI"

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["milktea"][0]["brand"] == "KOI Thé"


def test_month_summary_merges_koi_brands(tmp_path):
    rows = {
        "2026-10-01": "KOI Thé",
        "2026-10-02": "KOI",
        "2026-10-03": "霸王茶姬",
    }
    for date_str, brand in rows.items():
        (tmp_path / f"{date_str}.json").write_text(
            json.dumps({"milktea": [{"brand": brand, "name": "测试"}]}),
            encoding="utf-8",
        )

    summary = month_summary(tmp_path, "2026-10")
    assert summary["total"] == 3
    assert summary["favorite"] == "KOI"
