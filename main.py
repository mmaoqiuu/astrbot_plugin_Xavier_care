"""Xavier_care —— 健康数据接入插件（AstrBot v4.25.1）。

三件事：
  模块一：开一个独立的 HTTP 端点，接住手机推来的当天健康数据并存成文件。
  模块二：给主 LLM（角色）挂一个函数工具 check_health，让角色能读最近的身体数据。
  模块三：后台 monitor —— 基于历史基线做异常检测，发现异常时主动推送关怀。

为什么自己用 aiohttp 开监听、而不用 AstrBot 的 register_web_api：
  register_web_api 注册的路由挂在「管理面板」那个 web 服务上（默认 6185 端口），
  且会被面板的登录鉴权（JWT）拦截。手机端不可能带着面板登录令牌来上报，
  把 6185 暴露到公网又等于把整个管理后台暴露出去。所以这里按 HANDOFF 第五节的
  备选方案，自开一个独立端口（默认 8787）的轻量监听，自包含、只暴露这一个端点。
  （已对照 v4.25.1 源码 astrbot/dashboard/server.py 的 auth_middleware 确认。）

纯逻辑（解析/存储/读取/格式化）都在 health_logic.py，便于脱离 AstrBot 本地测。
本文件只负责把那些逻辑接到 AstrBot 的网络监听和工具注册上。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web


from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import chat_care, health_logic, milktea, mirror_life, mirror_logic
from .care_guard import CareGuard
from .config_compat import migrate_and_wrap
from .mirror_store import MirrorStore
from .monitor import HealthMonitor

# 插件目录名，同时用作数据子目录名。数据存在 AstrBot 的 data 目录下，
# 不放插件自身目录，这样更新/重装插件不丢历史数据。
PLUGIN_DIR_NAME = "astrbot_plugin_Xavier_care"

# 插件更名前使用的目录名，仅用于历史数据的一次性迁移。
LEGACY_DIR_NAME = "astrbot_plugin_health_bridge"

# 默认只在下面这个会话里工作（面板「固定目标会话」可改）。
DEFAULT_TARGET_UMO = "sxh:FriendMessage:3800806724"

# 接收端点路径。固定值；真正的门锁是 auth_token，不是这个路径。
ENDPOINT_PATH = "/health/report"

# 鉴权请求头名。手机上报时带 X-Auth-Token: <auth_token>。
AUTH_HEADER = "X-Auth-Token"

# 请求体上限（健康数据只有几百字节，给到 64KB 足够，挡掉超大请求）。
MAX_BODY_BYTES = 64 * 1024

# 主动关怀注入事件时用的发送者昵称。用来把「自己注入的伪消息」和
# 「她真的说了话」区分开——否则注入会被判成对话活跃，把闸门自己锁死。
INJECT_SENDER_NAME = "event_bridge"


@register(PLUGIN_DIR_NAME, "pupotato", "接收手机推送的健康数据并存储，提供一个工具供角色读取最近的身体状态。", "2.2.0")
class HealthBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = migrate_and_wrap(config)
        # data/plugin_data/astrbot_plugin_Xavier_care/<date>.json
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_DIR_NAME
        # aiohttp 监听相关对象，启动后赋值，便于 terminate 时干净关闭。
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._listen_port: int = 0
        self._listen_error: str = ""
        self._last_event: AstrMessageEvent | None = None
        self._pending_mirror_record: dict | None = None
        self._mirror_data_task: asyncio.Task | None = None

        # 健康异常检测 + 主动关怀
        self.monitor = HealthMonitor(data_dir=self._data_dir, config=self.config)
        # 推送闸门：簇冷却 / 每日上限 / 最小间隔 / 对话互斥
        self.guard = CareGuard(data_dir=self._data_dir, config=self.config)
        # 聊天内自然关怀：她说话时把「可以顺口提一句」的要点软注入本轮提示
        self.chat_care = chat_care.ChatCareInjector(
            data_dir=self._data_dir, config=self.config
        )
        # monitor.scan() 的短时缓存（她连发几条消息时避免重复读盘）
        self._chat_scan_cache: tuple[float, list] = (0.0, [])
        self._monitor_task: asyncio.Task | None = None
        # 沈星回身体镜像：按会话存，后台生成不阻塞回复
        self.mirror_store = MirrorStore(self._data_dir)
        self._mirror_task: asyncio.Task | None = None
        self._mirror_data_task: asyncio.Task | None = None
        self._mirror_lock = asyncio.Lock()
        self._mirror_last_tick = 0.0
        self._mirror_last_error = ""
        # 运行时开关（/health on|off 会即时改它，不依赖 config 写回）
        self._monitor_runtime_enabled: bool = bool(
            self.config.get("monitor_enabled", True)
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_record(self, event: AstrMessageEvent):
        if not await self._is_session_enabled(event):
            return
        # 缓存最近一次的真实交互事件，拿到 umo、cqhttp 实例和 bot 身份
        if not event.get_sender_id() or event.get_sender_id() == event.get_self_id():
            return
        # 主动关怀注入的伪消息（昵称 event_bridge）不是「她在说话」：
        # 既不缓存，也不计入对话活跃，否则会把闸门自己锁死。
        if self._is_injected_event(event):
            return
        # 主动关怀只认私聊：群聊消息既不更新推送目标，也不参与对话互斥。
        if not event.is_private_chat():
            return
        self._last_event = event
        self.guard.record_user_message()
        # 身体镜像：任何一句真人对话都算「有对话」，用于判断是否满 2 小时没说话
        if bool(self.config.get("mirror_enabled", True)):
            await self._mirror_touch_chat(event)

    @filter.on_decorating_result(priority=1)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """机器人每次发送时记一笔，并兜底剥掉身体镜像标签。"""
        if not await self._is_session_enabled(event):
            return
        try:
            result = event.get_result()
            for comp in getattr(result, "chain", None) or []:
                if (
                    getattr(comp, "type", "") == "Plain"
                    or type(comp).__name__ == "Plain"
                ) and hasattr(comp, "text"):
                    comp.text = mirror_logic.strip_heart_tags(comp.text)
        except Exception:
            logger.exception("[Xavier_care] 兜底清理身体镜像标签失败（不影响发送）")
        try:
            if event.is_private_chat():
                self.guard.record_bot_reply()
        except Exception:
            logger.exception("[Xavier_care] 记录机器人发送时间失败")

    @filter.on_llm_response(priority=90)
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """主回复里顺手带来的 <xavier_heart> 标签：解析、落盘、删掉标签。

        不再额外调用模型；标签缺失或 JSON 坏掉都只跳过，不影响她正常回复。
        优先级低于 Xavier_Hub 的 100，确保心声先提取，再处理心率标签。
        """
        if not response:
            return

        # 绝对兜底剥离：比 active_function(priority=0) 先跑，无条件剥离 response.completion_text
        raw = response.completion_text or ""
        cleaned = mirror_logic.strip_heart_tags(raw)
        if cleaned != raw:
            response.completion_text = cleaned

        try:
            if not await self._is_session_enabled(event):
                return
            if not bool(self.config.get("mirror_enabled", True)):
                return
            if not event.is_private_chat() or self._is_injected_event(event):
                return
            umo = self._mirror_resolve_umo(event)
            if not umo:
                return
            parsed, _ = mirror_logic.extract_heart_payload(raw)
            if not parsed:
                return
            await self._mirror_apply_response(umo, parsed)
        except Exception:
            logger.exception("[Xavier_care] 解析对话心率失败（不影响回复）")

    def _mirror_resolve_umo(self, event: AstrMessageEvent) -> str:
        """只在固定会话里工作；别的会话一律返回空，整条链路不落盘。"""
        target = str(self.config.get("mirror_target_umo") or "").strip()
        if not target:
            target = DEFAULT_TARGET_UMO
        return target if event.unified_msg_origin == target else ""

    async def _mirror_touch_chat(self, event: AstrMessageEvent) -> None:
        """记下最近一次真人对话时间，供静息补点判断「多久没说话」。"""
        try:
            umo = self._mirror_resolve_umo(event)
            if not umo:
                return
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            async with self._mirror_lock:
                await asyncio.to_thread(
                    self._mirror_touch_sync, umo, date_str, now
                )
        except Exception:
            logger.exception("[Xavier_care] 记录对话时间失败（不影响回复）")

    def _mirror_touch_sync(self, umo: str, date_str: str, now: datetime) -> None:
        state = self.mirror_store.load(umo, date_str)
        if not isinstance(state, dict) or not state:
            state = {
                "version": 1,
                "date": date_str,
                "name": str(self.config.get("mirror_name") or "沈星回"),
                "umo": umo,
                "points": [],
                "current": {},
            }
        state["last_chat_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        self.mirror_store.save(umo, date_str, state)

    async def _mirror_apply_response(self, umo: str, parsed: dict) -> None:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        decay = int(self.config.get("mirror_decay_minutes", 25) or 25)
        gap = int(self.config.get("mirror_emotion_gap_minutes", 5) or 5)
        limit = int(self.config.get("mirror_daily_emotion_points", 20) or 20)
        life = await asyncio.to_thread(
            mirror_life.build_snapshot, self._data_dir, now
        )
        cooldown = int(self.config.get("mirror_chat_cooldown_minutes", 20) or 20)
        async with self._mirror_lock:
            state = mirror_logic.ensure_state(
                self.mirror_store.load(umo, date_str),
                umo,
                date_str,
                str(self.config.get("mirror_name") or "沈星回"),
                life,
            )
            if not mirror_logic.chat_prompt_due(state, now, cooldown):
                return
            state, result = mirror_logic.apply_dialog_update(
                state, parsed, life, now, decay, gap, limit
            )
            if result == "calm":
                self.mirror_store.save(umo, date_str, state)
                self._mirror_last_error = ""
                return
            self.mirror_store.save(umo, date_str, state)
            self._mirror_last_error = ""
            logger.info(
                "[Xavier_care] 对话心率已更新："
                f"{state['current'].get('hr')} bpm / "
                f"{state['current'].get('emotion')} / {result}"
            )

    def _mirror_schedule_data_resonance(self, record: dict) -> None:
        """手机数据上报后，后台跑一次「身体共振」，不阻塞 HTTP。"""
        if not isinstance(record, dict):
            return
        self._pending_mirror_record = dict(record)
        if self._mirror_data_task and not self._mirror_data_task.done():
            return
        self._mirror_data_task = asyncio.create_task(self._mirror_data_worker())

    async def _mirror_data_worker(self) -> None:
        try:
            while self._pending_mirror_record:
                record = self._pending_mirror_record
                self._pending_mirror_record = None
                await self._mirror_data_sync(record)
        except Exception:
            logger.exception("[Xavier_care] 数据共振失败（不影响上报）")
        finally:
            self._mirror_data_task = None

    async def _mirror_data_sync(self, record: dict) -> None:
        if not bool(self.config.get("mirror_enabled", True)):
            return
        if not bool(self.config.get("mirror_data_resonance_enabled", True)):
            return
        # 数据推送只负责共振曲线；主动关怀交给 monitor，默认不在这里说话。
        allow_whisper = bool(
            self.config.get("mirror_data_whisper_enabled", False)
        )
        hr = int(record.get("latest_hr") or 0)
        if hr <= 0:
            points = record.get("hr_timeline") or []
            if isinstance(points, list) and points:
                hr = int((points[-1] or {}).get("hr") or 0)
        if hr <= 0:
            return

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        life = await asyncio.to_thread(mirror_life.build_snapshot, self._data_dir, now)
        umo = self._mirror_default_umo()
        if not umo:
            return

        sleep_min = 0
        sleep = record.get("sleep")
        if isinstance(sleep, dict):
            sleep_min = int(sleep.get("duration_min") or 0)
        facts = [
            f"她最近心率：{hr} bpm",
            f"今天步数：{int(record.get('steps') or 0)}",
            f"今日运动：{int(record.get('exercise_min') or 0)} 分钟",
        ]
        if sleep_min:
            facts.append(f"昨晚睡了：{sleep_min // 60} 小时 {sleep_min % 60} 分")
        if record.get("resting_hr"):
            facts.append(f"静息心率：{record.get('resting_hr')} bpm")
        if record.get("hrv_ms"):
            facts.append(f"HRV：{record.get('hrv_ms')} ms")

        name = str(self.config.get("mirror_name") or "沈星回")
        persona = await self._mirror_persona(umo)
        sep = chr(10)
        system_prompt = sep.join(
            p for p in (
                persona,
                f"你是{name}。现在用第一人称，像平时一样和她说话。",
                "不要提手环、手表、心率数字或健康数据，除非她主动问。",
            ) if p
        )
        prompt = sep.join(
            ["小狗戴着手表，你感应到她的身体状态："]
            + [f"- {f}" for f in facts]
            + [
                f"现在时间：{now.strftime('%H:%M')}",
                "你自己的身体会跟着轻轻共振。判断她此刻在做什么、累不累、需不需要你开口。",
                "只输出 JSON，不要 Markdown：",
                '{"whisper":"想对她说的一句短话，25字内，口语，温柔，不解释数据",'
                '"emotion":"平静|开心|吃醋|亲昵|激动|疲惫|运动",'
                '"reason":"二十字内","should_speak":true或false}',
                "规则：should_speak 只在真的想关心她时填 true，不要每半小时都说话；"
                "她睡着或数据正常时填 false。",
                "whisper 只能报备或表达关心，必须是陈述句，禁止提问、追问和问号；"
                "禁止“问问、感觉怎么样、要不要、是不是、困不困、累不累”。",
            ]
        )

        parsed: dict = {}
        try:
            provider = self.context.get_using_provider(umo)
            if provider:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                    timeout=25,
                )
                parsed = mirror_logic.parse_llm_json(
                    getattr(resp, "completion_text", "") or ""
                )
        except Exception:
            logger.exception("[Xavier_care] 数据共振生成失败，仅更新心率点")
            parsed = {}

        whisper = mirror_logic.sanitize_whisper(parsed.get("whisper"))
        if parsed.get("should_speak") is False:
            whisper = ""
        emotion = str(parsed.get("emotion") or "运动").strip()
        reason = str(parsed.get("reason") or "感应到你在动").strip()

        debounce = int(self.config.get("mirror_data_debounce_minutes", 15) or 15)
        cool = int(self.config.get("mirror_data_whisper_cooldown_minutes", 40) or 40)
        async with self._mirror_lock:
            state = mirror_logic.ensure_state(
                self.mirror_store.load(umo, date_str),
                umo,
                date_str,
                name,
                life,
            )
            state, result = mirror_logic.apply_data_update(
                state,
                life,
                now,
                hr,
                whisper=whisper,
                emotion=emotion,
                reason=reason,
                debounce_minutes=debounce,
                whisper_cooldown_minutes=cool,
                allow_whisper=allow_whisper,
            )
            self.mirror_store.save(umo, date_str, state)
            self._mirror_last_error = ""
        logger.info(
            f"[Xavier_care] 数据共振：{state['current'].get('hr')} bpm / {result}"
        )
        if result == "point_whisper" and whisper:
            ok = await self._send_direct(umo, whisper)
            if not ok:
                logger.warning("[Xavier_care] 数据共振私语发送失败")

    def _mirror_default_umo(self) -> str:
        """主动推送的目标：固定会话优先，没配就用默认 QQ 会话。"""
        target = str(self.config.get("mirror_target_umo") or "").strip()
        return target or DEFAULT_TARGET_UMO

    async def _mirror_persona(self, umo: str) -> str:
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return ""
            persona = await pm.get_default_persona_v3(umo=umo)
            if not persona:
                persona = await pm.get_default_persona_v3()
            if isinstance(persona, dict):
                return str(persona.get("prompt") or "").strip()
            return str(getattr(persona, "system_prompt", "") or "").strip()
        except Exception:
            logger.exception("[Xavier_care] 读取人格失败，使用内置口吻")
            return ""

    async def _mirror_tick(self) -> None:
        """静息补点：只补当前时刻，不回头伪造早上的点。"""
        if not bool(self.config.get("mirror_enabled", True)):
            return
        hours = int(self.config.get("mirror_auto_rest_hours", 2) or 0)
        if hours <= 0:
            return
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        decay = int(self.config.get("mirror_decay_minutes", 25) or 25)
        guard = int(self.config.get("mirror_auto_rest_recent_guard_minutes", 30) or 0)
        life = await asyncio.to_thread(mirror_life.build_snapshot, self._data_dir, now)
        for state in self.mirror_store.all_today(date_str):
            umo = str(state.get("umo") or "")
            if not umo:
                continue
            if not mirror_logic.auto_rest_due(state, now, hours, guard):
                continue
            async with self._mirror_lock:
                state = mirror_logic.ensure_state(
                    state,
                    umo,
                    date_str,
                    str(self.config.get("mirror_name") or "沈星回"),
                    life,
                )
                mirror_logic.append_auto_rest(state, life, now, decay)
                self.mirror_store.save(umo, date_str, state)
            logger.info(
                f"[Xavier_care] 身体镜像静息补点：{state['current'].get('hr')} bpm"
            )

    async def _mirror_loop(self) -> None:
        try:
            await asyncio.sleep(20)
            while True:
                try:
                    await self._mirror_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[Xavier_care] 身体镜像静息补点失败")
                await asyncio.sleep(600)
        except asyncio.CancelledError:
            pass

    async def _mirror_api_snapshot(self) -> dict:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        decay = int(self.config.get("mirror_decay_minutes", 25) or 25)
        life = await asyncio.to_thread(mirror_life.build_snapshot, self._data_dir, now)

        if time.time() - self._mirror_last_tick >= 60:
            self._mirror_last_tick = time.time()
            try:
                await self._mirror_tick()
            except Exception:
                logger.exception("[Xavier_care] 打开页面时补静息点失败")

        target = self._mirror_default_umo()
        if target:
            state = self.mirror_store.load(target, date_str)
        else:
            state = self.mirror_store.latest_today(date_str)

        fallback = self._mirror_last_error or "正在同步"
        if not state:
            try:
                if not self.context.get_using_provider(target or None):
                    fallback = "未配置生成模型"
            except Exception:
                pass
        return mirror_logic.public_snapshot(state, life, now, decay, fallback)

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """聊天内自然关怀：把「今天可以顺口提一句」的要点软注入本轮系统提示。

        与主动推送的区别（也是它存在的理由）：
        - 不新增任何消息，所以她说话时「顺口关心」不会和她的对话撞车；
        - 不占每日额度、不计冷却、不参与「对话互斥」，因此不会把主动推送
          的额度提前吃掉；
        - 提示词里明确写了「可以完全不提」，是否开口交给角色自己判断。
        """
        if not await self._is_session_enabled(event):
            return
        if not event.is_private_chat():
            return
        if not self._mirror_resolve_umo(event):
            # 只在固定会话里关心，别的会话一句话都不插。
            return
        if self._is_injected_event(event):
            # 主动推送自己注入的伪消息：那条提示词里已经带好关怀内容，
            # 再叠一层会变成「对着自己的指令回复」。
            return
        if self.chat_care.enabled:
            try:
                text, used = self.chat_care.render(
                    alerts=self._chat_alerts(),
                    period=self._period_snapshot(),
                )
                if text:
                    req.system_prompt = (req.system_prompt or "") + "\n\n" + text
                    logger.info(
                        "[Xavier_care] 已注入聊天内关怀："
                        f"{('要点 ' + ', '.join(used)) if used else '仅经期语境'}"
                    )
            except Exception:
                logger.exception("[Xavier_care] 注入聊天内关怀失败（不影响本轮回复）")
        try:
            await self._inject_mirror_prompt(event, req)
        except Exception:
            logger.exception("[Xavier_care] 注入身体镜像规则失败（不影响本轮回复）")

    async def _inject_mirror_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把「回复末尾顺手报心率」的隐藏规则塞进本轮系统提示。"""
        if not bool(self.config.get("mirror_enabled", True)):
            return
        if not event.is_private_chat() or self._is_injected_event(event):
            return
        umo = self._mirror_resolve_umo(event)
        if not umo:
            return
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        decay = int(self.config.get("mirror_decay_minutes", 25) or 25)
        life = await asyncio.to_thread(
            mirror_life.build_snapshot, self._data_dir, now
        )
        state = mirror_logic.ensure_state(
            self.mirror_store.load(umo, date_str),
            umo,
            date_str,
            str(self.config.get("mirror_name") or "沈星回"),
            life,
        )
        cooldown = int(self.config.get("mirror_chat_cooldown_minutes", 20) or 20)
        if not mirror_logic.chat_prompt_due(state, now, cooldown):
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + mirror_logic.build_inline_prompt(
            str(self.config.get("mirror_name") or "沈星回"),
            state,
            life,
            now,
            decay,
        )

    async def _is_session_enabled(self, event: AstrMessageEvent) -> bool:
        """会话级插件被禁用时，钩子必须完全不执行。

        AstrBot 自身也会过滤，但这里再判一次：钩子里要写状态文件、要注入
        提示词，宁可靠自己确认一遍，也别在被禁用的会话里留下痕迹。
        """
        try:
            from astrbot.core.star.session_plugin_manager import SessionPluginManager

            return await SessionPluginManager.is_plugin_enabled_for_session(
                session_id=event.get_session_id(),
                plugin_name=getattr(self, "name", "") or PLUGIN_DIR_NAME,
            )
        except Exception:
            # 判断不出来按启用处理：宁可多关心一句，也不要静默失效
            return True

    def _chat_alerts(self) -> list:
        """给聊天注入用的异常列表，带 60 秒缓存。

        monitor.scan() 要读今天的数据文件加近几天基线；她连发几条消息就会
        连着触发几次 LLM 请求，缓存一下省掉重复 IO（也避免日志刷屏）。
        """
        now = time.time()
        ts, cached = self._chat_scan_cache
        if cached and (now - ts) < 60:
            return cached
        try:
            alerts = self.monitor.scan()
        except Exception:
            logger.exception("[Xavier_care] 聊天内关怀扫描异常，本轮按无异常处理")
            alerts = []
        self._chat_scan_cache = (now, alerts)
        return alerts

    def _period_snapshot(self) -> dict:
        """经期状态快照，供语境行使用。失败就返回空字典（跳过语境行）。"""
        try:
            return health_logic.compute_period_status(self._data_dir)
        except Exception:
            logger.exception("[Xavier_care] 推算经期状态失败（跳过语境行）")
            return {}

    def _skip_because_talked_today(self, alert_type: str) -> bool:
        """反向去重：聊天里已经顺口提过的要点，当天不再单独推一条。

        「提过」用「已注入」近似——我们没法判断模型有没有真的说出口，
        所以按提过处理，宁少不重复。开关 chat_hint_dedup_push（默认开）。
        """
        try:
            if not bool(self.config.get("chat_hint_dedup_push", True)):
                return False
        except Exception:
            return False
        try:
            if alert_type in self.chat_care.mentioned_types_today():
                logger.info(
                    f"[Xavier_care] 跳过 {alert_type}：今天聊天里已经顺口提过"
                )
                return True
        except Exception:
            logger.exception("[Xavier_care] 查聊天内关怀记录失败，本次照常推送")
        return False

    @staticmethod
    def _is_injected_event(event: AstrMessageEvent) -> bool:
        """这条事件是不是主动关怀自己注入的。"""
        try:
            return (event.get_sender_name() or "") == INJECT_SENDER_NAME
        except Exception:
            return False

    @staticmethod
    def _is_private_umo(umo: str) -> bool:
        """判断 unified_msg_origin 是不是私聊。

        群聊的 message_type 里一定带 group；私聊常见 private / FriendMessage。
        判断不出来时按「非私聊」处理，宁可少推一条，也不把健康关怀发进群里。
        """
        try:
            parts = str(umo or "").split(":")
            if len(parts) < 2:
                return False
            msg_type = parts[1].strip().lower()
            if "group" in msg_type:
                return False
            return any(k in msg_type for k in ("private", "friend", "dm", "user"))
        except Exception:
            return False

    def _resolve_push_umo(self) -> str:
        """主动关怀的私聊目标。配置了非私聊目标也不会发。"""
        configured = str(self.config.get("push_target_umo") or "").strip()
        if configured:
            if self._is_private_umo(configured):
                return configured
            logger.warning(
                "[Xavier_care] push_target_umo 不是私聊会话，已忽略；"
                "主动关怀只会发到私聊"
            )
        # 没单独配推送目标就跟着镜像走，避免关怀漏到别的会话。
        mirror_target = self._mirror_default_umo()
        if mirror_target and self._is_private_umo(mirror_target):
            return mirror_target
        return ""

    def _migrate_legacy_data_dir(self) -> None:
        """插件更名后的一次性迁移：把旧目录里的数据复制过来（只复制，不删除旧目录）。"""
        legacy = Path(get_astrbot_data_path()) / "plugin_data" / LEGACY_DIR_NAME
        if not legacy.is_dir() or legacy == self._data_dir:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for item in legacy.iterdir():
                if not item.is_file():
                    continue
                target = self._data_dir / item.name
                try:
                    # 目标不存在，或旧目录里的文件更新（重启前手机又上报过），都需要复制
                    if target.exists() and target.stat().st_mtime >= item.stat().st_mtime:
                        continue
                except OSError:
                    continue
                shutil.copy2(item, target)
                copied += 1
            if copied:
                logger.info(f"[Xavier_care] 已从旧目录 {LEGACY_DIR_NAME} 迁移 {copied} 个数据文件")
        except OSError:
            logger.exception("[Xavier_care] 迁移旧数据目录失败（不影响新数据写入）")

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        """插件激活时调用：确保密钥、建目录、清过期、起监听、起 monitor。"""
        self._ensure_auth_token()
        self._migrate_legacy_data_dir()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("[Xavier_care] 创建数据目录失败")
        self._run_cleanup()
        try:
            self.mirror_store.prune()
        except Exception:
            logger.exception("[Xavier_care] 清理身体镜像旧数据失败")
        await self._start_server()

        # 启动主动关怀轮询（用运行时开关判断，/health on 也能补起）
        if self._monitor_runtime_enabled:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info(
                f"[Xavier_care] 主动关怀已启动，间隔 "
                f"{int(self.config.get('check_interval', 900))}s"
            )

        if bool(self.config.get("mirror_enabled", True)):
            self._mirror_task = asyncio.create_task(self._mirror_loop())
            logger.info("[Xavier_care] 身体镜像已启动（每 10 分钟检查，满 2 小时补静息点）")

    async def terminate(self) -> None:
        """插件停用/重载时调用：关掉监听、取消 monitor，释放端口。"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[Xavier_care] 停止 monitor 时出错")
            self._monitor_task = None
        if self._mirror_task:
            self._mirror_task.cancel()
            try:
                await self._mirror_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[Xavier_care] 停止身体镜像定时器时出错")
            self._mirror_task = None
        await self._stop_server()

    def _ensure_auth_token(self) -> None:
        """没配密钥时，自动生成一串本机专属的随机密钥并写回配置。"""
        if self.config.get("auth_token"):
            return
        new_token = secrets.token_urlsafe(24)
        self.config["auth_token"] = new_token
        try:
            self.config.save_config()
            logger.warning(
                "[Xavier_care] 未配置 auth_token，已自动生成一串专属密钥并写入配置。"
                "在私聊发送 /health setup 即可看到上报地址和密钥。"
            )
        except Exception:
            self.config["auth_token"] = ""
            logger.warning(
                "[Xavier_care] 自动生成密钥后写入配置失败，已放弃以避免密钥落日志。"
                "接收端在配置前会拒收一切；请在 WebUI 手动填写 auth_token 后重载插件。"
            )

    # -- 模块一：接收端 ---------------------------------------------------

    async def _start_server(self) -> None:
        """启动 aiohttp 监听。失败只记日志、不抛异常，避免连累其他插件。

        端口被占用时自动往后试两个（如 8787→8788→8789），
        并把最终端口记在 self._listen_port，供 /health setup 展示。
        """
        await self._stop_server()

        try:
            base_port = int(self.config.get("listen_port", 8787))
        except (TypeError, ValueError):
            base_port = 8787
        if base_port < 1 or base_port > 65535:
            base_port = 8787

        last_error = ""
        for offset in range(3):
            port = base_port + offset
            if port > 65535:
                break
            runner = None
            try:
                app = web.Application(client_max_size=MAX_BODY_BYTES)
                app.router.add_post(ENDPOINT_PATH, self._handle_report)
                app.router.add_get("/health/dashboard", self._handle_dashboard)
                app.router.add_get("/health/api/data", self._handle_api_data)
                app.router.add_get("/health/api/history", self._handle_api_history)
                app.router.add_get("/health/api/period/month", self._handle_api_period_month)
                app.router.add_post("/health/api/period/mark", self._handle_api_period_mark)
                app.router.add_post("/health/api/period/unmark", self._handle_api_period_unmark)
                app.router.add_get("/health/api/milktea/month", self._handle_api_milktea_month)
                app.router.add_get("/health/api/milktea/day", self._handle_api_milktea_day)
                app.router.add_post("/health/api/milktea/add", self._handle_api_milktea_add)
                app.router.add_post("/health/api/milktea/delete", self._handle_api_milktea_delete)
                app.router.add_get("/health/assets/{name}", self._handle_asset)

                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host="0.0.0.0", port=port)
                await site.start()

                self._app, self._runner, self._site = app, runner, site
                self._listen_port = port
                self._listen_error = ""
                logger.info(
                    f"[Xavier_care] 接收端已启动：监听 0.0.0.0:{port}{ENDPOINT_PATH}（仅 POST）"
                )
                if port != base_port:
                    logger.warning(
                        f"[Xavier_care] 配置端口 {base_port} 被占用，已自动改用 {port}；"
                        "手机端请用 /health setup 里的新地址"
                    )
                if not self.config.get("auth_token"):
                    logger.warning(
                        "[Xavier_care] auth_token 尚未配置，配置前所有上报都会被拒绝（401）。"
                        "请在私聊发送 /health setup 获取密钥。"
                    )
                return
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    f"[Xavier_care] 端口 {port} 启动失败（可能被占用）：{exc}"
                )
                if runner is not None:
                    try:
                        await runner.cleanup()
                    except Exception:
                        logger.exception("[Xavier_care] 回收启动失败的监听器时出错")

        self._listen_error = last_error or "端口不可用"
        logger.error(
            f"[Xavier_care] 接收端启动失败：{base_port} / {base_port + 1} / "
            f"{base_port + 2} 都不可用。读取工具仍可用，但暂时收不到新数据。"
        )

    async def _stop_server(self) -> None:
        site, runner = self._site, self._runner
        self._site = self._runner = self._app = None
        self._listen_port = 0
        try:
            if site is not None:
                await site.stop()
        except Exception:
            logger.exception("[Xavier_care] 停止监听站点时出错")
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception:
            logger.exception("[Xavier_care] 清理监听器时出错")

    async def _handle_report(self, request: web.Request) -> web.Response:
        """处理手机的一次上报。任何异常都兜住，不让插件崩。"""
        try:
            token = self.config.get("auth_token", "") or ""
            provided = request.headers.get(AUTH_HEADER, "")

            if not token:
                logger.warning("[Xavier_care] 收到上报但 auth_token 未配置，已拒绝。")
                return web.json_response(
                    {"ok": False, "error": "server not configured"}, status=401
                )
            if not provided or not hmac.compare_digest(provided, token):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

            raw = await request.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return web.json_response({"ok": False, "error": "bad json"}, status=400)

            try:
                records = health_logic.normalize_payload(data)
            except health_logic.InvalidPayloadError as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)

            saved_days: list[str] = []
            for rec in records:
                try:
                    saved_days.append(health_logic.store_report(self._data_dir, rec).stem)
                except health_logic.InvalidPayloadError as e:
                    logger.warning(f"[Xavier_care] 丢弃一条非法记录: {e}")
                    continue
            if not saved_days:
                return web.json_response({"ok": False, "error": "no usable data"}, status=400)

            self._run_cleanup()
            # 数据共振：只取本次最新一天，后台跑，不阻塞手机上报。
            self._mirror_schedule_data_resonance(records[-1])
            logger.info(
                f"[Xavier_care] 已接收并存储 {len(saved_days)} 天数据：{', '.join(saved_days)}"
            )
            return web.json_response({"ok": True, "saved": saved_days})

        except web.HTTPException:
            raise
        except Exception:
            logger.exception("[Xavier_care] 处理上报时发生未预期错误")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _send_direct(self, umo: str, text: str) -> bool:
        """直接以 Bot 身份把文案发到指定会话（兜底路径）。"""
        try:
            from astrbot.api.event import MessageChain
            chain = MessageChain().message(text)
            ok = await self.context.send_message(umo, chain)
            return bool(ok)
        except Exception:
            logger.exception(f"[Xavier_care] 直发兜底失败 umo={umo}")
            return False

    async def _trigger_event_wakeup(self, prompt: str) -> bool:
        """monitor 主动关怀用：构造一条伪造消息注入 AstrBot 处理流程。"""
        umo = self._resolve_push_umo() or None
        bot_self_id = None
        cq_bot = None

        if self._last_event:
            bot_self_id = self._last_event.get_self_id()
            if hasattr(self._last_event, "bot"):
                cq_bot = self._last_event.bot

        if not umo or not cq_bot or not bot_self_id:
            for star_wrapper in getattr(self.context, "_stars", []) or getattr(self.context, "stars", []) or []:
                s = getattr(star_wrapper, "star_instance", star_wrapper)
                p_name = getattr(s, "plugin_name", "") or getattr(star_wrapper, "name", "")
                if "wakeup" in str(p_name):
                    if not umo:
                        umo = getattr(s, "target_umo", None)
                    if not bot_self_id:
                        bot_self_id = getattr(s, "_bot_qq_id", None)
                    if not cq_bot:
                        cq_bot = getattr(s, "_cqhttp_bot", None)
                    break

        if cq_bot is None:
            cq_bot = getattr(self.context, "_cqhttp_bot", None)
        if cq_bot is None:
            try:
                for mgr_name in (
                    "platform_manager",
                    "_platform_manager",
                    "platform_mgr",
                    "_platform_mgr",
                ):
                    mgr = getattr(self.context, mgr_name, None)
                    if not mgr:
                        continue
                    for list_name in (
                        "platforms",
                        "platform_insts",
                        "_platforms",
                        "adapters",
                        "_adapters",
                    ):
                        plist = getattr(mgr, list_name, None)
                        if not plist or not hasattr(plist, "__iter__"):
                            continue
                        for p in plist:
                            bot = getattr(p, "bot", None)
                            if bot and hasattr(bot, "send_private_msg"):
                                cq_bot = bot
                                break
                        if cq_bot:
                            break
                    if cq_bot:
                        break
            except Exception as e:
                logger.debug(f"[Xavier_care] 搜索 bot 实例失败: {e}")

        if not bot_self_id and cq_bot and hasattr(cq_bot, "get_login_info"):
            try:
                info = await cq_bot.get_login_info()
                bot_self_id = str(info.get("user_id", ""))
            except Exception:
                pass

        if not umo:
            logger.info("[Xavier_care] 尚未记录到最近私聊交互，跳过即时唤醒")
            return False
        if not self._is_private_umo(umo):
            logger.warning("[Xavier_care] 主动关怀目标不是私聊，已跳过（不会发到群里）")
            return False

        try:
            from aiocqhttp import Event as CQEvent
        except ImportError:
            logger.warning("[Xavier_care] 未找到 aiocqhttp，跳过伪造注入")
            return False

        parts = umo.rsplit(":", 2)
        if len(parts) < 3:
            return False
        session_id = parts[2]
        msg_type_str = parts[1]
        is_group = "Group" in msg_type_str

        if is_group:
            if "_" in session_id:
                uid, gid = session_id.rsplit("_", 1)
            else:
                return False
            payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": int(time.time()) % 2147483647,
                "group_id": int(gid),
                "user_id": int(uid),
                "message": [{"type": "text", "data": {"text": prompt}}],
                "raw_message": prompt,
                "font": 0,
                "sender": {"user_id": int(uid), "nickname": "event_bridge", "card": ""},
                "time": int(time.time()),
                "self_id": int(bot_self_id) if bot_self_id else 0,
            }
        else:
            payload = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": int(time.time()) % 2147483647,
                "user_id": int(session_id),
                "message": [{"type": "text", "data": {"text": prompt}}],
                "raw_message": prompt,
                "font": 0,
                "sender": {"user_id": int(session_id), "nickname": "event_bridge", "sex": "unknown", "age": 0},
                "time": int(time.time()),
                "self_id": int(bot_self_id) if bot_self_id else 0,
            }

        fake_event = CQEvent.from_payload(payload)
        if not fake_event:
            return False

        if cq_bot:
            handler = getattr(cq_bot, "_handle_event", None) or getattr(cq_bot, "handle_event", None)
            if handler:
                await handler(fake_event)
                logger.info(f"[Xavier_care] 🎯 已成功注入消息 | umo={umo}")
                return True
            else:
                logger.warning("[Xavier_care] cq_bot 没有可用 handle_event 方法")
                return False
        else:
            logger.warning("[Xavier_care] 未获取到 cq_bot 实例，无法注入")
            return False

    # -- 模块三：主动关怀 monitor ------------------------------------------

    def _log_care(self, alert: dict, umo: str, ok: bool, via: str = "") -> None:
        """把一次关怀落盘，便于事后查看。失败不影响主流程。"""
        if not self.config.get("care_log_enabled", True):
            return
        try:
            log_path = self._data_dir / "care_log.jsonl"
            line = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": alert.get("type"),
                "hint": alert.get("hint"),
                "umo": umo,
                "ok": ok,
                "via": via,
            }
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[Xavier_care] 写关怀日志失败（不影响推送）")

    async def _monitor_loop(self) -> None:
        """后台轮询：每隔 check_interval 秒扫一次健康数据。"""
        interval = int(self.config.get("check_interval", 900))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._monitor_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[Xavier_care] monitor tick 出错")

    def _in_quiet_hours(self) -> bool:
        """静默时段判断。支持跨天（如 23 → 7）。"""
        now_h = datetime.now().hour
        try:
            start = int(
                self.config.get("quiet_hours_start", self.config.get("quiet_start", 23))
            )
            end = int(
                self.config.get("quiet_hours_end", self.config.get("quiet_end", 7))
            )
        except (TypeError, ValueError):
            return False
        if start == end:
            return False
        if start < end:
            return start <= now_h < end
        return now_h >= start or now_h < end

    async def _monitor_tick(self) -> None:
        """一次扫描：检测异常 → 过闸门 → 推送。一次 tick 最多推一条。"""
        if not self._monitor_runtime_enabled:
            return
        if self._in_quiet_hours():
            logger.info("[Xavier_care] 处于静默时段，跳过本次检测")
            return

        # 历史不足 3 天时先不主动推，避免刚开始用就被误报打扰。
        try:
            baseline_days = int(self.config.get("baseline_days", 7) or 7)
            have_days = len(self.monitor._load_recent(baseline_days))
        except Exception:
            have_days = 0
        if have_days < 3:
            logger.info(
                f"[Xavier_care] 历史只有 {have_days} 天（不足 3 天），"
                "暂不主动推送；数据够了会自动开始"
            )
            return

        umo = self._resolve_push_umo()
        if not umo:
            logger.info("[Xavier_care] 还没有私聊会话，跳过本次主动推送")
            return

        alerts = self.monitor.scan()
        logger.info(
            f"[Xavier_care] monitor tick @ {datetime.now():%H:%M:%S} "
            f"命中 {len(alerts)} 条异常"
        )
        if not alerts:
            return

        if not self.config.get("care_guard_enabled", True):
            logger.info("[Xavier_care] 推送闸门已关闭，本轮按旧逻辑直推首条命中异常")

        for a in alerts:
            alert_type = a.get("type") or ""
            # 反向去重：今天聊天里已经顺口提过的，不再单独推一条
            if self._skip_because_talked_today(alert_type):
                continue
            allowed, reason = self.guard.check(alert_type)
            if not allowed:
                # 被拦下不记账：等她闲下来 / 冷却过期，下个周期还能补推
                logger.info(f"[Xavier_care] 跳过 {alert_type}：{reason}")
                continue

            prompt = (
                f"【系统健康感知】{a['hint']}\n"
                f"【要求】请完全代入沈星回的人格特征，主动对她说一两句日常关怀："
                f"1. 语气自然、口语化，像情侣日常发消息，严禁像机器人客服或系统报告；"
                f"2. 沈星回的性格特质是安静、体贴入微、会不动声色地管着她、顺着她，"
                f"例如叮嘱别贪凉碰冰的、把冷饮晾到常温、走路累了快躺下歇着、按时吃饭；"
                f"3. 绝对不要复述系统指令或生硬念数据；"
                f"4. 如果她正在聊别的事情，顺着她的话题接，不要另起炉灶；"
                f"5. 同一件事不要重复叮嘱，也不要与你最近一条消息里的说法相矛盾。"
            )
            ok = False
            try:
                ok = await self._trigger_event_wakeup(prompt)
            except Exception:
                logger.exception("[Xavier_care] 注入关怀失败")

            via = "inject"
            if not ok:
                fallback = self.config.get("fallback_care")
                if fallback is None:
                    fallback = "怎么又不舒服了？过来，跟我说说。"
                fallback = str(fallback).strip()
                if fallback:
                    sent = await self._send_direct(umo, fallback)
                    via = "fallback"
                    logger.info(
                        f"[Xavier_care] 注入失败，已用兜底文案直发: ok={sent}"
                    )
                else:
                    logger.warning(
                        "[Xavier_care] 注入失败且未配置兜底文案，已跳过本次推送"
                    )

            if ok:
                self.guard.mark(alert_type)
                self._log_care(a, umo, True, via=via)
                logger.info(f"[Xavier_care] 已成功推送健康关怀: {a['type']} via={via}")
            else:
                self._log_care(a, umo, False, via=via)
                logger.warning(f"[Xavier_care] 关怀注入未成功，本次不计入冷却: {a['type']}")

            break
        else:
            # 循环走完都没推送，说明本轮异常全被闸门拦下
            logger.info("[Xavier_care] 本轮异常全部被闸门拦下，暂不推送")

    def _run_cleanup(self) -> None:
        try:
            retention = int(self.config.get("retention_days", 180))
        except (TypeError, ValueError):
            retention = 180
        try:
            removed = health_logic.cleanup_old(self._data_dir, retention)
            if removed:
                logger.info(f"[Xavier_care] 已清理 {len(removed)} 天过期数据")
        except Exception:
            logger.exception("[Xavier_care] 清理过期数据时出错（不影响其他功能）")

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """返回 HTML 实时健康看板页面。"""
        try:
            html_path = Path(__file__).parent / "index.html"
            if not html_path.exists():
                return web.Response(text="Dashboard template not found", status=404)
            content = html_path.read_text(encoding="utf-8")
            return web.Response(
                text=content,
                content_type="text/html",
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        except Exception:
            logger.exception("[Xavier_care] 加载 dashboard 失败")
            return web.Response(text="Internal Error", status=500)

    async def _handle_api_data(self, request: web.Request) -> web.Response:
        """返回最新的健康数据 JSON。额外带上 period_status。"""
        try:
            data = health_logic.load_latest(self._data_dir) or {}
            try:
                data["period_status"] = health_logic.compute_period_status(self._data_dir)
            except Exception:
                logger.exception("[Xavier_care] 推算经期状态失败（不影响其余数据）")
            try:
                data["mirror"] = await self._mirror_api_snapshot()
            except Exception:
                logger.exception("[Xavier_care] 读取身体镜像失败（不影响其余数据）")
                data["mirror"] = {"available": False, "status": "读取失败", "points": []}
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return web.json_response(
                {"ok": True, "data": data, "server_time": now_str},
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        except Exception:
            logger.exception("[Xavier_care] API 获取数据失败")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _handle_api_history(self, request: web.Request) -> web.Response:
        """返回历史序列（当前只取体重）。"""
        try:
            dates = health_logic.list_dates(self._data_dir)
            points: list[dict] = []
            for d in dates:
                rec = health_logic.load_by_date(self._data_dir, d)
                if not rec:
                    continue
                w = rec.get("weight_kg")
                if w is None:
                    continue
                try:
                    points.append({"date": d, "weight": round(float(w), 1)})
                except (TypeError, ValueError):
                    continue
            return web.json_response({"ok": True, "weight": points})
        except Exception:
            logger.exception("[Xavier_care] API 历史数据失败")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _handle_api_period_month(self, request: web.Request) -> web.Response:
        """返回某个月的经期标记情况。"""
        try:
            month = request.query.get("month") or ""
            if not re.match(r"^\d{4}-\d{2}$", month):
                dates = health_logic.list_dates(self._data_dir)
                if dates:
                    month = dates[-1][:7]
                else:
                    month = datetime.now().strftime("%Y-%m")

            days: dict[str, bool] = {}
            prefix = month + "-"
            for d in health_logic.list_dates(self._data_dir):
                if not d.startswith(prefix):
                    continue
                rec = health_logic.load_by_date(self._data_dir, d)
                if rec and isinstance(rec.get("in_period"), bool):
                    days[d] = rec["in_period"]

            return web.json_response({
                "ok": True,
                "month": month,
                "days": days,
                "period_status": health_logic.compute_period_status(self._data_dir),
            })
        except Exception:
            logger.exception("[Xavier_care] API 经期月视图失败")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _handle_api_period_mark(self, request: web.Request) -> web.Response:
        """标记某天为经期。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        date_str = str(body.get("date", "")).strip()
        if not health_logic.is_valid_date(date_str):
            return web.json_response({"ok": False, "error": "bad date"}, status=400)

        flow = str(body.get("flow") or "中等").strip() or "中等"

        data_dir = self._data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / f"{date_str}.json"
        merged = {}
        if target.is_file():
            try:
                merged = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                merged = {}
        merged["date"] = date_str
        merged["in_period"] = True
        merged["period_flow"] = flow

        try:
            text = json.dumps(merged, ensure_ascii=False, indent=2)
            tmp = data_dir / f".{date_str}.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            logger.exception("[Xavier_care] 写经期标记失败")
            return web.json_response({"ok": False, "error": "write failed"}, status=500)

        return web.json_response({"ok": True, "date": date_str, "in_period": True})

    async def _handle_api_period_unmark(self, request: web.Request) -> web.Response:
        """取消某天的经期标记。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        date_str = str(body.get("date", "")).strip()
        if not health_logic.is_valid_date(date_str):
            return web.json_response({"ok": False, "error": "bad date"}, status=400)

        target = self._data_dir / f"{date_str}.json"
        if not target.is_file():
            return web.json_response({"ok": True, "date": date_str, "removed": False})

        try:
            merged = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            merged = {}

        merged.pop("in_period", None)
        merged.pop("period_flow", None)

        try:
            text = json.dumps(merged, ensure_ascii=False, indent=2)
            tmp = self._data_dir / f".{date_str}.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            logger.exception("[Xavier_care] 取消经期标记失败")
            return web.json_response({"ok": False, "error": "write failed"}, status=500)

        return web.json_response({"ok": True, "date": date_str, "removed": True})

    # -- 奶茶记录 ----------------------------------------------------------

    async def _handle_api_milktea_month(self, request: web.Request) -> web.Response:
        """某月每天喝了几杯（只返回有记录的日期）。"""
        ym = (request.query.get("ym") or datetime.now().strftime("%Y-%m")).strip()
        try:
            summary = milktea.month_summary(self._data_dir, ym)
        except Exception:
            logger.exception("[Xavier_care] 读取奶茶月度数据失败")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)
        return web.json_response({"ok": True, "ym": ym, **summary})

    async def _handle_api_milktea_day(self, request: web.Request) -> web.Response:
        """某天的奶茶明细。"""
        date_str = (request.query.get("date") or "").strip()
        if not health_logic.is_valid_date(date_str):
            return web.json_response({"ok": False, "error": "bad date"}, status=400)
        items = milktea.load_day(self._data_dir, date_str)
        return web.json_response({"ok": True, "date": date_str, "items": items})

    async def _handle_api_milktea_add(self, request: web.Request) -> web.Response:
        """页面补记一条奶茶。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        date_str = str(body.get("date") or "").strip()
        if not health_logic.is_valid_date(date_str):
            return web.json_response({"ok": False, "error": "bad date"}, status=400)
        if milktea.is_future(date_str):
            return web.json_response({"ok": False, "error": "future date"}, status=400)

        entry = milktea.add_entry(self._data_dir, date_str, body, source=milktea.SOURCE_MANUAL)
        if entry is None:
            return web.json_response({"ok": False, "error": "rejected"}, status=400)
        logger.info(
            f"[Xavier_care] 补记奶茶 {date_str}: {entry.get('brand')}{entry.get('name')}"
        )
        return web.json_response({"ok": True, "date": date_str, "item": entry})

    async def _handle_api_milktea_delete(self, request: web.Request) -> web.Response:
        """删掉某天的一条奶茶记录。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        date_str = str(body.get("date") or "").strip()
        if not health_logic.is_valid_date(date_str):
            return web.json_response({"ok": False, "error": "bad date"}, status=400)
        if not milktea.delete_entry(self._data_dir, date_str, body.get("id")):
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"ok": True, "date": date_str})

    async def _handle_asset(self, request: web.Request) -> web.Response:
        """返回插件 assets/ 目录下的静态图片。防路径穿越。"""
        from urllib.parse import unquote
        name = unquote(request.match_info.get("name", ""))
        if not name or "/" in name or "\\" in name or ".." in name:
            return web.Response(status=404)
        path = Path(__file__).parent / "assets" / name
        if not path.is_file():
            return web.Response(status=404)
        return web.FileResponse(
            path, headers={"Cache-Control": "no-cache, must-revalidate"}
        )

    # -- 模块二：读取工具 --------------------------------------------------

    @filter.llm_tool(name="check_health")
    async def check_health(self, event: AstrMessageEvent, date: str = "") -> str:
        """读取用户最近的身体数据：睡眠、心率、活动、经期、症状等。
        返回内容里会标明数据是什么时候收到的，请据此判断数据新鲜度，
        不要把过期数据当成当前状态来讲。

        Args:
            date(string): 可选，指定日期 YYYY-MM-DD；留空返回最近一天。
        """
        try:
            if isinstance(date, str) and date.strip():
                return health_logic.format_for_date(self._data_dir, date.strip())
            return health_logic.format_latest(self._data_dir)
        except Exception:
            logger.exception("[Xavier_care] 读取身体数据时出错")
            return "读取身体数据时出错了。"

    # -- 指令入口 ----------------------------------------------------------

    # -- 奶茶工具 ----------------------------------------------------------

    @filter.llm_tool(name="record_milktea")
    async def record_milktea(
        self,
        event: AstrMessageEvent,
        brand: str = "",
        name: str = "",
        sugar: str = "",
        ice: str = "",
        toppings: str = "",
        rating: int = 0,
        note: str = "",
        date: str = "",
    ) -> str:
        """记下她今天喝了什么奶茶。她自己说喝了奶茶、晒奶茶、或提到新尝试的店时调用。

        她没提到的字段一律留空，不要替她编——尤其是评分，她没打分就传 0。
        note 要照抄她的原话，不要改写她的语气。

        Args:
            brand(string): 品牌或店名，如「霸王茶姬」。
            name(string): 饮品名，如「伯牙绝弦」。
            sugar(string): 甜度原话，如「三分糖」「无糖」；不知道留空。
            ice(string): 冰量，如「少冰」「去冰」；不知道留空。
            toppings(string): 加料，多种用「、」隔开，如「珍珠、奶盖」。
            rating(int): 她给这杯的评分 1-5；她没打分就传 0。
            note(string): 她的点评原话。
            date(string): 日期 YYYY-MM-DD，默认今天。
        """
        try:
            if not brand and not name:
                return "没听清是哪一杯，等她说清楚再记。"
            target = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
            if not health_logic.is_valid_date(target):
                return "日期格式不对，需要 YYYY-MM-DD。"
            if milktea.is_future(target):
                return "这个日期还没到，先记到今天吧。"

            entry = milktea.add_entry(
                self._data_dir,
                target,
                {
                    "brand": brand,
                    "name": name,
                    "sugar_text": sugar,
                    "ice": ice,
                    "toppings": toppings,
                    "rating": rating or None,
                    "note": note,
                },
                source=milktea.SOURCE_CHAT,
            )
            if entry is None:
                return "这条没记下：至少要有品牌或饮品名。"
            logger.info(
                f"[Xavier_care] 记录奶茶 {target}: {entry.get('brand')}{entry.get('name')}"
            )
            head = "".join(x for x in (entry.get("brand"), entry.get("name")) if x)
            return f"已记下：{target} {head}。"
        except Exception:
            logger.exception("[Xavier_care] 记录奶茶失败")
            return "记录奶茶时出错了。"

    @filter.llm_tool(name="query_milktea")
    async def query_milktea(self, event: AstrMessageEvent, days: int = 7, ym: str = "") -> str:
        """查她的奶茶记录。聊到口味偏好、要不要点奶茶、或者她想回顾时调用。

        Args:
            days(int): 回顾最近多少天，默认 7，最多 90。
            ym(string): 指定月份 YYYY-MM；填了就按月汇总，此时忽略 days。
        """
        try:
            if ym and ym.strip():
                month = ym.strip()
                s = milktea.month_summary(self._data_dir, month)
                if not s["total"]:
                    return f"{month} 没有奶茶记录。"
                detail = "\n".join(
                    milktea.describe_day(d, milktea.load_day(self._data_dir, d))
                    for d in sorted(s["days"])
                )
                return f"{month} 共 {s['total']} 杯，最常喝：{s['favorite'] or '—'}\n{detail}"
            return milktea.describe_recent(self._data_dir, days)
        except Exception:
            logger.exception("[Xavier_care] 查询奶茶记录失败")
            return "查奶茶记录时出错了。"

    @filter.command("health")
    async def cmd_health(self, event: AstrMessageEvent, action: str = ""):
        """/health [on|off|test|setup|base|log|mark|end|chat] 健康插件总入口。

        不带参数时给出总览：主动关怀状态 + 经期状态。
        """
        try:
            if not event.is_private_chat():
                return
        except Exception:
            # 判断不出会话类型时也不回复，避免把健康信息发进群里。
            return

        act = (action or "").lower().strip()

        try:
            if act == "on":
                self._monitor_runtime_enabled = True
                if not self._monitor_task or self._monitor_task.done():
                    self._monitor_task = asyncio.create_task(self._monitor_loop())
                yield event.plain_result("主动关怀已开启（本次运行有效，重启后按配置为准）")
                return

            if act == "off":
                self._monitor_runtime_enabled = False
                yield event.plain_result("主动关怀已关闭")
                return

            if act == "test":
                yield event.plain_result(self._render_scan_result())
                return

            if act in ("setup", "connect", "配置", "连接"):
                yield event.plain_result(self._render_setup_card())
                return

            if act in ("base", "baseline"):
                yield event.plain_result(self._render_baseline())
                return

            if act in ("log", "history"):
                yield event.plain_result(self._render_care_log(limit=3))
                return

            if act in ("mark", "end"):
                yield event.plain_result(self._apply_period_mark(act))
                return

            if act == "chat":
                # 觉得聊天里插关怀不好用，一句话就能关掉（写回配置，长期生效）
                new_val = not self.chat_care.enabled
                self.config["care_in_chat_enabled"] = new_val
                saved = True
                try:
                    self.config.save_config()
                except Exception:
                    saved = False
                    logger.exception("[Xavier_care] 写「聊天内关怀」开关失败")
                yield event.plain_result(
                    f"聊天内自然关怀已{'开启' if new_val else '关闭'}"
                    + ("" if saved else "（本次运行有效，写入配置失败，见日志）")
                    + "\n\n"
                    + self._render_chat_care_status()
                )
                return

            # 默认（含 status）：总览
            yield event.plain_result(
                self._render_monitor_status()
                + "\n\n"
                + self._render_period_status()
                + "\n\n"
                + self._render_chat_care_status()
            )
            return

        except Exception:
            logger.exception("[Xavier_care] 处理 /health 指令时出错")
            yield event.plain_result("操作出错了，详情见服务端日志。")

    # -- 各动作的输出 ------------------------------------------------------

    @staticmethod
    def _local_ip() -> str:
        """取本机局域网 IP。取不到就回 127.0.0.1。"""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("223.5.5.5", 80))
            return sock.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    def _render_setup_card(self) -> str:
        """给手机上报端看的一张「照着填」卡片。"""
        token = str(self.config.get("auth_token") or "").strip()
        if not token:
            return "[手机连接信息]" + chr(10) + "密钥还没生成成功。请先重载插件，再发一次 /health setup。"
        if not self._listen_port:
            reason = self._listen_error or "端口被占用"
            return (
                "[手机连接信息]" + chr(10)
                + f"接收端没有启动成功（{reason}）。" + chr(10)
                + "在插件配置里把 listen_port 改成 8788 后重载插件，再发一次 /health setup。"
            )

        ip = self._local_ip()
        base = f"http://{ip}:{self._listen_port}"
        lines = [
            "[手机连接信息]",
            f"上报地址：{base}{ENDPOINT_PATH}",
            f"密钥：{token}",
            f"看板：{base}/health/dashboard",
            "",
            "手机和电脑要连同一个 Wi-Fi。",
            "把地址和密钥填进手机端一次就行。",
            "只会发私聊，不会发到群里。",
        ]
        if ip in ("127.0.0.1", "0.0.0.0"):
            lines.append("注意：没拿到局域网 IP，手机若不在同一台机器上，请把 127.0.0.1 换成电脑的局域网 IP。")
        return chr(10).join(lines)

    def _render_baseline(self) -> str:
        """健康基线文本。"""
        info = self.monitor.get_baseline()
        lines = [
            "[健康基线]",
            f"数据天数 : {info['days']} 天",
            f"日期范围 : {info['range'] or '（无）'}",
        ]
        for key, label in (
            ("resting_hr", "静息心率"),
            ("sleep_min", "睡眠时长"),
            ("hrv_ms", "HRV"),
            ("spo2", "血氧"),
        ):
            v = info.get(key) or {}
            m, s, n = v.get("mean"), v.get("std"), v.get("n")
            if m is None:
                lines.append(f"{label:<6}: 无数据（{n} 条）")
            else:
                lines.append(f"{label:<6}: 均值 {m}，标准差 {s}（{n} 条）")
        return "\n".join(lines)

    def _render_scan_result(self) -> str:
        """手动跑一次异常扫描，列出候选关怀。"""
        alerts = self.monitor.scan()
        baseline = int(self.config.get("baseline_days", 7))
        have = len(self.monitor._load_recent(baseline))
        if not alerts:
            return f"本次无异常。\n基线天数: {have}/{baseline}"
        lines = [f"检测到 {len(alerts)} 条候选关怀："]
        for a in alerts:
            cd = "（同簇冷却中）" if self.guard.is_cluster_cooling(a.get("type") or "") else ""
            lines.append(f"  - [{a['type']}]{cd} {a['hint']}")
        return "\n".join(lines)

    def _render_care_log(self, limit: int = 3) -> str:
        """最近若干条关怀记录，默认只取 3 条，避免刷屏。"""
        log_path = self._data_dir / "care_log.jsonl"
        if not log_path.is_file():
            return "还没有推送过任何关怀。"
        try:
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            return "读取关怀日志失败。"
        recent = lines[-limit:]
        out = [f"[最近 {len(recent)} 条关怀]"]
        for ln in reversed(recent):
            try:
                j = json.loads(ln)
            except Exception:
                continue
            mark = "✅" if j.get("ok") else "🟡"
            via = j.get("via") or ""
            out.append(
                f"{j.get('ts','')}  [{j.get('type','')}] {mark} {via}\n"
                f"  {j.get('hint','')}"
            )
        return "\n".join(out)

    def _render_monitor_status(self) -> str:
        """主动关怀运行状态。"""
        baseline = int(self.config.get("baseline_days", 7))
        have = len(self.monitor._load_recent(baseline))
        receive = (
            f"✅ 端口 {self._listen_port}" if self._listen_port
            else f"❌ {self._listen_error or '未启动'}"
        )
        lines = [
            "[健康主动关怀 状态]",
            f"启用     : {'✅' if self._monitor_runtime_enabled else '❌'}",
            f"接收端   : {receive}",
            f"轮询间隔 : {int(self.config.get('check_interval', 900))}s",
            f"基线天数 : {have}/{baseline}",
            f"静默时段 : {int(self.config.get('quiet_hours_start', 23))} - "
            f"{int(self.config.get('quiet_hours_end', 7))} 点",
        ]
        lines.extend(self.guard.status_lines())
        lines.append(f"簇冷却   : {self.guard.cluster_status()}")
        return "\n".join(lines)

    def _render_chat_care_status(self) -> str:
        """聊天内自然关怀状态。"""
        lines = [
            "[聊天内自然关怀 状态]",
            f"启用     : {'✅' if self.chat_care.enabled else '❌'}",
            f"每轮最多 : {int(self.config.get('chat_hint_max_items', 2))} 条要点",
            f"静默期   : 同一要点 {self.config.get('chat_hint_ttl_hours', 6)} 小时内不重复提",
            "反向去重 : "
            f"{'✅' if self.config.get('chat_hint_dedup_push', True) else '❌'}"
            "（聊天提过 → 当天不再单独推）",
        ]
        try:
            today = sorted(self.chat_care.mentioned_types_today())
            lines.append(f"今天已提 : {', '.join(today) if today else '（无）'}")
        except Exception:
            lines.append("今天已提 : 读取失败（见日志）")
        return "\n".join(lines)

    def _render_period_status(self) -> str:
        """经期 / 周期状态。"""
        try:
            status = health_logic.compute_period_status(self._data_dir)
        except Exception:
            logger.exception("[Xavier_care] 推算经期状态失败")
            return "[经期 / 周期状态]\n推算失败了，详情见日志。"
        lines = ["[经期 / 周期状态]"]
        if status.get("in_period"):
            lines.append(f"当前    : 在经期，第 {status.get('period_day')} 天")
        else:
            d = status.get("days_to_next")
            lines.append(
                "当前    : 不在经期"
                + (f"，距下次约 {d} 天" if d is not None else "")
            )
        lines.append(f"上次起始: {status.get('last_start') or '（无记录）'}")
        nd = status.get("next_start_date")
        if nd:
            try:
                d = datetime.strptime(nd, "%Y-%m-%d")
                lines.append(f"下次预测: {d.month} 月 {d.day} 日")
            except Exception:
                lines.append(f"下次预测: {nd}")
        lines.append(f"平均周期: {status.get('avg_cycle')} 天")
        lines.append(f"平均经期: {status.get('avg_length')} 天")
        return "\n".join(lines)

    def _apply_period_mark(self, act: str) -> str:
        """手动标记今天是否在经期。act 为 mark 或 end。"""
        today = datetime.now().strftime("%Y-%m-%d")
        data_dir = self._data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / f"{today}.json"

        merged = {}
        if target.is_file():
            try:
                merged = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                merged = {}
        merged["date"] = today
        merged["in_period"] = (act == "mark")
        if act == "mark" and "period_flow" not in merged:
            merged["period_flow"] = "中等"

        try:
            text = json.dumps(merged, ensure_ascii=False, indent=2)
            tmp = data_dir / f".{today}.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            return "写入失败，请检查数据目录权限。"

        if act == "mark":
            return f"已标记 {today} 为经期 ✅"
        return f"已标记 {today} 为不在经期 ✅"
