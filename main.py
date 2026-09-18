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
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import health_logic
from .monitor import HealthMonitor

# 插件目录名，同时用作数据子目录名。数据存在 AstrBot 的 data 目录下，
# 不放插件自身目录，这样更新/重装插件不丢历史数据。
PLUGIN_DIR_NAME = "astrbot_plugin_Xavier_care"

# 插件更名前使用的目录名，仅用于历史数据的一次性迁移。
LEGACY_DIR_NAME = "astrbot_plugin_health_bridge"

# 接收端点路径。固定值；真正的门锁是 auth_token，不是这个路径。
ENDPOINT_PATH = "/health/report"

# 鉴权请求头名。手机上报时带 X-Auth-Token: <auth_token>。
AUTH_HEADER = "X-Auth-Token"

# 请求体上限（健康数据只有几百字节，给到 64KB 足够，挡掉超大请求）。
MAX_BODY_BYTES = 64 * 1024


@register(PLUGIN_DIR_NAME, "pupotato", "接收手机推送的健康数据并存储，提供一个工具供角色读取最近的身体状态。", "1.3.0")
class HealthBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # data/plugin_data/astrbot_plugin_Xavier_care/<date>.json
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_DIR_NAME
        # aiohttp 监听相关对象，启动后赋值，便于 terminate 时干净关闭。
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._last_event: AstrMessageEvent | None = None

        # 健康异常检测 + 主动关怀
        self.monitor = HealthMonitor(
            data_dir=self._data_dir,
            config=self.config,
            cooldown_path=self._data_dir / "_cooldowns.json",
        )
        self._monitor_task: asyncio.Task | None = None
        # 运行时开关（/health on|off 会即时改它，不依赖 config 写回）
        self._monitor_runtime_enabled: bool = bool(
            self.config.get("monitor_enabled", False)
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_record(self, event: AstrMessageEvent):
        # 缓存最近一次的真实交互事件，拿到 umo、cqhttp 实例和 bot 身份
        if not event.get_sender_id() or event.get_sender_id() == event.get_self_id():
            return
        self._last_event = event

    def _migrate_legacy_data_dir(self) -> None:
        """插件更名后的一次性迁移：把旧目录里的数据复制过来（只复制，不删除旧目录）。"""
        legacy = Path(get_astrbot_data_path()) / "plugin_data" / LEGACY_DIR_NAME
        if not legacy.is_dir() or legacy == self._data_dir:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for item in legacy.iterdir():
                target = self._data_dir / item.name
                if item.is_file() and not target.exists():
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
        await self._start_server()

        # 启动主动关怀轮询（用运行时开关判断，/health on 也能补起）
        if self._monitor_runtime_enabled:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info(
                f"[Xavier_care] 主动关怀已启动，间隔 "
                f"{int(self.config.get('check_interval', 900))}s"
            )

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
                "请在 WebUI 插件配置里查看 auth_token，并把同样的值填进手机上报端。"
            )
        except Exception:
            self.config["auth_token"] = ""
            logger.warning(
                "[Xavier_care] 自动生成密钥后写入配置失败，已放弃以避免密钥落日志。"
                "接收端在配置前会拒收一切；请在 WebUI 手动填写 auth_token 后重载插件。"
            )

    # -- 模块一：接收端 ---------------------------------------------------

    async def _start_server(self) -> None:
        """启动 aiohttp 监听。失败只记日志、不抛异常，避免连累其他插件。"""
        await self._stop_server()

        try:
            port = int(self.config.get("listen_port", 8787))
        except (TypeError, ValueError):
            port = 8787

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
            # 静态资源（图标/装饰图），文件放插件目录下的 assets/
            app.router.add_get("/health/assets/{name}", self._handle_asset)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=port)
            await site.start()
            self._app, self._runner, self._site = app, runner, site
            logger.info(
                f"[Xavier_care] 接收端已启动：监听 0.0.0.0:{port}{ENDPOINT_PATH}（仅 POST）"
            )
            if not self.config.get("auth_token"):
                logger.warning(
                    "[Xavier_care] auth_token 尚未配置，配置前所有上报都会被拒绝（401）。"
                    "请在 WebUI 插件配置里填写。"
                )
        except Exception:
            logger.exception(
                f"[Xavier_care] 接收端启动失败（端口 {port} 可能被占用或被防火墙拦）。"
                "读取工具仍可用，但暂时收不到新数据。"
            )
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    logger.exception("[Xavier_care] 回收启动失败的监听器时出错")

    async def _stop_server(self) -> None:
        site, runner = self._site, self._runner
        self._site = self._runner = self._app = None
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
        umo = self.config.get("push_target_umo") or None
        bot_self_id = None
        cq_bot = None

        if self._last_event:
            if not umo:
                umo = self._last_event.unified_msg_origin
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
                for mgr_name in ("platform_manager", "platform_mgr", "_platform_manager"):
                    mgr = getattr(self.context, mgr_name, None)
                    if not mgr:
                        continue
                    for list_name in ("platforms", "_platforms", "adapters", "_adapters"):
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
            logger.info("[Xavier_care] 尚未记录到最近会话交互，跳过即时唤醒")
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
            start = int(self.config.get("quiet_hours_start", 23))
            end = int(self.config.get("quiet_hours_end", 7))
        except (TypeError, ValueError):
            return False
        if start == end:
            return False
        if start < end:
            return start <= now_h < end
        return now_h >= start or now_h < end

    async def _monitor_tick(self) -> None:
        """一次扫描：检测异常 → 过滤冷却 → 推送。一次 tick 最多推一条。"""
        if not self._monitor_runtime_enabled:
            return
        if self._in_quiet_hours():
            logger.info("[Xavier_care] 处于静默时段，跳过本次检测")
            return

        alerts = self.monitor.scan()
        if not alerts:
            return

        for a in alerts:
            key = a.get("cooldown_key") or a.get("type")
            if self.monitor._in_cooldown(key):
                continue

            umo = self.config.get("push_target_umo") or (
                self._last_event.unified_msg_origin if self._last_event else ""
            )

            ok = False
            try:
                ok = await self._trigger_event_wakeup(
                    f"【系统健康感知】{a['hint']}"
                    f"请结合当前对话上下文和你们的关系，用你的人格自然地说一句关心的话，"
                    f"不要罗列数据、不要像健康报告。"
                )
            except Exception:
                logger.exception("[Xavier_care] 注入关怀失败")

            via = "inject"
            if not ok:
                fallback = (self.config.get("fallback_care") or "").strip()
                if fallback and umo:
                    sent = await self._send_direct(umo, fallback)
                    via = "fallback"
                    logger.info(
                        f"[Xavier_care] 注入失败，已用兜底文案直发: ok={sent}"
                    )
                else:
                    logger.warning(
                        "[Xavier_care] 注入失败且未配置兜底文案，已跳过本次推送"
                    )

            self.monitor._mark(key)
            self._log_care(a, umo, ok, via=via)
            if ok:
                logger.info(f"[Xavier_care] 已推送健康关怀: {a['type']} via={via}")
            else:
                logger.warning(f"[Xavier_care] 关怀注入失败，已跳过: {a['type']}")

            break

    def _run_cleanup(self) -> None:
        try:
            retention = int(self.config.get("retention_days", 30))
        except (TypeError, ValueError):
            retention = 30
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
            return web.Response(text=content, content_type="text/html", charset="utf-8")
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
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return web.json_response({"ok": True, "data": data, "server_time": now_str})
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

    async def _handle_asset(self, request: web.Request) -> web.Response:
        """返回插件 assets/ 目录下的静态图片。防路径穿越。"""
        from urllib.parse import unquote
        name = unquote(request.match_info.get("name", ""))
        if not name or "/" in name or "\\" in name or ".." in name:
            return web.Response(status=404)
        path = Path(__file__).parent / "assets" / name
        if not path.is_file():
            return web.Response(status=404)
        return web.FileResponse(path)

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

    @filter.command("health")
    async def cmd_health(self, event: AstrMessageEvent, action: str = ""):
        """/health [on|off|test|base|log|mark|end] 健康插件总入口。

        不带参数时给出总览：主动关怀状态 + 经期状态。
        """
        try:
            if not event.is_private_chat():
                return
        except Exception:
            pass

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

            if act in ("base", "baseline"):
                yield event.plain_result(self._render_baseline())
                return

            if act in ("log", "history"):
                yield event.plain_result(self._render_care_log(limit=3))
                return

            if act in ("mark", "end"):
                yield event.plain_result(self._apply_period_mark(act))
                return

            # 默认（含 status）：总览
            yield event.plain_result(
                self._render_monitor_status() + "\n\n" + self._render_period_status()
            )
            return

        except Exception:
            logger.exception("[Xavier_care] 处理 /health 指令时出错")
            yield event.plain_result("操作出错了，详情见服务端日志。")

    # -- 各动作的输出 ------------------------------------------------------

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
            key = a.get("cooldown_key") or a.get("type")
            cd = "（冷却中）" if self.monitor._in_cooldown(key) else ""
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
        lines = [
            "[健康主动关怀 状态]",
            f"启用     : {'✅' if self._monitor_runtime_enabled else '❌'}",
            f"轮询间隔 : {int(self.config.get('check_interval', 900))}s",
            f"基线天数 : {have}/{baseline}",
            f"静默时段 : {int(self.config.get('quiet_hours_start', 23))} - "
            f"{int(self.config.get('quiet_hours_end', 7))} 点",
        ]
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
