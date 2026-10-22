"""奶茶记录 —— 纯逻辑层。

这里不碰 AstrBot 运行时，只用标准库，方便单独跑测试。

数据落在「当天的健康 JSON」里（新增 milktea 字段），所以奶茶的日期
天然和体重曲线、经期标记对齐，不用另开一套存储。

几条底线：
- 写入一律「读 → 只改 milktea → 原子替换」，手机推送健康数据时两边互不覆盖；
- 坏输入直接拒绝，不先写进去再清理；
- 评分可以是空的，空就是真的没有——不补 0，也不补星星；
- 老文件没有 milktea 字段时按空列表处理，不做数据迁移。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:  # 作为插件包里的子模块加载
    from .health_logic import is_valid_date
except ImportError:  # pragma: no cover - 兜底，兼容直接以顶层模块加载
    from health_logic import is_valid_date


SOURCE_CHAT = "chat"
SOURCE_MANUAL = "manual"

LIMIT_BRAND = 40
LIMIT_NAME = 60
LIMIT_SUGAR_TEXT = 16
LIMIT_ICE = 16
LIMIT_NOTE = 300
LIMIT_TOPPING = 16
MAX_TOPPINGS = 6
MAX_PER_DAY = 10

SUGAR_LEVELS = ("none", "light", "half", "seven", "full")

# 长匹配放前面：「少少糖」必须先于「少糖」被判成七分，否则会串档。
_SUGAR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("none", ("无糖", "不要糖", "不加糖", "去糖", "零糖", "0糖")),
    ("seven", ("少少糖", "七分糖", "七分")),
    ("half", ("五分糖", "五分", "半糖")),
    ("light", ("三分糖", "三分", "微糖", "少糖", "二分糖", "二分")),
    ("full", ("全糖", "正常糖", "标准糖", "满糖")),
)


# ---------------------------------------------------------------------------
# 规整输入
# ---------------------------------------------------------------------------


def normalize_sugar(text: str) -> str:
    """把「三分糖」这类原话映射成标准档；认不出来返回空串。"""
    t = (text or "").strip()
    if not t:
        return ""
    for level, hints in _SUGAR_HINTS:
        if any(h in t for h in hints):
            return level
    return ""


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


# 品牌别名只做精确匹配，避免 KOI 作为子串误伤其他店名。
_BRAND_ALIASES = {
    "koi": "KOI",
    "koithe": "KOI",
}


def canonical_brand(value: Any) -> str:
    """把同一品牌的不同写法收成一个名字；未收录的品牌原样返回。"""
    brand = _text(value, LIMIT_BRAND)
    if not brand:
        return ""
    key = unicodedata.normalize("NFKD", brand)
    key = "".join(ch for ch in key if not unicodedata.combining(ch))
    key = re.sub(r"[\s·._-]+", "", key).casefold()
    return _BRAND_ALIASES.get(key, brand)


def _toppings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[、,，/　\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        return []
    out: list[str] = []
    for p in parts:
        p = _text(p, LIMIT_TOPPING)
        if p and p not in out:
            out.append(p)
        if len(out) >= MAX_TOPPINGS:
            break
    return out


def _rating(value: Any) -> int | None:
    """评分：没给就留空，不做 0 分兜底。"""
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def normalize_entry(
    raw: dict,
    source: str = SOURCE_MANUAL,
    now: datetime | None = None,
) -> dict | None:
    """把外部输入规整成一条合法记录；品牌和品种都空就返回 None。"""
    if not isinstance(raw, dict):
        return None

    brand = canonical_brand(raw.get("brand"))
    name = _text(raw.get("name") or raw.get("drink"), LIMIT_NAME)
    if not brand and not name:
        return None

    now = now or datetime.now()
    sugar_text = _text(raw.get("sugar_text") or raw.get("sugar"), LIMIT_SUGAR_TEXT)

    return {
        "id": _text(raw.get("id"), 16) or uuid.uuid4().hex[:8],
        "time": _text(raw.get("time"), 5) or now.strftime("%H:%M"),
        "brand": brand,
        "name": name,
        "sugar_text": sugar_text,
        "sugar_level": normalize_sugar(sugar_text),
        "ice": _text(raw.get("ice"), LIMIT_ICE),
        "toppings": _toppings(raw.get("toppings")),
        "rating": _rating(raw.get("rating")),
        "note": _text(raw.get("note"), LIMIT_NOTE),
        "source": source if source in (SOURCE_CHAT, SOURCE_MANUAL) else SOURCE_MANUAL,
        "created_at": now.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 读写当天文件
# ---------------------------------------------------------------------------


def load_day(data_dir: Path, date_str: str) -> list[dict]:
    """读某天的奶茶记录；没有或读坏了一律当空列表，不抛异常。"""
    if not is_valid_date(date_str):
        return []
    target = Path(data_dir) / f"{date_str}.json"
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("milktea") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item = dict(it)
        item["brand"] = canonical_brand(item.get("brand"))
        out.append(item)
    return out


def save_day(data_dir: Path, date_str: str, items: list[dict]) -> bool:
    """原子写回当天文件：只改 milktea，其余字段（健康数据、经期标记）原样保留。"""
    if not is_valid_date(date_str):
        return False
    data_dir = Path(data_dir)
    target = data_dir / f"{date_str}.json"

    merged: dict = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged = loaded
        except Exception:
            merged = {}
    merged["milktea"] = items

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f".{date_str}.json.tmp"
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def add_entry(
    data_dir: Path,
    date_str: str,
    raw: dict,
    source: str = SOURCE_MANUAL,
    now: datetime | None = None,
) -> dict | None:
    """加一条，返回落库后的记录；日期非法 / 内容太空 / 当天超额都返回 None。"""
    if not is_valid_date(date_str):
        return None
    entry = normalize_entry(raw, source=source, now=now)
    if entry is None:
        return None
    items = load_day(data_dir, date_str)
    if len(items) >= MAX_PER_DAY:
        return None
    items.append(entry)
    return entry if save_day(data_dir, date_str, items) else None


def delete_entry(data_dir: Path, date_str: str, entry_id: Any) -> bool:
    """按 id 删一条。找不到对应 id 返回 False。"""
    entry_id = _text(entry_id, 16)
    if not entry_id or not is_valid_date(date_str):
        return False
    items = load_day(data_dir, date_str)
    kept = [it for it in items if _text(it.get("id"), 16) != entry_id]
    if len(kept) == len(items):
        return False
    return save_day(data_dir, date_str, kept)


# ---------------------------------------------------------------------------
# 查询与汇总
# ---------------------------------------------------------------------------


def is_future(date_str: str, today: date | None = None) -> bool:
    """补记只允许记到「今天」为止，不给未来记账。"""
    if not is_valid_date(date_str):
        return False
    today = today or date.today()
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date() > today
    except ValueError:
        return False


def month_dates(data_dir: Path, ym: str) -> list[str]:
    """某月里存在健康文件（或奶茶文件）的日期，升序。"""
    if not re.fullmatch(r"\d{4}-\d{2}", ym or ""):
        return []
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return []
    return sorted(p.stem for p in data_dir.glob(f"{ym}-*.json") if is_valid_date(p.stem))


def month_days(data_dir: Path, ym: str) -> dict[str, int]:
    """{日期: 杯数}，只含有记录的日期。"""
    out: dict[str, int] = {}
    for d in month_dates(data_dir, ym):
        n = len(load_day(data_dir, d))
        if n:
            out[d] = n
    return out


def month_summary(data_dir: Path, ym: str) -> dict:
    """本月汇总：每天杯数、总杯数、最常喝的牌子。"""
    days = month_days(data_dir, ym)
    brands: dict[str, int] = {}
    for d in days:
        for it in load_day(data_dir, d):
            b = _text(it.get("brand"), LIMIT_BRAND) or _text(it.get("name"), LIMIT_NAME)
            if b:
                brands[b] = brands.get(b, 0) + 1
    favorite = max(brands.items(), key=lambda kv: kv[1])[0] if brands else ""
    return {"days": days, "total": sum(days.values()), "favorite": favorite}


def recent_days(
    data_dir: Path,
    days: int = 7,
    today: date | None = None,
) -> list[tuple[str, list[dict]]]:
    """最近 N 天里有记录的日期和条目，日期倒序。"""
    today = today or date.today()
    span = max(1, min(int(days or 7), 90))
    out: list[tuple[str, list[dict]]] = []
    for i in range(span):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        items = load_day(data_dir, d)
        if items:
            out.append((d, items))
    return out


def describe_day(date_str: str, items: list[dict]) -> str:
    """一天的记录，拼成人话（给角色看）。"""
    if not items:
        return f"{date_str} 没有奶茶记录。"
    lines = [f"{date_str}："]
    for it in items:
        head = "·".join(x for x in (_text(it.get("brand"), 40), _text(it.get("name"), 60)) if x)
        bits = [_text(it.get("sugar_text"), 16), _text(it.get("ice"), 16)]
        tops = it.get("toppings")
        if isinstance(tops, list) and tops:
            bits.append("加" + "、".join(str(t) for t in tops))
        detail = " ".join(x for x in bits if x)
        line = f"  {_text(it.get('time'), 5)} {head}"
        if detail:
            line += f"（{detail}）"
        rating = it.get("rating")
        if rating:
            line += f" {rating}星"
        note = _text(it.get("note"), 120)
        if note:
            line += f"，她的点评：{note}"
        lines.append(line)
    return "\n".join(lines)


def describe_recent(data_dir: Path, days: int = 7, today: date | None = None) -> str:
    """最近 N 天的汇总文本，给 query_milktea 用。"""
    span = max(1, min(int(days or 7), 90))
    found = recent_days(data_dir, span, today)
    if not found:
        return f"最近 {span} 天没有奶茶记录。"
    total = sum(len(items) for _, items in found)
    head = f"最近 {span} 天共 {total} 杯，记录在 {len(found)} 天里："
    body = "\n".join(describe_day(d, items) for d, items in found)
    return f"{head}\n{body}"
