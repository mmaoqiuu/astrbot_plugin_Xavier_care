"""Xavier_care 聊天内自然关怀（v1.9.1）。

为什么要多这一条路
------------------
主动推送那条路的闸门里有一道「对话互斥」——她刚说完话的几分钟内不插播。
这道闸门本身是对的（否则会出现「她刚说完，机器人立刻另起一句」的自相矛盾），
但副作用是：她一天里最需要被惦记的时候（正跟你/朋友说着话），关怀反而最
容易被拦下，最后变成「她安静了才收到一条」。

所以补第二条路：不新增任何消息，只把「今天可以顺口提一句」的要点注入到她
这一轮 LLM 请求的系统提示里，由角色自己在回复里自然带出来。

两层内容
--------
1. 语境行（每天照带）：在经期第 N 天 / 距预测经期约 X 天。
   作用是让角色知道「现在是什么日子」，语气自己会软下来，不用她开口提。
2. 可选关怀点（最多 max_items 条）：今天命中的异常里，既没被主动推送推过、
   也不在静默期内的，取原始 hint 压成一句。

去重（防复读机）
----------------
- 同一类型注入后 chat_hint_ttl_hours 小时内不再注入；
- 当天已经被主动推送推过的类型不再注入（推过就不用再念叨一遍）；
- 反向去重：注入过的类型当天不再走主动推送（main 的 monitor tick 查询
  mentioned_types_today() 判断），避免「刚聊过又被单独推一条」；
- 语境行不参与去重：经期期间每天带，天数本身就在变，不会显得重复。

设计取舍
--------
- 不注入任何原始数值（步数 / 血氧 / 心率曲线），那些留给 check_health 工具：
  既省 token，也避免角色变成播报机器人。
- 注入是「软提示」：里面明确写了「可以完全不提」，不强制、不占每日额度、
  不计冷却、不参与主动推送的任何闸门判定。
- 「是否已经提过」用「是否已注入」近似：我们无法判断模型有没有真的说出口，
  所以只要注入了就当作提过，宁少不重复。
- 状态文件原子写入，坏文件一律当空处理——关怀功能不该因为她手机上少一条
  数据而崩掉。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 状态文件：记录每个要点「上次注入时间」，同时供反向去重查「今天提过哪些」。
STATE_FILE = "chat_hint_state.json"
# 主动推送的日志，用来判断「今天哪些类型已经推过了」。
CARE_LOG_FILE = "care_log.jsonl"

# 经期相关类型不单独作为「关怀点」：它们已由语境行每天覆盖，
# 再列一条只会让同一句话在提示里出现两遍。
PERIOD_TYPES: frozenset[str] = frozenset(
    {"period_started", "period_daily", "period_soon"}
)

HEAD = "【今日关怀备忘｜内部信息，别照念】"
FOOT = (
    "【怎么用】顺着当前话题自然地带一句关心就好，最多带一条；"
    "别为了它另起话题，别写成健康报告，绝对不要说「我监测到 / 数据显示 / "
    "检测到异常」这类词；对方正在说正事、或者情绪不好时，可以完全不提；"
    "同一件事不要反复叮嘱。"
)


# ---------- 时间 / 状态文件 -------------------------------------------------

def date_key(ts: float | None = None) -> str:
    """时间戳 → 本地日期字符串（YYYY-MM-DD）。"""
    t = float(ts if ts is not None else time.time())
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def load_state(path: Path) -> dict[str, Any]:
    """读状态文件。文件不存在、内容损坏都返回空结构，绝不抛异常。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"mentioned": {}}
        if not isinstance(data.get("mentioned"), dict):
            data["mentioned"] = {}
        return data
    except FileNotFoundError:
        return {"mentioned": {}}
    except Exception:
        logger.exception("[Xavier_care] 读聊天关怀状态失败，按空状态处理")
        return {"mentioned": {}}


def save_state(path: Path, state: dict[str, Any]) -> bool:
    """原子写状态文件（临时文件 + os.replace）。失败只记日志。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        logger.exception("[Xavier_care] 写聊天关怀状态失败（不影响本次回复）")
        return False


def prune_mentioned(
    mentioned: dict[str, Any],
    now: float | None = None,
    ttl_hours: float = 6.0,
) -> dict[str, float]:
    """清掉过期记录：既不在 TTL 内、又已经不是今天的，就丢掉。

    注意刻意保留「今天」的记录——哪怕 TTL 只有 1 小时，反向去重也要靠
    「今天提过没有」来判断，不能因为 TTL 过期就把当天记录删了。
    """
    now = float(now if now is not None else time.time())
    ttl_seconds = max(0.0, float(ttl_hours)) * 3600
    today = date_key(now)
    out: dict[str, float] = {}
    for k, v in (mentioned or {}).items():
        try:
            ts = float(v)
        except (TypeError, ValueError):
            continue
        if ts > now:
            ts = now
        if (now - ts) < ttl_seconds or date_key(ts) == today:
            out[str(k)] = ts
    return out


def mentioned_types_today(
    state: dict[str, Any],
    now: float | None = None,
) -> set[str]:
    """今天已经注入过（≈ 已经顺口提过）的类型集合，供反向去重用。"""
    now = float(now if now is not None else time.time())
    today = date_key(now)
    out: set[str] = set()
    for k, v in (state.get("mentioned") or {}).items():
        try:
            ts = float(v)
        except (TypeError, ValueError):
            continue
        if date_key(ts) == today:
            out.add(str(k))
    return out


def pushed_types_today(care_log_path: Path, now: float | None = None) -> set[str]:
    """今天已经「主动推送成功」过的异常类型集合。

    读 care_log.jsonl：只看 ok=True 且 ts 是今天的行。文件不存在/坏行都跳过。
    """
    today = date_key(now)
    path = Path(care_log_path)
    out: set[str] = set()
    try:
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if not j.get("ok"):
                continue
            if str(j.get("ts", ""))[:10] != today:
                continue
            t = str(j.get("type") or "").strip()
            if t:
                out.add(t)
    except Exception:
        logger.exception("[Xavier_care] 读关怀日志失败，按「今天没推过」处理")
    return out


# ---------- 提示词拼装 -----------------------------------------------------

def period_context_line(period: dict[str, Any] | None, soon_days: int = 2) -> str:
    """经期语境行：一行，让角色知道「现在是什么日子」。"""
    if not period:
        return ""
    try:
        if period.get("in_period"):
            day = period.get("period_day") or 1
            return f"她正在经期第 {day} 天，身体容易累、怕凉，肚子可能不舒服。"
        left = period.get("days_to_next")
        if isinstance(left, int) and 1 <= left <= max(1, int(soon_days)):
            return (
                f"距预测经期大约还有 {left} 天，"
                f"这几天她可能容易累、腰酸、情绪起伏。"
            )
    except Exception:
        logger.exception("[Xavier_care] 生成经期语境行失败，跳过该行")
    return ""


def pick_points(
    alerts: list[dict[str, Any]] | None,
    mentioned: dict[str, Any] | None,
    pushed: set[str] | None = None,
    now: float | None = None,
    ttl_hours: float = 6.0,
    max_items: int = 2,
) -> list[dict[str, Any]]:
    """挑出可以顺口提一句的关怀点（保持原始顺序，最多 max_items 条）。"""
    now = float(now if now is not None else time.time())
    ttl_seconds = max(0.0, float(ttl_hours)) * 3600
    pushed = pushed or set()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        t = str(a.get("type") or "").strip()
        hint = str(a.get("hint") or "").strip()
        if not t or not hint or t in seen:
            continue
        if t in PERIOD_TYPES:
            # 经期由语境行覆盖，不重复列点
            continue
        if t in pushed:
            # 今天已经主动推送过：不再念叨
            continue
        try:
            last = float((mentioned or {}).get(t, 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last and (now - last) < ttl_seconds:
            # 静默期内：同一个要点不反复注入
            continue
        seen.add(t)
        out.append({"type": t, "hint": hint})
        if len(out) >= max(1, int(max_items)):
            break
    return out


def build_hint(
    alerts: list[dict[str, Any]] | None,
    period: dict[str, Any] | None,
    mentioned: dict[str, Any] | None = None,
    pushed: set[str] | None = None,
    now: float | None = None,
    ttl_hours: float = 6.0,
    max_items: int = 2,
    soon_days: int = 2,
) -> tuple[str, list[str]]:
    """拼出注入文本。返回 (文本, 本次用到的类型列表)；没内容时返回 ("", [])。"""
    lines: list[str] = []
    used: list[str] = []

    ctx = period_context_line(period, soon_days=soon_days)
    if ctx:
        lines.append(f"- {ctx}")
        # 语境行不进 used：它不受 TTL 限制，也不该挡住主动推送
    for p in pick_points(
        alerts,
        mentioned=mentioned,
        pushed=pushed,
        now=now,
        ttl_hours=ttl_hours,
        max_items=max_items,
    ):
        lines.append(f"- {p['hint']}")
        used.append(p["type"])

    if not lines:
        return "", []
    return "\n".join([HEAD, *lines, FOOT]), used


class ChatCareInjector:
    """把上面的纯逻辑接到配置和状态文件上（每轮回复调用一次）。"""

    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config
        self.state_path = self.data_dir / STATE_FILE
        self.care_log_path = self.data_dir / CARE_LOG_FILE

    # -- 小工具：配置读取容错（配置项缺失/类型不对都不该崩） ----------------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            v = self.config.get(key)
            return default if v is None else bool(v)
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            v = self.config.get(key)
            return default if v is None else int(v)
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            v = self.config.get(key)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    # -- 对外 ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._cfg_bool("care_in_chat_enabled", True)

    def mentioned_types_today(self) -> set[str]:
        """今天注入过（≈ 聊到过）的类型，供反向去重。"""
        return mentioned_types_today(load_state(self.state_path))

    def render(
        self,
        alerts: list[dict[str, Any]] | None,
        period: dict[str, Any] | None,
    ) -> tuple[str, list[str]]:
        """生成注入文本并记账。返回 (文本, 本次用到的类型)。"""
        now = time.time()
        ttl = self._cfg_float("chat_hint_ttl_hours", 6.0)
        max_items = self._cfg_int("chat_hint_max_items", 2)
        soon_days = self._cfg_int("period_soon_days", 2)

        state = load_state(self.state_path)
        mentioned = prune_mentioned(
            state.get("mentioned") or {}, now=now, ttl_hours=ttl
        )
        pushed = pushed_types_today(self.care_log_path, now=now)

        text, used = build_hint(
            alerts,
            period,
            mentioned=mentioned,
            pushed=pushed,
            now=now,
            ttl_hours=ttl,
            max_items=max_items,
            soon_days=soon_days,
        )

        # 记账：无论这次文本里有没有点，都把修剪后的 mentioned 落盘，
        # 免得过期记录一直堆在文件里。
        for t in used:
            mentioned[t] = now
        state["mentioned"] = mentioned
        state["last_inject_ts"] = now if text else state.get("last_inject_ts", 0)
        state["last_used"] = used
        save_state(self.state_path, state)
        return text, used
