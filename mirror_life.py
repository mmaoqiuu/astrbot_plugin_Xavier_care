"""沈星回身体镜像：只读生活状态，推活动档位。

只依赖标准库，方便脱离 AstrBot 单测。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


# 0 宅家/久坐，1 日常，2 外出，3 运动/爬山
_LEVEL_RULES = (
    (
        3,
        (
            "爬山",
            "徒步",
            "远足",
            "跑步",
            "晨跑",
            "夜跑",
            "健身",
            "训练",
            "游泳",
            "骑行",
            "球",
            "运动",
            "锻炼",
            "攀岩",
        ),
    ),
    (
        2,
        (
            "出门",
            "外出",
            "逛街",
            "书店",
            "超市",
            "散步",
            "公园",
            "约会",
            "咖啡馆",
            "餐厅",
            "买菜",
            "上班",
            "通勤",
        ),
    ),
    (
        0,
        (
            "窝",
            "沙发",
            "床",
            "看书",
            "看漫画",
            "漫画",
            "电影",
            "宅",
            "在家",
            "躺",
            "睡",
            "发呆",
            "游戏",
        ),
    ),
)

# 档位：步数范围、活动分钟范围、基础心率范围
_LEVEL_RANGES = {
    0: ((1200, 2500), (5, 15), (40, 48)),
    1: ((3000, 4500), (15, 30), (44, 53)),
    2: ((5000, 7000), (25, 45), (49, 59)),
    3: ((8000, 13000), (70, 90), (70, 82)),
}

_NEUTRAL = {
    "level": 1,
    "label": "日常在家",
    "summary": "",
    "schedule": "",
    "mood": "",
    "step_target": 3800,
    "exercise_target": 22,
    "base_hr": 46,
}


def _stable_int(seed: str, low: int, high: int) -> int:
    if high <= low:
        return low
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return low + int(digest[:8], 16) % (high - low + 1)


def _blob(*parts: object) -> str:
    return " ".join(str(p or "") for p in parts)


def _pick_level(text: str) -> int:
    # 先从重到轻；同档关键词任意命中即可。
    for level, words in _LEVEL_RULES:
        if any(w in text for w in words):
            return level
    return 1


def _extract_current(data: dict) -> tuple[str, str, str]:
    """兼容 life_state 的 current 以及顶层 summary。"""
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    schedule = ""
    mood = ""
    if current:
        schedule = str(
            current.get("schedule")
            or current.get("activity")
            or current.get("doing")
            or current.get("label")
            or ""
        )
        mood = str(current.get("mood") or current.get("emotion") or "")
    summary = str(data.get("summary") or data.get("today") or "")
    return schedule.strip(), mood.strip(), summary.strip()


def _progress(now: datetime) -> float:
    hour = now.hour + now.minute / 60 + now.second / 3600
    if hour < 7:
        return 0.02
    if hour >= 23:
        return 0.98
    return min(0.98, max(0.02, 0.03 + 0.95 * (hour - 7) / 16))


def build_snapshot(data_dir: Path, now: datetime | None = None) -> dict:
    """读 data/plugin_data/xavier_life_state/life_state.json。

    日期不是今天、文件缺失、JSON 坏掉，一律回退中性档，绝不抛给主流程。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    path = Path(data_dir).parent / "xavier_life_state" / "life_state.json"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("life_state is not an object")
        if str(data.get("date") or "") != today:
            raise ValueError("life_state date is not today")
    except Exception:
        out = dict(_NEUTRAL)
        out.update(
            {
                "valid": False,
                "date": today,
                "progress": _progress(now),
                "steps": int(out["step_target"] * _progress(now)),
                "exercise_min": int(out["exercise_target"] * _progress(now)),
            }
        )
        return out

    schedule, mood, summary = _extract_current(data)
    # 当前正在做什么优先；没有 current 才退回今日摘要。
    source_text = _blob(schedule, mood) or summary
    level = _pick_level(source_text)
    (step_lo, step_hi), (ex_lo, ex_hi), (hr_lo, hr_hi) = _LEVEL_RANGES[level]
    seed = f"{today}:{level}:{schedule}:{summary}"
    step_target = _stable_int(seed + ":steps", step_lo, step_hi)
    exercise_target = _stable_int(seed + ":exercise", ex_lo, ex_hi)
    base_hr = _stable_int(seed + ":hr", hr_lo, hr_hi)
    progress = _progress(now)

    labels = {0: "宅家/久坐", 1: "日常在家", 2: "外出", 3: "运动/爬山"}
    return {
        "valid": True,
        "date": today,
        "level": level,
        "label": labels[level],
        "summary": summary,
        "schedule": schedule,
        "mood": mood,
        "step_target": step_target,
        "exercise_target": exercise_target,
        "base_hr": base_hr,
        "progress": progress,
        "steps": int(step_target * progress),
        "exercise_min": int(exercise_target * progress),
    }
