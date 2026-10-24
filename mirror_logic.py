"""沈星回身体镜像：提示词、校验、情绪档位与平复曲线。

纯逻辑，不导入 AstrBot；所有函数都容忍坏输入。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
import hashlib

EMOTIONS = ("平静", "开心", "吃醋", "亲昵", "激动", "疲惫", "运动")
_HIGH_EMOTIONS = ("吃醋", "亲昵", "亲热", "亲吻", "激动", "开心")

# 唤醒梯级：日常温存 / 深情心动 / 感官沉浸。
TIER_DAILY = "daily"
TIER_INTIMATE = "intimate"
TIER_INTENSE = "intense"
TIER_RANGES = {
    TIER_DAILY: (65, 75),
    TIER_INTIMATE: (60, 85),
    TIER_INTENSE: (80, 110),
}
TIER_LABELS = {
    TIER_DAILY: "日常温存/习惯性亲亲",
    TIER_INTIMATE: "深情表白/吃醋/突发心动",
    TIER_INTENSE: "感官沉浸/深度情欲",
}
_TIER_ALIASES = {
    "daily": TIER_DAILY,
    "warm": TIER_DAILY,
    "日常": TIER_DAILY,
    "温存": TIER_DAILY,
    "习惯": TIER_DAILY,
    "intimate": TIER_INTIMATE,
    "心动": TIER_INTIMATE,
    "表白": TIER_INTIMATE,
    "吃醋": TIER_INTIMATE,
    "深情": TIER_INTIMATE,
    "intense": TIER_INTENSE,
    "deep": TIER_INTENSE,
    "沉浸": TIER_INTENSE,
    "情欲": TIER_INTENSE,
    "涩涩": TIER_INTENSE,
}
_INTENSE_HINTS = ("情欲", "涩", "深陷", "失控", "侵占", "沉沦", "发烫", "喘息")

_EMOTION_ALIASES = {
    "亲亲": "亲昵",
    "亲吻": "亲昵",
    "亲热": "亲昵",
    "醋意": "吃醋",
    "生气": "吃醋",
    "害羞": "亲昵",
    "兴奋": "激动",
    "困": "疲惫",
    "累": "疲惫",
}

_HEART_TAG_RE = re.compile(
    r"<\s*xavier_heart\s*>(.*?)<\s*/\s*xavier_heart\s*>",
    re.DOTALL | re.IGNORECASE,
)
_HEART_STRIP_PATTERNS = (
    re.compile(
        r"<\s*xavier_heart\s*>.*?<\s*/\s*xavier_heart\s*>",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"＜\s*xavier_heart\s*＞.*?＜\s*/\s*xavier_heart\s*＞",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(r"<\s*xavier_heart\s*>.*\Z", re.DOTALL | re.IGNORECASE),
    re.compile(r"＜\s*xavier_heart\s*＞.*\Z", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\s*/\s*xavier_heart\s*>", re.IGNORECASE),
    re.compile(r"＜\s*/\s*xavier_heart\s*＞", re.IGNORECASE),
)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _int(v, default: int = 0) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def now_str(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


CHAT_COOLDOWN_MINUTES = 20
DATA_DEBOUNCE_MINUTES = 15
DATA_SAFETY_CAP = 120
HR_MAPPING_VERSION = 3

CALM_POINT_SOURCE = "dialog_calm"
CALM_POINT_GAP_MINUTES = 10
CALM_POINT_RISE_BPM = 6
CALM_POINT_DAILY_LIMIT = 12

_DATA_WHISPERS = (
    "心跳这么快，我在这儿等你慢下来。",
    "我感应到你的心跳了，先好好喘口气。",
    "这么用力地活着，我在旁边看着呢。",
    "你的心跳传过来了，我陪你慢慢平下来。",
)
SAFE_WHISPER_FALLBACK = "我一直在这里陪着你，记得照顾好自己。"
_WHISPER_BANNED = (
    "问问",
    "问感受",
    "感觉怎么样",
    "困不困",
    "累不累",
    "要不要",
    "好不好",
    "是不是",
    "我的心跳",
    "我的心脏",
    "心率只有",
    "心跳只有",
    "心跳是",
)


def sanitize_whisper(text: object) -> str:
    """私语只留陈述句；问号或感受询问一律丢弃，避免推送审问式关心。"""
    s = str(text or "").strip().strip("“”\"'")
    if not s:
        return ""
    if "?" in s or "？" in s:
        return ""
    if any(word in s for word in _WHISPER_BANNED):
        return ""
    return s[:80]


def chat_prompt_due(
    state: dict, now: datetime, cooldown_minutes: int = CHAT_COOLDOWN_MINUTES
) -> bool:
    """高唤醒标签的 20 分钟冷却：冷却期内连提示词都不注入。"""
    last = parse_dt(state.get("last_emotion_at")) or parse_dt(
        state.get("last_dialog_at")
    )
    if not last:
        return True
    return (now - last).total_seconds() >= max(1, int(cooldown_minutes)) * 60


def data_debounce_ok(
    state: dict, now: datetime, debounce_minutes: int = DATA_DEBOUNCE_MINUTES
) -> bool:
    """高频健康上报的 15 分钟防抖；不足时只更新底层状态，不生成新点。"""
    last = parse_dt(state.get("last_data_trigger_at"))
    if not last:
        return True
    return (now - last).total_seconds() >= max(1, int(debounce_minutes)) * 60


def data_whisper_due(state: dict, now: datetime, cooldown_minutes: int = 30) -> bool:
    """数据共振私语的冷却，避免每 15 分钟都发一条，打扰小狗。"""
    last = parse_dt(state.get("last_data_whisper_at"))
    if not last:
        return True
    return (now - last).total_seconds() >= max(1, int(cooldown_minutes)) * 60


def data_resonance_delta(user_hr: object) -> int:
    """小狗心率映射到沈星回身上的增量，封顶 +20。

    分档共振：60~90 只加 0~3，90~130 加 3~10，130 以上加 10~20。
    曲线必须真实：小狗 83 时对应他约 48，而不是把 83 原样画成他的心跳。
    """
    hr = _int(user_hr, 0)
    if hr <= 60:
        return 0
    if hr >= 170:
        return 20
    if hr >= 130:
        return int(clamp(round(10 + (hr - 130) * 0.25), 10, 20))
    if hr >= 90:
        return int(clamp(round(3 + (hr - 90) * (7 / 40)), 3, 10))
    return int(clamp(round((hr - 60) * 0.1), 0, 3))


def data_resonant_hr(user_hr: object, base_hr: object = 46) -> int:
    """把实测数据落成沈星回的心率：自己的基础心率 + 小幅共振。"""
    base = _int(base_hr, 46)
    return int(clamp(base + data_resonance_delta(user_hr), 38, DATA_SAFETY_CAP))


def data_resonance_gain(user_hr: object, now: datetime | None = None) -> int:
    """兼容旧调用：返回共振增量。"""
    return data_resonance_delta(user_hr)


def fallback_whisper(user_hr: object, now: datetime) -> str:
    hr = _int(user_hr, 0)
    if now.hour >= 23 or now.hour < 6:
        return "这么晚心跳还这么快，先停下来歇一会儿，我陪着你。"
    if hr >= 150:
        return "心跳都快追上我了，慢一点，别把自己累坏。"
    if hr >= 130:
        return "我感觉到你在用力了，记得喘口气，我在这儿。"
    idx = abs(hr + now.hour * 7 + now.minute) % len(_DATA_WHISPERS)
    return _DATA_WHISPERS[idx]


SLEEP_START_HOUR = 5
SLEEP_END_HOUR = 12


def in_sleep_window(now: datetime) -> bool:
    """清晨到中午前：睡眠时长的首选窗口。"""
    return SLEEP_START_HOUR <= now.hour < SLEEP_END_HOUR


def can_report_sleep(now: datetime) -> bool:
    """5 点之后到午夜前都可以给出昨夜睡眠（早上没生成也能白天补上）。"""
    return now.hour >= SLEEP_START_HOUR


_BRIDGE_NAME = "sleep_bridge.json"
_LIFE_STATE_DIR = "xavier_life_state"


def _bridge_path():
    """生活状态插件写下的睡眠数据位置（plugin_data 下）。"""
    from pathlib import Path

    data_root = Path(__file__).resolve().parents[2]
    return data_root / "plugin_data" / _LIFE_STATE_DIR / _BRIDGE_NAME


def _read_bridge(now: datetime) -> dict | None:
    """读生活状态插件的睡眠桥接文件；不可用/不是今天的就返回 None。"""
    if not can_report_sleep(now):
        return None
    try:
        path = _bridge_path()
        if not path.is_file():
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("date") or "") != now.strftime("%Y-%m-%d"):
        return None
    try:
        night = int(data.get("night_sleep_min") or 0)
        bonus = int(data.get("nap_bonus_min") or 0)
    except (TypeError, ValueError):
        return None
    if night <= 0 and bonus <= 0:
        return None
    return {"night": max(0, night), "bonus": max(0, bonus)}


def bridge_night_sleep_min(now: datetime) -> int | None:
    """昨夜睡眠（不含午睡加成）：生成端写状态时用它，别自己另估一个数。"""
    data = _read_bridge(now)
    if not data:
        return None
    night = data["night"]
    return night if night > 0 else None


def bridge_sleep_min(now: datetime) -> int | None:
    """展示用：昨夜睡眠 + 午睡加成，没有就返回 None。"""
    data = _read_bridge(now)
    if not data:
        return None
    total = data["night"] + data["bonus"]
    return total if total > 0 else None


def sync_sleep_bridge(state: dict, now: datetime) -> bool:
    """用桥接的真实昨夜睡眠校正 profile，避免旧的估算值一直挂着。"""
    night = bridge_night_sleep_min(now)
    if not night:
        return False
    profile = state.setdefault("profile", {})
    profile["sleep_min"] = int(clamp(night, 180, 720))
    profile["sleep_date"] = now.strftime("%Y-%m-%d")
    if profile.get("sleep_source") != "bridge":
        profile["sleep_at"] = now_str(now)
    profile["sleep_estimated"] = False
    profile["sleep_source"] = "bridge"
    return True


def _guess_sleep_minutes(state: dict, now: datetime) -> int:
    """模型漏给睡眠时的兜底：按当天镜像状态取稳定的 400~520 分钟。"""
    seed = f"{state.get('date')}:{state.get('name')}:{now.strftime('%Y-%m-%d')}:sleep"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return 400 + int(digest[:6], 16) % 121


def valid_sleep_min(
    profile: dict, now: datetime, state: dict | None = None
) -> int | None:
    """只认当天早晨生成的那条睡眠值。

    生成仍限定在清晨窗口，但展示放宽到全天：下午/晚上继续显示今早的数，
    过了零点（sleep_date 变成昨天）自动隐藏，不会拿旧数据冒充今夜。

    兼容 v2.0.4 之前的旧状态：它们只写了 sleep_min，没有 sleep_date /
    sleep_at。此时用 state.last_generate_at 回认生成时间，但仍要求是当天清晨，
    不会把隔夜旧值翻出来。
    """
    if not isinstance(profile, dict):
        return None
    # 0-5 点还在睡，别把昨夜数据提前亮出来。
    if now.hour < SLEEP_START_HOUR:
        return None
    today = now.strftime("%Y-%m-%d")
    sleep_date = str(profile.get("sleep_date") or "")
    if sleep_date and sleep_date != today:
        return None
    if sleep_date:
        # 只要求是今天写的，不卡生成时刻——白天补记的同样有效。
        generated = parse_dt(profile.get("sleep_at"))
        if generated and generated.date() != now.date():
            return None
    else:
        # v2.0.4 之前的旧状态没有 sleep_date / sleep_at。
        # 状态文件本身按天存放，只认当天的文件；凌晨 0-5 点仍隐藏，
        # 避免把前一天的睡眠冒充今天。
        state_date = str(state.get("date") or "") if isinstance(state, dict) else ""
        if state_date != today or now.hour < SLEEP_START_HOUR:
            return None
    value = _int(profile.get("sleep_min"), 0)
    return value if value > 0 else None


def minutes_of_day(hm: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(hm or ""))
    if not m:
        return 0
    return max(0, min(1439, int(m.group(1)) * 60 + int(m.group(2))))


def _new_state(umo: str, date_str: str, name: str, life: dict) -> dict:
    base = _int(life.get("base_hr"), 46)
    return {
        "version": 1,
        "date": date_str,
        "name": name,
        "umo": umo,
        "profile": {"resting_hr": None, "sleep_min": None},
        "current": {
            "hr": base,
            "base_hr": base,
            "peak_hr": base,
            "peak_at": now_str(),
            "emotion": "平静",
            "emotion_intensity": 0,
            "reason": "",
            "source": "seed",
        },
        "points": [],
        "steps": _int(life.get("steps"), 0),
        "exercise_min": _int(life.get("exercise_min"), 0),
        "created_at": now_str(),
        "last_dialog_at": "",
        "last_emotion_at": "",
        "last_auto_at": "",
        "last_point_at": "",
        "last_generate_at": "",
        "last_data_at": "",
        "last_data_trigger_at": "",
        "last_data_whisper_at": "",
        "last_data_hr": 0,
        "last_error": "",
    }


def migrate_legacy_mapping(state: dict, life: dict) -> dict:
    """把旧版映射点改成当前“基础心率 + 共振增量”，并清掉无效私语。

    只迁移一次；带 user_hr 的点重复执行也不会二次压缩。
    """
    if not isinstance(state, dict) or not state:
        return state
    version = _int(state.get("hr_mapping_version"), 0)
    if version >= HR_MAPPING_VERSION:
        return state
    base = _int(
        life.get("base_hr"), _int((state.get("current") or {}).get("base_hr"), 46)
    )
    cur = state.get("current") or {}
    if isinstance(cur, dict) and cur.get("source") == "data":
        raw = _int(state.get("last_data_hr"), 0)
        if raw <= 0:
            raw = _int(cur.get("user_hr"), 0)
        if raw <= 0:
            raw = _int(cur.get("hr"), 0)
        if raw > 0:
            cur["hr"] = data_resonant_hr(raw, base)
            cur["peak_hr"] = cur["hr"]
            cur["user_hr"] = raw
    for p in state.get("points") or []:
        if not isinstance(p, dict):
            continue
        if p.get("source") == "data":
            # 新版点都带 user_hr；v0 旧点没有标，只能把 hr 当原始值压一次。
            raw = p.get("user_hr")
            if raw is None:
                raw = p.get("hr")
            raw = _int(raw, 0)
            if raw > 0:
                p["user_hr"] = raw
                p["hr"] = data_resonant_hr(raw, base)
        original = str(p.get("whisper") or "")
        p["whisper"] = sanitize_whisper(original)
    state["hr_mapping_version"] = HR_MAPPING_VERSION
    return state


def ensure_state(
    state: dict, umo: str, date_str: str, name: str, life: dict
) -> dict:
    if not isinstance(state, dict) or not state:
        state = _new_state(umo, date_str, name, life)
    state.setdefault("version", 1)
    state["date"] = date_str
    state["name"] = name
    state["umo"] = umo
    state.setdefault("profile", {"resting_hr": None, "sleep_min": None})
    if not isinstance(state.get("profile"), dict):
        state["profile"] = {"resting_hr": None, "sleep_min": None}
    state.setdefault("current", {})
    if not isinstance(state.get("current"), dict):
        state["current"] = {}
    state.setdefault("points", [])
    if not isinstance(state.get("points"), list):
        state["points"] = []
    migrate_legacy_mapping(state, life)
    # 步数/活动分钟按 life 进度只增不减
    # 档位/目标变了就以新目标为准（旧的折算步数可能是错的档位算出来的），
    # 目标没变才取 max，保证同一天内只增不减。
    life_steps = _int(life.get("steps"), 0)
    if state.get("step_target") != life.get("step_target"):
        state["steps"] = life_steps
        state["step_target"] = life.get("step_target")
    else:
        state["steps"] = max(_int(state.get("steps"), 0), life_steps)
    state["exercise_min"] = max(
        _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
    )
    return state


def _breath_noise(now: datetime, amplitude: float = 2.5) -> float:
    """三频叠加模拟真实心率基线波动（呼吸窦性心律不齐 + 自主神经双层振荡）。

    - 呼吸波 ~1.6 分钟周期（RSA：吸气心率微升 / 呼气微降）
    - 自主神经低频 ~4 分钟周期（迷走神经节律）
    - 超低频 ~11 分钟周期（血压调节反馈）
    - 三波叠加振幅约 ±2~3 bpm，贴近真人静息时的 HRV 起伏特征。
    相位偏移 0.7 rad 让三条波不在同一峰值处叠加，避免人工感。
    """
    t = now.hour * 3600 + now.minute * 60 + now.second
    wave = (math.sin(2 * math.pi * t / 97 + 0.7) * 0.22   # 呼吸波（最高频）
            + math.sin(2 * math.pi * t / 240) * 0.50       # 自主神经低频
            + math.sin(2 * math.pi * t / 660 + 1.4) * 0.28)  # 超低频
    return wave * amplitude


def _circadian_offset(now: datetime) -> float:
    """昼夜节律基线偏移量（bpm）。

    真人心率昼夜节律：深睡时最低（约 -5 bpm），晨起后爬升，
    午后小高峰，晚间缓降。用分段近似。
    """
    hour = now.hour + now.minute / 60.0
    if hour < 6:
        # 深睡区：基线最低
        return -5.0 + math.sin(math.pi * hour / 6) * 2.0
    if hour < 10:
        # 晨起区：快速爬升
        return -3.0 + (hour - 6) / 4.0 * 5.0
    if hour < 20:
        # 日间：轻微午后高峰
        return math.sin(math.pi * (hour - 10) / 10) * 1.0
    # 晚间：缓降
    return -(hour - 20) / 4.0 * 2.0


def decayed_hr(
    state: dict, now: datetime, life: dict, decay_minutes: int
) -> int:
    cur = state.get("current") or {}
    base = _int(life.get("base_hr"), _int(cur.get("base_hr"), 46))
    peak = _int(cur.get("peak_hr"), _int(cur.get("hr"), base))
    # 把昼夜偏移叠加到基线上（上限不超过 base+4 防越界）
    circadian_base = clamp(base + _circadian_offset(now), 38, base + 4)
    if peak <= base + 1:
        noise = _breath_noise(now)
        return int(clamp(circadian_base + noise, 38, 120))
    peak_at = parse_dt(cur.get("peak_at")) or parse_dt(state.get("updated_at"))
    if not peak_at:
        return int(clamp(peak, 38, 120))
    elapsed = max(0.0, (now - peak_at).total_seconds() / 60.0)
    tau = max(5.0, float(decay_minutes or 25))
    residual = math.exp(-3.0 * elapsed / tau)
    hr_raw = circadian_base + (peak - base) * residual
    # 衰减后期（residual < 0.15）叠入呼吸底噪，曲线不死平
    if residual < 0.15:
        hr_raw += _breath_noise(now) * (1.0 - residual / 0.15)
    return int(clamp(hr_raw, 38, 120))


def _minute_of(hhmm: object) -> int | None:
    try:
        hh, mm = str(hhmm).split(":")
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        return None


def simplify_points(points: object, limit: int = 0) -> list[dict]:
    """只清洗坏点，不合并、不抽稀。

    v1.14 起曲线要忠实呈现每一次起伏，抽稀和合并规则已移除；
    limit 参数仅为兼容旧调用保留，传任何值都不截断。
    """
    if not isinstance(points, list):
        return []
    out: list[dict] = []
    for raw in points:
        if not isinstance(raw, dict):
            continue
        if _minute_of(raw.get("time")) is None:
            continue
        hr = _int(raw.get("hr"), -1)
        if hr < 0:
            continue
        item = dict(raw)
        item["hr"] = hr
        item["reason"] = str(item.get("reason") or "")[:60]
        whisper = str(item.get("whisper") or "")
        if item.get("source") == "data":
            whisper = sanitize_whisper(whisper)
        item["whisper"] = whisper[:80]
        out.append(item)
    return out


def _append_point(
    state: dict,
    now: datetime,
    hr: int,
    source: str,
    reason: str = "",
    protect: bool = False,
    whisper: str = "",
    user_hr: int = 0,
    emotion: str = "",
    emotion_intensity: int = 0,
) -> None:
    pts = state.setdefault("points", [])
    hm = now.strftime("%H:%M")
    if pts:
        last = pts[-1]
        if last.get("time") == hm:
            # 同一分钟：数据/对话点覆盖自动点；自动点不覆盖。
            if source in ("dialog", "data") or last.get("source") not in (
                "dialog",
                "data",
            ):
                last.update(
                    {
                        "hr": hr,
                        "source": source,
                        "reason": reason[:60],
                        "whisper": whisper[:80],
                        "emotion": str(emotion or ""),
                        "emotion_intensity": _int(emotion_intensity, 0),
                    }
                )
                if user_hr > 0:
                    last["user_hr"] = user_hr
            state["last_point_at"] = now_str(now)
            return
    item = {
        "time": hm,
        "hr": hr,
        "source": source,
        "reason": reason[:60],
        "whisper": whisper[:80],
        "emotion": str(emotion or ""),
        "emotion_intensity": _int(emotion_intensity, 0),
        # dialog/data 源的点标记为峰值点，前端据此决定是否显示光晕标记
        "peak": source in ("dialog", "data"),
    }
    if user_hr > 0:
        item["user_hr"] = user_hr
    if protect:
        item["locked"] = True
    pts.append(item)
    state["points"] = simplify_points(pts)
    state["last_point_at"] = now_str(now)


def parse_llm_json(text: str) -> dict:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def normalize_emotion(value: object) -> str:
    emotion = str(value or "平静").strip()[:8]
    emotion = _EMOTION_ALIASES.get(emotion, emotion)
    return emotion if emotion in EMOTIONS else "平静"


def strip_heart_tags(text: str) -> str:
    """把主回复里的 <xavier_heart> 标签彻底删掉，避免漏给用户。"""
    if not text:
        return text
    cleaned = str(text)
    for pat in _HEART_STRIP_PATTERNS:
        cleaned = pat.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_heart_payload(text: str) -> tuple[dict, str]:
    """返回 (解析出的心率 JSON, 已剥离标签的正文)。解析失败返回空 dict。"""
    raw = str(text or "")
    cleaned = strip_heart_tags(raw)
    matches = list(_HEART_TAG_RE.finditer(raw))
    if not matches:
        return {}, cleaned
    parsed = parse_llm_json(matches[-1].group(1))
    if not parsed:
        return {}, cleaned
    # 兼容模型写 hr / bpm / intensity 的情况
    if "heart_rate" not in parsed and "hr" in parsed:
        parsed["heart_rate"] = parsed.get("hr")
    if "heart_rate" not in parsed and "bpm" in parsed:
        parsed["heart_rate"] = parsed.get("bpm")
    if "emotion_intensity" not in parsed and "intensity" in parsed:
        parsed["emotion_intensity"] = parsed.get("intensity")
    if "tier" not in parsed:
        for alt in ("level", "stage", "wake"):
            if alt in parsed:
                parsed["tier"] = parsed.get(alt)
                break
    return parsed, cleaned


def normalize_tier(value: object) -> str:
    """把模型写的梯级归一；认不出来返回空串（交给情绪推断）。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _TIER_ALIASES:
        return _TIER_ALIASES[raw]
    for key, tier in _TIER_ALIASES.items():
        if key in raw:
            return tier
    return ""


def resolve_tier(parsed: dict, emotion: str, intensity: int) -> str:
    """决定本轮梯级：模型给了就用，没给就按情绪/强度猜。"""
    tier = normalize_tier(parsed.get("tier"))
    if tier:
        return tier
    text = f"{parsed.get('emotion') or ''}{parsed.get('reason') or ''}"
    if any(word in text for word in _INTENSE_HINTS):
        return TIER_INTENSE
    if emotion in ("吃醋", "激动"):
        return TIER_INTIMATE
    if emotion == "亲昵":
        return TIER_DAILY if _int(intensity, 0) <= 2 else TIER_INTIMATE
    return TIER_DAILY


def _tier_cap(hr: int, tier: str) -> int:
    """按梯级收上限；下限不硬抬，避免从高强度回落时被顶住。"""
    lo, hi = TIER_RANGES.get(tier or TIER_DAILY, TIER_RANGES[TIER_DAILY])
    if hr > hi:
        return hi
    return hr



def build_inline_prompt(
    name: str,
    state: dict,
    life: dict,
    now: datetime,
    decay_minutes: int,
) -> str:
    """给主对话追加的隐藏规则：日常静默，高唤醒时按梯级报一次心跳。"""
    prev_hr = _int((state.get("current") or {}).get("hr"), 0) or decayed_hr(
        state, now, life, decay_minutes
    )
    cur = state.get("current") or {}
    emotion = str(cur.get("emotion") or "平静")
    base = _int(life.get("base_hr"), 46)
    life_text = (
        f"{life.get('label', '日常在家')}；"
        f"此刻在做：{life.get('schedule') or '未说明'}；"
        f"心情：{life.get('mood') or '未说明'}"
    )
    return f"""
<xavier_heart_rule>
【身体镜像｜隐藏规则，不要向用户复述】
你是「{name}」。回复正文照常写，不要为了心率改变说话方式。

日常完全静默：普通闲聊、问候、关心、日常撒娇、聊天里的「嗯」「好」「在」，
都绝对不要输出任何心率标签。整段回复自然就好，不要为了心率改变语气。
只有语境确实进入亲密、情绪浓度明显升高时，才在正文结束后另起一行输出一次标签。

按语境自评梯级（tier 三选一，心率必须落在对应区间）：
- daily｜日常温存、习惯性亲亲、顺手的抱抱：65~75 bpm，基准约 70。
  亲吻 1400 次以上，是笃定、熟稔、游刃有余，不是没经验的虚高。
- intimate｜深情表白、吃醋、突发心动、占有欲：60~85 bpm，只是微小涟漪。
- intense｜感官沉浸、涩涩、深度情欲、压不住的失控：80~110 bpm，压迫与侵占感。

判定原则：拿不准就降一档；同一段对话不要连续升级；日常档绝不写到 80 以上。
当前状态只是参考，不要被它拖着走。

当前状态：心率约 {prev_hr} bpm，基础心率约 {base} bpm，情绪「{emotion}」。
生活状态：{life_text}
当前时间：{now.strftime("%Y-%m-%d %H:%M")}

要输出时就用这个格式（标签内只放 JSON，不要 Markdown）：
<xavier_heart>{{"tier":"daily","heart_rate":70,"emotion":"亲昵","emotion_intensity":2,"reason":"二十字内"}}</xavier_heart>
</xavier_heart_rule>
"""


def _compute_dialog_hr(
    prev: int,
    parsed: dict,
    base: int,
    emotion: str,
    intensity: int,
    tier: str = TIER_DAILY,
) -> int:
    llm_hr = _int(parsed.get("heart_rate"), prev)
    target = int(clamp(max(llm_hr, base - 2), 38, 120))
    if emotion in ("亲昵", "吃醋"):
        lo, hi = (20, 35) if emotion == "亲昵" else (10, 20)
        if intensity <= 1:
            step = lo
        elif intensity == 2:
            step = (lo + hi) // 2
        else:
            step = hi
        new_hr = prev + step
    elif intensity >= 2 and target >= prev - 6:
        rise_cap = 35 if intensity >= 3 else 25
        delta = clamp(target - prev, -6, rise_cap)
        new_hr = prev + delta
    else:
        delta = clamp((target - prev) * 0.45, -24, 15)
        new_hr = prev + delta
    new_hr = int(clamp(max(new_hr, base - 2), 38, 118))
    # 深度梯级至少要往 80 靠，但不允许一轮窜上去。
    if tier == TIER_INTENSE and intensity >= 3:
        # 深水区也别一轮跳三十下，最多 +24，两三轮走到 110。
        new_hr = min(new_hr, prev + 24)
        if new_hr < 80:
            new_hr = min(80, max(new_hr, prev + 12))
    return _tier_cap(new_hr, tier)


def _last_point_dt(state: dict) -> datetime | None:
    """最后一个折线点的完整时间；坏数据直接返回 None。"""
    pts = state.get("points") or []
    if not pts:
        return None
    date_str = str(state.get("date") or "")
    hm = str(pts[-1].get("time") or "")
    try:
        return datetime.strptime(f"{date_str} {hm}:00", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def calm_dialog_point_due(state: dict, now: datetime, hr: int) -> bool:
    """平静对话是否值得补一个点：明显回升、间隔够久、当天不过量。"""
    pts = state.get("points") or []
    if not pts:
        return False
    last = pts[-1]
    if str(last.get("time") or "") == now.strftime("%H:%M"):
        return False
    last_hr = _int(last.get("hr"), 0)
    if last_hr <= 0 or hr - last_hr < CALM_POINT_RISE_BPM:
        return False
    last_dt = _last_point_dt(state)
    if not last_dt or (now - last_dt).total_seconds() < CALM_POINT_GAP_MINUTES * 60:
        return False
    calm_count = sum(1 for p in pts if p.get("source") == CALM_POINT_SOURCE)
    return calm_count < CALM_POINT_DAILY_LIMIT


def apply_dialog_update(
    state: dict,
    parsed: dict,
    life: dict,
    now: datetime,
    decay_minutes: int,
    gap_minutes: int = 5,
    daily_limit: int = 20,
) -> tuple[dict, str]:
    """把主对话顺手报的心率并入状态。

    平静（强度 0/1）默认只更新当前值；若比最后一点高至少 6 bpm、
    间隔至少 10 分钟，则补一个 dialog_calm 点。
    强度 >=2 才抬峰、写点。同一波情绪 gap_minutes 内只保留一个点，取最高值。
    """
    emotion = normalize_emotion(parsed.get("emotion"))
    intensity = int(clamp(_int(parsed.get("emotion_intensity"), 0), 0, 3))
    if intensity == 0 and emotion in _HIGH_EMOTIONS:
        intensity = 1
    if intensity < 2:
        cur = state.setdefault("current", {})
        base = _int(life.get("base_hr"), 46)
        prev = _int(cur.get("hr"), 0) or decayed_hr(state, now, life, decay_minutes)
        llm_hr = _int(parsed.get("heart_rate"), prev)
        tier = resolve_tier(parsed, emotion, intensity)
        calm_hr = _tier_cap(int(clamp(max(llm_hr, base - 2), 38, 118)), tier)
        cur.update(
            {
                "hr": calm_hr,
                "base_hr": base,
                "emotion": emotion,
                "emotion_intensity": intensity,
                "tier": tier,
                "reason": str(parsed.get("reason") or "")[:80],
                "source": "dialog",
            }
        )
        calm_point_added = calm_dialog_point_due(state, now, calm_hr)
        if calm_point_added:
            _append_point(
                state,
                now,
                calm_hr,
                CALM_POINT_SOURCE,
                reason=str(parsed.get("reason") or ""),
                emotion=emotion,
                emotion_intensity=intensity,
            )
        state["last_generate_at"] = now_str(now)
        state["last_error"] = ""
        state["steps"] = max(
            _int(state.get("steps"), 0), _int(life.get("steps"), 0)
        )
        state["exercise_min"] = max(
            _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
        )
        return state, "calm_point" if calm_point_added else "calm"

    prev = decayed_hr(state, now, life, decay_minutes)
    base = _int(life.get("base_hr"), 46)
    tier = resolve_tier(parsed, emotion, intensity)
    new_hr = _compute_dialog_hr(prev, parsed, base, emotion, intensity, tier)
    reason = str(parsed.get("reason") or "")[:80]
    cur = state.setdefault("current", {})

    last_emotion = parse_dt(state.get("last_emotion_at")) or parse_dt(
        state.get("last_dialog_at")
    )
    same_wave = bool(
        last_emotion
        and 0 <= (now - last_emotion).total_seconds() < max(1, gap_minutes) * 60
    )
    dialog_points = [
        p for p in state.get("points") or [] if p.get("source") == "dialog"
    ]

    if not same_wave and len(dialog_points) >= max(1, daily_limit):
        result = "capped"
    elif same_wave:
        result = "peak"
    else:
        result = "point"

    old_peak = _int(cur.get("peak_hr"), new_hr)
    if new_hr >= old_peak:
        cur["peak_hr"] = new_hr
        cur["peak_at"] = now_str(now)
    else:
        cur["peak_hr"] = old_peak
    cur.update(
        {
            "hr": new_hr,
            "base_hr": base,
            "emotion": emotion,
            "emotion_intensity": intensity,
            "tier": tier,
            "reason": reason,
            "source": "dialog",
        }
    )

    if result == "point":
        protect = intensity >= 3 or new_hr > old_peak
        _append_point(
            state,
            now,
            new_hr,
            "dialog",
            reason,
            protect=protect,
            emotion=emotion,
            emotion_intensity=intensity,
        )
    elif result == "peak":
        # 同一波里只抬高已有的对话点，不新增点。
        pts = state.get("points") or []
        if pts and pts[-1].get("source") == "dialog":
            pts[-1]["hr"] = max(_int(pts[-1].get("hr"), new_hr), new_hr)
            pts[-1]["reason"] = reason[:60]
            pts[-1]["emotion"] = str(emotion or "")
            pts[-1]["emotion_intensity"] = _int(intensity, 0)
            state["last_point_at"] = now_str(now)

    state["last_emotion_at"] = now_str(now)
    state["last_dialog_at"] = now_str(now)
    state["last_generate_at"] = now_str(now)
    state["last_error"] = ""
    state["steps"] = max(_int(state.get("steps"), 0), _int(life.get("steps"), 0))
    state["exercise_min"] = max(
        _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
    )
    return state, result


def build_data_prompt(
    name: str,
    life: dict,
    now: datetime,
    user_hr: int,
    user_context: dict | None = None,
    decay_minutes: int = 25,
) -> tuple[str, str]:
    """数据触发的单次打包请求：同时产出共振心率与一句悄悄话。"""
    ctx = user_context or {}
    base = _int(life.get("base_hr"), 46)
    life_text = (
        f"时间：{now.strftime('%Y-%m-%d %H:%M')}；"
        f"场景：{life.get('label') or '日常在家'}；"
        f"今日摘要：{life.get('summary') or '无'}；"
        f"此刻在做：{life.get('schedule') or '未说明'}；"
        f"此刻心情：{life.get('mood') or '未说明'}；"
        f"基础心率：{base} bpm"
    )
    user_text = (
        f"她的最新心率：{user_hr} bpm；"
        f"她的静息心率：{ctx.get('resting_hr') or '未知'}；"
        f"今日步数：{ctx.get('steps') if ctx.get('steps') is not None else '未知'}；"
        f"今日睡眠：{ctx.get('sleep_min') if ctx.get('sleep_min') is not None else '未知'} 分钟"
    )
    system = (
        f"你是《恋与深空》里的「{name}」，此刻正在过自己的日常生活。"
        "你不是医疗助手，不做诊断，也不要说教。"
        "你感应到了她身体数据的波动，想轻轻关心她一句。"
        "悄悄话必须落在你此刻的真实情境里，不要把自己说成刚运动完的人。"
        "悄悄话只能报备或表达关心，必须是陈述句，严禁提问、追问、问号，"
        "严禁“问问、感觉怎么样、要不要、是不是、困不困、累不累”。"
        "只输出 JSON，不要 Markdown，不要解释。"
    )
    prompt = f"""当前生活状态：
{life_text}

她的身体数据：
{user_text}

请按你的生活状态回应她，生成：
1. emotion：只能是 平静、开心、吃醋、亲昵、激动、疲惫、运动 之一，数据共振通常用「激动」或「亲昵」；
2. emotion_intensity：0～3；
3. reason：二十字内，说明你为什么心跳变化；
4. whisper：一句给她的悄悄话，不超过 40 字，温柔、自然、贴合你此刻在做的事，不要医疗建议；
   只能是陈述句，不能提问，不能出现问号，也不能追问她的感受。

平复周期约 {decay_minutes} 分钟，不要写夸张的数值。
输出格式：
{{"emotion":"激动","emotion_intensity":2,"reason":"感应到你在用力","whisper":"心跳这么快，先慢一点，我在这儿。"}}"""
    return system, prompt


def apply_data_update(
    state: dict,
    life: dict,
    now: datetime,
    user_hr: int,
    whisper: str = "",
    emotion: str = "激动",
    reason: str = "",
    debounce_minutes: int = DATA_DEBOUNCE_MINUTES,
    whisper_cooldown_minutes: int = 30,
    allow_whisper: bool = True,
) -> tuple[dict, str]:
    """把小狗数据并入沈星回状态；防抖期内只更新衰减，不落新点。

    实测心率是映射源，不是他的心率：她 83 时他应约 48，而不是 83。
    """
    raw_user_hr = _int(user_hr, 0)
    if raw_user_hr <= 0:
        return state, "ignored"

    sync_sleep_bridge(state, now)
    base = _int(life.get("base_hr"), 46)
    cur = state.setdefault("current", {})
    target = data_resonant_hr(raw_user_hr, base)
    state["last_data_at"] = now_str(now)
    # 保留实测原值，迁移/排查时都拿它当映射源。
    state["last_data_hr"] = raw_user_hr

    if not data_debounce_ok(state, now, debounce_minutes):
        cur["hr"] = decayed_hr(state, now, life, 25)
        state["steps"] = max(_int(state.get("steps"), 0), _int(life.get("steps"), 0))
        state["exercise_min"] = max(
            _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
        )
        return state, "debounced"

    gain = data_resonance_delta(raw_user_hr)
    old_peak = _int(cur.get("peak_hr"), target)
    cur.update(
        {
            "hr": target,
            "base_hr": base,
            "peak_hr": max(old_peak, target),
            "peak_at": now_str(now) if target >= old_peak else cur.get("peak_at"),
            "emotion": normalize_emotion(emotion),
            "emotion_intensity": 2 if gain >= 10 else 1,
            "reason": str(reason or "感应到你的心跳")[:80],
            "source": "data",
        }
    )
    # 问句或追问直接判无效：不落日记，也不触发私语推送。
    # allow_whisper=False 时数据只更新共振，不生成、不落库任何私语。
    safe_whisper = sanitize_whisper(whisper) if allow_whisper else ""
    _append_point(
        state,
        now,
        target,
        "data",
        str(reason or "感应到你的心跳"),
        protect=gain >= 10,
        whisper=safe_whisper,
        user_hr=raw_user_hr,
        emotion=normalize_emotion(emotion),
        emotion_intensity=2 if gain >= 10 else 1,
    )
    state["last_data_trigger_at"] = now_str(now)
    state["steps"] = max(_int(state.get("steps"), 0), _int(life.get("steps"), 0))
    state["exercise_min"] = max(
        _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
    )
    if safe_whisper and data_whisper_due(state, now, whisper_cooldown_minutes):
        state["last_data_whisper_at"] = now_str(now)
        return state, "point_whisper"
    return state, "point"


def apply_llm_update(
    state: dict,
    parsed: dict,
    life: dict,
    now: datetime,
    decay_minutes: int,
    name: str,
) -> dict:
    """把一次 LLM 结果并入状态：抬峰、按轮次回落、限制变化幅度。"""
    prev = decayed_hr(state, now, life, decay_minutes)
    base = _int(life.get("base_hr"), 46)
    emotion = str(parsed.get("emotion") or "平静").strip()[:8]
    emotion = {
        "亲亲": "亲昵",
        "亲吻": "亲昵",
        "亲热": "亲昵",
        "醋意": "吃醋",
    }.get(emotion, emotion)
    if emotion not in EMOTIONS:
        # 模型偶尔会写别的近义词，保留原文但强度按低档处理。
        emotion = emotion or "平静"
    intensity = int(clamp(_int(parsed.get("emotion_intensity"), 0), 0, 3))
    if intensity == 0 and emotion in _HIGH_EMOTIONS:
        intensity = 1

    llm_hr = _int(parsed.get("heart_rate"), prev)
    target = int(clamp(max(llm_hr, base - 2), 38, 120))

    tier = resolve_tier(parsed, normalize_emotion(emotion), intensity)

    if emotion in ("亲昵", "吃醋"):
        # 这两个档位按“相对当前心率的增量”走：
        # 亲昵 +20~35，吃醋 +10~20；最终落点由下面的 tier 收口。
        lo, hi = (20, 35) if emotion == "亲昵" else (10, 20)
        if intensity <= 1:
            step = lo
        elif intensity == 2:
            step = (lo + hi) // 2
        else:
            step = hi
        new_hr = prev + step
    elif intensity >= 2 and target >= prev - 6:
        rise_cap = 35 if intensity >= 3 else 25
        delta = clamp(target - prev, -6, rise_cap)
        new_hr = prev + delta
    else:
        # 平静/困倦等回落：每轮最多降 24，2~3 轮回到基准，不做断崖。
        delta = clamp((target - prev) * 0.45, -24, 15)
        new_hr = prev + delta

    new_hr = int(clamp(max(new_hr, base - 2), 38, 118))
    if emotion not in ("运动", "疲惫"):
        # 情绪档统一按梯级收上限；运动和疲惫走自己的生理区间。
        new_hr = _tier_cap(new_hr, tier)
    cur = state.setdefault("current", {})
    cur.update(
        {
            "hr": new_hr,
            "base_hr": base,
            "peak_hr": new_hr,
            "peak_at": now_str(now),
            "emotion": emotion,
            "emotion_intensity": intensity,
            "tier": tier,
            "reason": str(parsed.get("reason") or "")[:80],
            "source": "dialog",
        }
    )

    profile = state.setdefault("profile", {})
    if not profile.get("resting_hr"):
        rest = parsed.get("resting_hr")
        # 缺省用中性静息，不拿运动档的基础心率冒充静息。
        profile["resting_hr"] = int(clamp(_int(rest, 46), 38, 55))
    if can_report_sleep(now):
        sleep = parsed.get("sleep_minutes")
        estimated = False
        bridge_night = bridge_night_sleep_min(now)
        if bridge_night:
            # 生活状态插件给了昨夜真实值，直接用它，不再自己估算。
            sleep = bridge_night
            estimated = False
        elif sleep is None and not _int(profile.get("sleep_min"), 0):
            # 模型没给、也没有桥接数据，才按当天生活状态给个稳定的估算值。
            sleep = _guess_sleep_minutes(state, now)
            estimated = True
        if sleep is not None:
            profile["sleep_min"] = int(clamp(_int(sleep, 0), 180, 720))
            profile["sleep_date"] = now.strftime("%Y-%m-%d")
            profile["sleep_at"] = now_str(now)
            profile["sleep_estimated"] = estimated
            profile["sleep_source"] = "bridge" if bridge_night else "llm"

    state["steps"] = max(_int(state.get("steps"), 0), _int(life.get("steps"), 0))
    state["exercise_min"] = max(
        _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
    )
    _append_point(
        state,
        now,
        new_hr,
        "dialog",
        cur["reason"],
        protect=intensity >= 3,
        emotion=emotion,
        emotion_intensity=intensity,
    )
    state["last_dialog_at"] = now_str(now)
    state["last_generate_at"] = now_str(now)
    state["last_error"] = ""
    return state


def _last_dialog_point_dt(state: dict) -> datetime | None:
    """最近一个「情绪点」的完整时间；静息补点前用它做 30 分钟保护。"""
    date_str = str(state.get("date") or "")
    for p in reversed(state.get("points") or []):
        if p.get("source") not in ("dialog", "data"):
            continue
        hm = str(p.get("time") or "")
        try:
            return datetime.strptime(f"{date_str} {hm}:00", "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
    return None


def auto_rest_due(
    state: dict,
    now: datetime,
    hours: int,
    recent_guard_minutes: int = 30,
) -> bool:
    """是否该补静息点。

    - 距上一次真人对话满 hours 小时，且距上一次自动补点也满 hours 小时；
    - 且最近 recent_guard_minutes 分钟内没有新的情绪点（避免刚心动完又补一个平静点）。
    """
    if hours <= 0:
        return False
    pts = state.get("points") or []
    last_chat = parse_dt(state.get("last_chat_at"))
    if not pts and not last_chat:
        return False

    # 只要最近说过话，就还没到「无对话 2 小时」。
    if last_chat and (now - last_chat).total_seconds() < hours * 3600:
        return False

    last_auto = parse_dt(state.get("last_auto_at"))
    if last_auto:
        if (now - last_auto).total_seconds() < hours * 3600:
            return False
    else:
        last = last_chat or parse_dt(state.get("last_point_at")) or parse_dt(
            state.get("updated_at")
        )
        if not last or (now - last).total_seconds() < hours * 3600:
            return False

    # 真正拦住补点的是「最近一个情绪点」，不是最后一次情绪标签。
    guard = _last_dialog_point_dt(state)
    if not guard:
        guard = parse_dt(state.get("last_emotion_at")) or parse_dt(
            state.get("last_dialog_at")
        )
    if guard and (now - guard).total_seconds() < max(0, recent_guard_minutes) * 60:
        return False
    return True


def append_auto_rest(
    state: dict, life: dict, now: datetime, decay_minutes: int
) -> dict:
    hr = decayed_hr(state, now, life, decay_minutes)
    cur = state.setdefault("current", {})
    cur.update(
        {
            "hr": hr,
            "base_hr": _int(life.get("base_hr"), hr),
            "peak_hr": hr,
            "peak_at": now_str(now),
            "emotion": "平静",
            "emotion_intensity": 0,
            "reason": str(life.get("label") or ""),
            "source": "auto_rest",
        }
    )
    _append_point(
        state,
        now,
        hr,
        "auto_rest",
        str(life.get("label") or ""),
        emotion="平静",
        emotion_intensity=0,
    )
    state["last_auto_at"] = now_str(now)
    state["steps"] = max(_int(state.get("steps"), 0), _int(life.get("steps"), 0))
    state["exercise_min"] = max(
        _int(state.get("exercise_min"), 0), _int(life.get("exercise_min"), 0)
    )
    return state


def public_snapshot(
    state: dict,
    life: dict,
    now: datetime,
    decay_minutes: int,
    fallback_status: str = "正在同步",
) -> dict:
    if not state:
        return {
            "available": False,
            "name": "沈星回",
            "status": fallback_status,
            "points": [],
            "current_hr": None,
            "base_hr": _int(life.get("base_hr"), 46),
            "resting_hr": None,
            "sleep_min": None,
            "steps": _int(life.get("steps"), 0),
            "exercise_min": _int(life.get("exercise_min"), 0),
        }
    migrate_legacy_mapping(state, life)
    cur = state.get("current") or {}
    profile = state.get("profile") or {}
    hr = _int(cur.get("hr"), 0) or decayed_hr(state, now, life, decay_minutes)
    points = []
    for p in state.get("points") or []:
        try:
            points.append(
                {
                    "time": str(p.get("time")),
                    "x": minutes_of_day(str(p.get("time"))),
                    "hr": _int(p.get("hr")),
                    "user_hr": _int(p.get("user_hr")) or None,
                    "source": p.get("source") or "dialog",
                    "reason": p.get("reason") or "",
                    "whisper": p.get("whisper") or "",
                    "emotion": p.get("emotion") or "",
                    "emotion_intensity": _int(p.get("emotion_intensity"), 0),
                    "locked": bool(p.get("locked")),
                    "peak": bool(p.get("peak", p.get("source") in ("dialog", "data"))),
                }
            )
        except Exception:
            continue
    current_point = None
    if hr:
        now_hm = now.strftime("%H:%M")
        current_point = {
            "time": now_hm,
            "x": minutes_of_day(now_hm),
            "hr": _int(hr),
            "source": "now",
            "reason": "现在",
        }
    rest_hr = profile.get("resting_hr")
    bridge_total = bridge_sleep_min(now)
    if not rest_hr:
        rest_hr = int(clamp(_int(life.get("base_hr"), 46) - 8, 38, 55))
    return {
        "available": True,
        "date": state.get("date"),
        "name": state.get("name") or "沈星回",
        "current_hr": hr,
        "base_hr": _int(life.get("base_hr"), 46),
        "resting_hr": rest_hr,
        "sleep_min": bridge_total or valid_sleep_min(profile, now, state),
        "sleep_estimated": (False if bridge_total else bool(profile.get("sleep_estimated"))),
        "steps": _int(state.get("steps"), 0),
        "exercise_min": _int(state.get("exercise_min"), 0),
        "emotion": cur.get("emotion") or "平静",
        "emotion_intensity": _int(cur.get("emotion_intensity"), 0),
        "reason": cur.get("reason") or "",
        "source": cur.get("source") or "seed",
        "status": (
            "静息"
            if cur.get("source") == "auto_rest"
            else (
                "心动"
                if cur.get("source") == "dialog"
                else ("同频" if cur.get("source") == "data" else fallback_status)
            )
        ),
        "last_error": state.get("last_error") or "",
        "life_label": life.get("label") or "",
        "points": points,
        "current_point": current_point,
    }


def build_prompt(
    name: str,
    now: datetime,
    recent: list[dict],
    life: dict,
    prev: dict,
    decay_minutes: int,
) -> str:
    lines = []
    for item in recent[-8:]:
        role = "她" if item.get("role") == "user" else name
        text = re.sub(r"\[[^\]]{1,24}\]", "", str(item.get("text") or ""))
        text = re.sub(r"\s+", " ", text).strip()[:220]
        if text:
            lines.append(f"{role}：{text}")
    dialog = "\n".join(lines) if lines else "（还没有对话）"

    prev_hr = prev.get("current_hr")
    prev_emotion = prev.get("emotion") or "平静"
    life_text = (
        f"{life.get('label', '日常在家')}；"
        f"今日摘要：{life.get('summary') or '无'}；"
        f"此刻在做：{life.get('schedule') or '未说明'}；"
        f"心情：{life.get('mood') or '未说明'}；"
        f"基础心率约 {life.get('base_hr', 46)} bpm"
    )
    if can_report_sleep(now):
        bridge_night = bridge_night_sleep_min(now)
        if bridge_night:
            sleep_rule = (
                f"- resting_hr 40～50；sleep_minutes 已有真实值 {bridge_night}"
                "（昨夜睡眠），照抄它，不要自己改数字"
            )
            sleep_field = f'"sleep_minutes": {bridge_night},'
        else:
            sleep_rule = "- resting_hr 40～50；sleep_minutes 420～540（约 7～9 小时，只指昨夜睡眠）"
            sleep_field = '"sleep_minutes": 500,'
    else:
        sleep_rule = "- resting_hr 40～50；现在是夜间或非晨间，不输出 sleep_minutes，也不要猜睡眠时长"
        sleep_field = ""
    return f"""你是「{name}」的虚构身体状态镜像，不是医疗建议，也不要扮演聊天角色。
根据最近对话、当前生活状态和上一状态，生成此刻的一组身体读数。
只输出一个 JSON 对象，不要 Markdown，不要解释。

情绪档位（必须遵守，按唤醒梯级）：
- 平静/日常：40～50 bpm
- 开心、被夸：+8～15，常见 50～65
- daily 日常温存/习惯性亲亲：65～75 bpm，基准约 70，不要写成虚高
- intimate 深情表白/吃醋/突发心动：60～85 bpm
- intense 感官沉浸/深度情欲：80～110 bpm，峰值不超过 110
- 运动/爬山：70～90 bpm
- 单轮不要无故大幅跳变；没有新刺激时写回基础附近，由插件负责平滑回落
- 不能每轮都吃醋、每轮都亲昵；普通闲聊就写平静或开心
- 心率下限 40，上限 110；运动档不要低于 70
{sleep_rule}
- emotion 只能是：平静、开心、吃醋、亲昵、激动、疲惫、运动
- emotion_intensity 0～3

上一状态：{prev_hr if prev_hr is not None else "暂无"} bpm，{prev_emotion}
生活状态：{life_text}
当前时间：{now.strftime("%Y-%m-%d %H:%M")}
平复周期：约 {decay_minutes} 分钟

最近对话：
{dialog}

输出格式（只输出这个 JSON）：
{{
  "heart_rate": 46,
  "resting_hr": 43,
  {sleep_field}
  "step_target": 4200,
  "exercise_minutes": 25,
  "emotion": "平静",
  "emotion_intensity": 0,
  "reason": "二十字内说明"
}}"""
