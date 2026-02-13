"""
Mochat 渠道实现 - 基于 Socket.IO 的实时通信，带 HTTP 轮询回退。

本模块实现了 Mochat（轻量版 OpenClaw 客户端）的消息收发功能：
- 入站：通过 Socket.IO WebSocket 实时接收消息事件，或回退到 HTTP 长轮询
- 出站：通过 Mochat HTTP API 发送消息到会话或面板

架构特点：
- 双模式接收：优先 WebSocket（实时），回退 HTTP 轮询（兼容性好）
- 支持会话（Session）和面板（Panel）两种目标类型
- 自动发现新会话和面板（通过 * 通配符配置）
- 消息缓冲和延迟发送机制（支持 @提及 聚合）
- 游标持久化到本地文件（断线恢复不丢消息）
- 消息去重（每个目标独立维护最近 2000 条 ID）

依赖：
- python-socketio：Socket.IO 客户端（pip install python-socketio）
- httpx：异步 HTTP 客户端
- msgpack（可选）：Socket.IO 消息序列化优化

二开提示：
- 这是最复杂的渠道实现，适合作为学习高级异步编程模式的参考
- 如需添加语音支持，可在 _process_inbound_event 中拦截音频类型消息
- 面板消息支持 @提及 过滤，适合构建多 Agent 群聊场景
"

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import MochatConfig
from nanobot.utils.helpers import get_data_path

try:
    import socketio
    SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None
    SOCKETIO_AVAILABLE = False

try:
    import msgpack  # noqa: F401
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False

MAX_SEEN_MESSAGE_IDS = 2000        # 每个目标的消息去重缓存上限
CURSOR_SAVE_DEBOUNCE_S = 0.5       # 游标保存防抖延迟（秒）


# ---------------------------------------------------------------------------
# 数据类定义
# ---------------------------------------------------------------------------

@dataclass
class MochatBufferedEntry:
    """延迟分发的入站消息缓冲条目。用于 reply_delay_mode 模式下聚合多条消息后一次性处理。"""
    raw_body: str                     # 原始消息文本
    author: str                       # 发送者 ID
    sender_name: str = ""             # 发送者昵称
    sender_username: str = ""         # 发送者用户名
    timestamp: int | None = None      # 消息时间戳（毫秒）
    message_id: str = ""              # 消息唯一 ID
    group_id: str = ""                # 所属群组 ID（空表示非群聊）


@dataclass
class DelayState:
    """每个目标的延迟消息状态。管理消息缓冲列表和定时器。"""
    entries: list[MochatBufferedEntry] = field(default_factory=list)  # 缓冲的消息列表
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)          # 并发保护锁
    timer: asyncio.Task | None = None                                 # 延迟刷新定时器任务


@dataclass
class MochatTarget:
    """出站目标解析结果。区分会话（Session）和面板（Panel）两种目标类型。"""
    id: str            # 目标 ID
    is_panel: bool     # 是否为面板类型（True=面板，False=会话）


# ---------------------------------------------------------------------------
# 纯函数辅助工具（无副作用）
# ---------------------------------------------------------------------------


def _safe_dict(value: Any) -> dict:
    """安全类型转换：如果 value 是 dict 则返回，否则返回空字典。"""
    return value if isinstance(value, dict) else {}


def _str_field(src: dict, *keys: str) -> str:
    """从字典中按优先级查找第一个非空字符串值（已去除首尾空格）。"""
    for k in keys:
        v = src.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _make_synthetic_event(
    message_id: str, author: str, content: Any,
    meta: Any, group_id: str, converse_id: str,
    timestamp: Any = None, *, author_info: Any = None,
) -> dict[str, Any]:
    """构建合成的 message.add 事件字典。用于将 HTTP 轮询结果转换为统一的事件格式。"""
    payload: dict[str, Any] = {
        "messageId": message_id, "author": author,
        "content": content, "meta": _safe_dict(meta),
        "groupId": group_id, "converseId": converse_id,
    }
    if author_info is not None:
        payload["authorInfo"] = _safe_dict(author_info)
    return {
        "type": "message.add",
        "timestamp": timestamp or datetime.utcnow().isoformat(),
        "payload": payload,
    }


def normalize_mochat_content(content: Any) -> str:
    """将消息内容标准化为文本。支持字符串、None、JSON 对象等多种输入格式。"""
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def resolve_mochat_target(raw: str) -> MochatTarget:
    """
    从用户提供的目标字符串解析出目标 ID 和类型。

    支持的前缀格式：
    - mochat:<id>：通用前缀
    - group:<id> / channel:<id> / panel:<id>：强制为面板类型
    - session_xxx：自动识别为会话类型
    - 其他：默认为面板类型
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return MochatTarget(id="", is_panel=False)

    lowered = trimmed.lower()
    cleaned, forced_panel = trimmed, False
    for prefix in ("mochat:", "group:", "channel:", "panel:"):
        if lowered.startswith(prefix):
            cleaned = trimmed[len(prefix):].strip()
            forced_panel = prefix in {"group:", "channel:", "panel:"}
            break

    if not cleaned:
        return MochatTarget(id="", is_panel=False)
    return MochatTarget(id=cleaned, is_panel=forced_panel or not cleaned.startswith("session_"))


def extract_mention_ids(value: Any) -> list[str]:
    """从异构的 @提及 数据中提取用户 ID 列表。支持字符串列表和字典列表两种格式。"""
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            for key in ("id", "userId", "_id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(candidate.strip())
                    break
    return ids


def resolve_was_mentioned(payload: dict[str, Any], agent_user_id: str) -> bool:
    """判断当前 Agent 是否在消息中被 @提及。先检查 meta 中的提及字段，回退到正文文本匹配。"""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        if meta.get("mentioned") is True or meta.get("wasMentioned") is True:
            return True
        for f in ("mentions", "mentionIds", "mentionedUserIds", "mentionedUsers"):
            if agent_user_id and agent_user_id in extract_mention_ids(meta.get(f)):
                return True
    if not agent_user_id:
        return False
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return False
    return f"<@{agent_user_id}>" in content or f"@{agent_user_id}" in content


def resolve_require_mention(config: MochatConfig, session_id: str, group_id: str) -> bool:
    """判断群组/面板对话是否要求 @提及 才响应。按优先级查找：特定群组配置 > 通配符配置 > 全局配置。"""
    groups = config.groups or {}
    for key in (group_id, session_id, "*"):
        if key and key in groups:
            return bool(groups[key].require_mention)
    return bool(config.mention.require_in_groups)


def build_buffered_body(entries: list[MochatBufferedEntry], is_group: bool) -> str:
    """将一条或多条缓冲消息合并为单个文本正文。群聊模式下会添加发送者标签前缀。"""
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0].raw_body
    lines: list[str] = []
    for entry in entries:
        if not entry.raw_body:
            continue
        if is_group:
            label = entry.sender_name.strip() or entry.sender_username.strip() or entry.author
            if label:
                lines.append(f"{label}: {entry.raw_body}")
                continue
        lines.append(entry.raw_body)
    return "\n".join(lines).strip()


def parse_timestamp(value: Any) -> int | None:
    """将事件时间戳（ISO 8601 字符串）解析为 epoch 毫秒数。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 渠道主类
# ---------------------------------------------------------------------------


class MochatChannel(BaseChannel):
    """
    Mochat 渠道 - 基于 Socket.IO 的实时通信，带 HTTP 轮询回退。

    架构设计：
    - 双模式接收：优先 WebSocket（实时），回退 HTTP 长轮询（兼容性好）
    - 双目标类型：会话（Session，1:1 对话）和面板（Panel，群组频道）
    - 自动发现：通过 * 通配符自动发现并订阅新会话/面板
    - 游标持久化：断线恢复不丢消息
    - 消息缓冲：支持延迟聚合发送（reply_delay_mode）
    - @提及过滤：面板消息可配置为仅响应被提及的消息

    这是所有渠道中最复杂的实现，融合了 WebSocket、HTTP 轮询、
    事件订阅、游标管理、消息缓冲等多种异步编程模式。
    """

    name = "mochat"

    def __init__(self, config: MochatConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: MochatConfig = config
        self._http: httpx.AsyncClient | None = None
        self._socket: Any = None
        self._ws_connected = self._ws_ready = False

        self._state_dir = get_data_path() / "mochat"
        self._cursor_path = self._state_dir / "session_cursors.json"
        self._session_cursor: dict[str, int] = {}
        self._cursor_save_task: asyncio.Task | None = None

        self._session_set: set[str] = set()
        self._panel_set: set[str] = set()
        self._auto_discover_sessions = self._auto_discover_panels = False

        self._cold_sessions: set[str] = set()
        self._session_by_converse: dict[str, str] = {}

        self._seen_set: dict[str, set[str]] = {}
        self._seen_queue: dict[str, deque[str]] = {}
        self._delay_states: dict[str, DelayState] = {}

        self._fallback_mode = False
        self._session_fallback_tasks: dict[str, asyncio.Task] = {}
        self._panel_fallback_tasks: dict[str, asyncio.Task] = {}
        self._refresh_task: asyncio.Task | None = None
        self._target_locks: dict[str, asyncio.Lock] = {}

    # ---- 生命周期管理 ---------------------------------------------------------

    async def start(self) -> None:
        """
        启动 Mochat 渠道。

        流程：
        1. 验证 claw_token 配置
        2. 初始化 HTTP 客户端和状态目录
        3. 加载持久化的游标（用于断线恢复）
        4. 从配置中解析目标列表
        5. 刷新远程目标（自动发现新会话/面板）
        6. 尝试 WebSocket 连接，失败则启动 HTTP 轮询回退
        7. 启动定期刷新任务
        """
        if not self.config.claw_token:
            logger.error("Mochat claw_token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        await self._load_session_cursors()
        self._seed_targets_from_config()
        await self._refresh_targets(subscribe_new=False)

        if not await self._start_socket_client():
            await self._ensure_fallback_workers()

        self._refresh_task = asyncio.create_task(self._refresh_loop())
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        停止 Mochat 渠道并清理所有资源。

        清理工作：
        1. 取消定期刷新任务
        2. 停止所有 HTTP 轮询回退工作协程
        3. 取消所有延迟消息定时器
        4. 断开 Socket.IO 连接
        5. 保存游标到磁盘
        6. 关闭 HTTP 客户端
        """
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

        await self._stop_fallback_workers()
        await self._cancel_delay_timers()

        if self._socket:
            try:
                await self._socket.disconnect()
            except Exception:
                pass
            self._socket = None

        if self._cursor_save_task:
            self._cursor_save_task.cancel()
            self._cursor_save_task = None
        await self._save_session_cursors()

        if self._http:
            await self._http.aclose()
            self._http = None
        self._ws_connected = self._ws_ready = False

    async def send(self, msg: OutboundMessage) -> None:
        """
        发送消息到 Mochat 会话或面板。

        流程：
        1. 合并文本内容和媒体附件
        2. 解析目标类型（会话 vs 面板）
        3. 调用对应的 API 发送消息

        参数:
            msg: 出站消息对象，chat_id 格式支持：
                 - session_xxx：会话 ID
                 - panel:xxx / group:xxx：面板 ID
                 - 普通 ID：自动判断类型
        """
        if not self.config.claw_token:
            logger.warning("Mochat claw_token missing, skip send")
            return

        parts = ([msg.content.strip()] if msg.content and msg.content.strip() else [])
        if msg.media:
            parts.extend(m for m in msg.media if isinstance(m, str) and m.strip())
        content = "\n".join(parts).strip()
        if not content:
            return

        target = resolve_mochat_target(msg.chat_id)
        if not target.id:
            logger.warning("Mochat outbound target is empty")
            return

        is_panel = (target.is_panel or target.id in self._panel_set) and not target.id.startswith("session_")
        try:
            if is_panel:
                await self._api_send("/api/claw/groups/panels/send", "panelId", target.id,
                                     content, msg.reply_to, self._read_group_id(msg.metadata))
            else:
                await self._api_send("/api/claw/sessions/send", "sessionId", target.id,
                                     content, msg.reply_to)
        except Exception as e:
            logger.error(f"Failed to send Mochat message: {e}")

    # ---- 配置/初始化辅助 ---------------------------------------------

    def _seed_targets_from_config(self) -> None:
        """从配置中解析初始的会话和面板目标列表。支持 * 通配符启用自动发现。"""
        sessions, self._auto_discover_sessions = self._normalize_id_list(self.config.sessions)
        panels, self._auto_discover_panels = self._normalize_id_list(self.config.panels)
        self._session_set.update(sessions)
        self._panel_set.update(panels)
        for sid in sessions:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

    @staticmethod
    def _normalize_id_list(values: list[str]) -> tuple[list[str], bool]:
        """标准化 ID 列表。返回 (去重排序的 ID 列表, 是否包含 * 通配符)。"""
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return sorted({v for v in cleaned if v != "*"}), "*" in cleaned

    # ---- WebSocket 连接管理 ---------------------------------------------------------

    async def _start_socket_client(self) -> bool:
        """
        启动 Socket.IO WebSocket 客户端。

        流程：
        1. 检查 python-socketio 是否可用
        2. 选择序列化方式（msgpack 优先，回退 JSON）
        3. 创建 Socket.IO 客户端并注册事件处理器
        4. 建立 WebSocket 连接

        返回:
            连接成功返回 True，失败返回 False
        """
        if not SOCKETIO_AVAILABLE:
            logger.warning("python-socketio not installed, Mochat using polling fallback")
            return False

        serializer = "default"
        if not self.config.socket_disable_msgpack:
            if MSGPACK_AVAILABLE:
                serializer = "msgpack"
            else:
                logger.warning("msgpack not installed but socket_disable_msgpack=false; using JSON")

        client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=self.config.max_retry_attempts or None,
            reconnection_delay=max(0.1, self.config.socket_reconnect_delay_ms / 1000.0),
            reconnection_delay_max=max(0.1, self.config.socket_max_reconnect_delay_ms / 1000.0),
            logger=False, engineio_logger=False, serializer=serializer,
        )

        @client.event
        async def connect() -> None:
            self._ws_connected, self._ws_ready = True, False
            logger.info("Mochat websocket connected")
            subscribed = await self._subscribe_all()
            self._ws_ready = subscribed
            await (self._stop_fallback_workers() if subscribed else self._ensure_fallback_workers())

        @client.event
        async def disconnect() -> None:
            if not self._running:
                return
            self._ws_connected = self._ws_ready = False
            logger.warning("Mochat websocket disconnected")
            await self._ensure_fallback_workers()

        @client.event
        async def connect_error(data: Any) -> None:
            logger.error(f"Mochat websocket connect error: {data}")

        @client.on("claw.session.events")
        async def on_session_events(payload: dict[str, Any]) -> None:
            await self._handle_watch_payload(payload, "session")

        @client.on("claw.panel.events")
        async def on_panel_events(payload: dict[str, Any]) -> None:
            await self._handle_watch_payload(payload, "panel")

        for ev in ("notify:chat.inbox.append", "notify:chat.message.add",
                    "notify:chat.message.update", "notify:chat.message.recall",
                    "notify:chat.message.delete"):
            client.on(ev, self._build_notify_handler(ev))

        socket_url = (self.config.socket_url or self.config.base_url).strip().rstrip("/")
        socket_path = (self.config.socket_path or "/socket.io").strip().lstrip("/")

        try:
            self._socket = client
            await client.connect(
                socket_url, transports=["websocket"], socketio_path=socket_path,
                auth={"token": self.config.claw_token},
                wait_timeout=max(1.0, self.config.socket_connect_timeout_ms / 1000.0),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to connect Mochat websocket: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            self._socket = None
            return False

    def _build_notify_handler(self, event_name: str):
        """为指定的 Socket.IO 事件名创建异步处理器函数。"""
        async def handler(payload: Any) -> None:
            if event_name == "notify:chat.inbox.append":
                await self._handle_notify_inbox_append(payload)
            elif event_name.startswith("notify:chat.message."):
                await self._handle_notify_chat_message(payload)
        return handler

    # ---- 事件订阅 ---------------------------------------------------------

    async def _subscribe_all(self) -> bool:
        """订阅所有已知的会话和面板事件流。返回是否全部订阅成功。"""
        ok = await self._subscribe_sessions(sorted(self._session_set))
        ok = await self._subscribe_panels(sorted(self._panel_set)) and ok
        if self._auto_discover_sessions or self._auto_discover_panels:
            await self._refresh_targets(subscribe_new=True)
        return ok

    async def _subscribe_sessions(self, session_ids: list[str]) -> bool:
        """通过 Socket.IO 订阅指定会话的事件流。首次订阅的冷启动会话会跳过历史消息。"""
        if not session_ids:
            return True
        for sid in session_ids:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

        ack = await self._socket_call("com.claw.im.subscribeSessions", {
            "sessionIds": session_ids, "cursors": self._session_cursor,
            "limit": self.config.watch_limit,
        })
        if not ack.get("result"):
            logger.error(f"Mochat subscribeSessions failed: {ack.get('message', 'unknown error')}")
            return False

        data = ack.get("data")
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            items = [i for i in data if isinstance(i, dict)]
        elif isinstance(data, dict):
            sessions = data.get("sessions")
            if isinstance(sessions, list):
                items = [i for i in sessions if isinstance(i, dict)]
            elif "sessionId" in data:
                items = [data]
        for p in items:
            await self._handle_watch_payload(p, "session")
        return True

    async def _subscribe_panels(self, panel_ids: list[str]) -> bool:
        """通过 Socket.IO 订阅指定面板的事件流。"""
        if not self._auto_discover_panels and not panel_ids:
            return True
        ack = await self._socket_call("com.claw.im.subscribePanels", {"panelIds": panel_ids})
        if not ack.get("result"):
            logger.error(f"Mochat subscribePanels failed: {ack.get('message', 'unknown error')}")
            return False
        return True

    async def _socket_call(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """封装 Socket.IO 的 call 方法（带超时和错误处理）。"""
        if not self._socket:
            return {"result": False, "message": "socket not connected"}
        try:
            raw = await self._socket.call(event_name, payload, timeout=10)
        except Exception as e:
            return {"result": False, "message": str(e)}
        return raw if isinstance(raw, dict) else {"result": True, "data": raw}

    # ---- 定期刷新/自动发现 -----------------------------------------------

    async def _refresh_loop(self) -> None:
        """定期刷新目标列表的后台循环。自动发现新会话和面板。"""
        interval_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await self._refresh_targets(subscribe_new=self._ws_ready)
            except Exception as e:
                logger.warning(f"Mochat refresh failed: {e}")
            if self._fallback_mode:
                await self._ensure_fallback_workers()

    async def _refresh_targets(self, subscribe_new: bool) -> None:
        """刷新远程目标列表。如果 subscribe_new 为 True，自动订阅新发现的目标。"""
        if self._auto_discover_sessions:
            await self._refresh_sessions_directory(subscribe_new)
        if self._auto_discover_panels:
            await self._refresh_panels(subscribe_new)

    async def _refresh_sessions_directory(self, subscribe_new: bool) -> None:
        """从 Mochat API 刷新会话目录。发现新会话时自动订阅或启动轮询。"""
        try:
            response = await self._post_json("/api/claw/sessions/list", {})
        except Exception as e:
            logger.warning(f"Mochat listSessions failed: {e}")
            return

        sessions = response.get("sessions")
        if not isinstance(sessions, list):
            return

        new_ids: list[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = _str_field(s, "sessionId")
            if not sid:
                continue
            if sid not in self._session_set:
                self._session_set.add(sid)
                new_ids.append(sid)
                if sid not in self._session_cursor:
                    self._cold_sessions.add(sid)
            cid = _str_field(s, "converseId")
            if cid:
                self._session_by_converse[cid] = sid

        if not new_ids:
            return
        if self._ws_ready and subscribe_new:
            await self._subscribe_sessions(new_ids)
        if self._fallback_mode:
            await self._ensure_fallback_workers()

    async def _refresh_panels(self, subscribe_new: bool) -> None:
        """从 Mochat API 刷新面板列表。仅获取文本类型面板（type=0）。"""
        try:
            response = await self._post_json("/api/claw/groups/get", {})
        except Exception as e:
            logger.warning(f"Mochat getWorkspaceGroup failed: {e}")
            return

        raw_panels = response.get("panels")
        if not isinstance(raw_panels, list):
            return

        new_ids: list[str] = []
        for p in raw_panels:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if isinstance(pt, int) and pt != 0:
                continue
            pid = _str_field(p, "id", "_id")
            if pid and pid not in self._panel_set:
                self._panel_set.add(pid)
                new_ids.append(pid)

        if not new_ids:
            return
        if self._ws_ready and subscribe_new:
            await self._subscribe_panels(new_ids)
        if self._fallback_mode:
            await self._ensure_fallback_workers()

    # ---- HTTP 轮询回退工作协程 --------------------------------------------------

    async def _ensure_fallback_workers(self) -> None:
        """确保每个目标都有对应的 HTTP 轮询回退协程在运行。WebSocket 不可用时启用。"""
        if not self._running:
            return
        self._fallback_mode = True
        for sid in sorted(self._session_set):
            t = self._session_fallback_tasks.get(sid)
            if not t or t.done():
                self._session_fallback_tasks[sid] = asyncio.create_task(self._session_watch_worker(sid))
        for pid in sorted(self._panel_set):
            t = self._panel_fallback_tasks.get(pid)
            if not t or t.done():
                self._panel_fallback_tasks[pid] = asyncio.create_task(self._panel_poll_worker(pid))

    async def _stop_fallback_workers(self) -> None:
        """停止所有 HTTP 轮询回退协程。通常在 WebSocket 连接恢复后调用。"""
        self._fallback_mode = False
        tasks = [*self._session_fallback_tasks.values(), *self._panel_fallback_tasks.values()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session_fallback_tasks.clear()
        self._panel_fallback_tasks.clear()

    async def _session_watch_worker(self, session_id: str) -> None:
        """单个会话的 HTTP 长轮询工作协程。使用 watch API 等待新消息。"""
        while self._running and self._fallback_mode:
            try:
                payload = await self._post_json("/api/claw/sessions/watch", {
                    "sessionId": session_id, "cursor": self._session_cursor.get(session_id, 0),
                    "timeoutMs": self.config.watch_timeout_ms, "limit": self.config.watch_limit,
                })
                await self._handle_watch_payload(payload, "session")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Mochat watch fallback error ({session_id}): {e}")
                await asyncio.sleep(max(0.1, self.config.retry_delay_ms / 1000.0))

    async def _panel_poll_worker(self, panel_id: str) -> None:
        """单个面板的 HTTP 轮询工作协程。定期获取最新消息列表。"""
        sleep_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running and self._fallback_mode:
            try:
                resp = await self._post_json("/api/claw/groups/panels/messages", {
                    "panelId": panel_id, "limit": min(100, max(1, self.config.watch_limit)),
                })
                msgs = resp.get("messages")
                if isinstance(msgs, list):
                    for m in reversed(msgs):
                        if not isinstance(m, dict):
                            continue
                        evt = _make_synthetic_event(
                            message_id=str(m.get("messageId") or ""),
                            author=str(m.get("author") or ""),
                            content=m.get("content"),
                            meta=m.get("meta"), group_id=str(resp.get("groupId") or ""),
                            converse_id=panel_id, timestamp=m.get("createdAt"),
                            author_info=m.get("authorInfo"),
                        )
                        await self._process_inbound_event(panel_id, evt, "panel")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Mochat panel polling error ({panel_id}): {e}")
            await asyncio.sleep(sleep_s)

    # ---- 入站事件处理 ------------------------------------------

    async def _handle_watch_payload(self, payload: dict[str, Any], target_kind: str) -> None:
        """处理 watch/subscribe 返回的事件载荷。更新游标并分发 message.add 事件。"""
        if not isinstance(payload, dict):
            return
        target_id = _str_field(payload, "sessionId")
        if not target_id:
            return

        lock = self._target_locks.setdefault(f"{target_kind}:{target_id}", asyncio.Lock())
        async with lock:
            prev = self._session_cursor.get(target_id, 0) if target_kind == "session" else 0
            pc = payload.get("cursor")
            if target_kind == "session" and isinstance(pc, int) and pc >= 0:
                self._mark_session_cursor(target_id, pc)

            raw_events = payload.get("events")
            if not isinstance(raw_events, list):
                return
            if target_kind == "session" and target_id in self._cold_sessions:
                self._cold_sessions.discard(target_id)
                return

            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                seq = event.get("seq")
                if target_kind == "session" and isinstance(seq, int) and seq > self._session_cursor.get(target_id, prev):
                    self._mark_session_cursor(target_id, seq)
                if event.get("type") == "message.add":
                    await self._process_inbound_event(target_id, event, target_kind)

    async def _process_inbound_event(self, target_id: str, event: dict[str, Any], target_kind: str) -> None:
        """
        处理单条入站消息事件。

        流程：
        1. 过滤：跳过自身消息、未授权发送者
        2. 去重：基于 message_id
        3. 解析：提取发送者信息、消息内容
        4. @提及检查：面板消息可能需要被提及才响应
        5. 缓冲/分发：根据 reply_delay_mode 决定立即分发还是缓冲
        """
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        author = _str_field(payload, "author")
        if not author or (self.config.agent_user_id and author == self.config.agent_user_id):
            return
        if not self.is_allowed(author):
            return

        message_id = _str_field(payload, "messageId")
        seen_key = f"{target_kind}:{target_id}"
        if message_id and self._remember_message_id(seen_key, message_id):
            return

        raw_body = normalize_mochat_content(payload.get("content")) or "[empty message]"
        ai = _safe_dict(payload.get("authorInfo"))
        sender_name = _str_field(ai, "nickname", "email")
        sender_username = _str_field(ai, "agentId")

        group_id = _str_field(payload, "groupId")
        is_group = bool(group_id)
        was_mentioned = resolve_was_mentioned(payload, self.config.agent_user_id)
        require_mention = target_kind == "panel" and is_group and resolve_require_mention(self.config, target_id, group_id)
        use_delay = target_kind == "panel" and self.config.reply_delay_mode == "non-mention"

        if require_mention and not was_mentioned and not use_delay:
            return

        entry = MochatBufferedEntry(
            raw_body=raw_body, author=author, sender_name=sender_name,
            sender_username=sender_username, timestamp=parse_timestamp(event.get("timestamp")),
            message_id=message_id, group_id=group_id,
        )

        if use_delay:
            delay_key = seen_key
            if was_mentioned:
                await self._flush_delayed_entries(delay_key, target_id, target_kind, "mention", entry)
            else:
                await self._enqueue_delayed_entry(delay_key, target_id, target_kind, entry)
            return

        await self._dispatch_entries(target_id, target_kind, [entry], was_mentioned)

    # ---- 去重/消息缓冲 -------------------------------------------------

    def _remember_message_id(self, key: str, message_id: str) -> bool:
        """记录消息 ID 用于去重。如果已存在返回 True（重复），否则返回 False（新消息）。"""
        seen_set = self._seen_set.setdefault(key, set())
        seen_queue = self._seen_queue.setdefault(key, deque())
        if message_id in seen_set:
            return True
        seen_set.add(message_id)
        seen_queue.append(message_id)
        while len(seen_queue) > MAX_SEEN_MESSAGE_IDS:
            seen_set.discard(seen_queue.popleft())
        return False

    async def _enqueue_delayed_entry(self, key: str, target_id: str, target_kind: str, entry: MochatBufferedEntry) -> None:
        """将消息加入延迟缓冲队列，重置刷新定时器。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            state.entries.append(entry)
            if state.timer:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._delay_flush_after(key, target_id, target_kind))

    async def _delay_flush_after(self, key: str, target_id: str, target_kind: str) -> None:
        """延迟定时器到期后触发缓冲区刷新。"""
        await asyncio.sleep(max(0, self.config.reply_delay_ms) / 1000.0)
        await self._flush_delayed_entries(key, target_id, target_kind, "timer", None)

    async def _flush_delayed_entries(self, key: str, target_id: str, target_kind: str, reason: str, entry: MochatBufferedEntry | None) -> None:
        """刷新延迟缓冲区：合并所有缓冲消息并分发。reason 为 'mention' 或 'timer'。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            if entry:
                state.entries.append(entry)
            current = asyncio.current_task()
            if state.timer and state.timer is not current:
                state.timer.cancel()
            state.timer = None
            entries = state.entries[:]
            state.entries.clear()
        if entries:
            await self._dispatch_entries(target_id, target_kind, entries, reason == "mention")

    async def _dispatch_entries(self, target_id: str, target_kind: str, entries: list[MochatBufferedEntry], was_mentioned: bool) -> None:
        """将一批消息条目合并为单条消息并转发到消息总线。"""
        if not entries:
            return
        last = entries[-1]
        is_group = bool(last.group_id)
        body = build_buffered_body(entries, is_group) or "[empty message]"
        await self._handle_message(
            sender_id=last.author, chat_id=target_id, content=body,
            metadata={
                "message_id": last.message_id, "timestamp": last.timestamp,
                "is_group": is_group, "group_id": last.group_id,
                "sender_name": last.sender_name, "sender_username": last.sender_username,
                "target_kind": target_kind, "was_mentioned": was_mentioned,
                "buffered_count": len(entries),
            },
        )

    async def _cancel_delay_timers(self) -> None:
        """取消所有延迟消息定时器并清空缓冲区。"""
        for state in self._delay_states.values():
            if state.timer:
                state.timer.cancel()
        self._delay_states.clear()

    # ---- Socket.IO 通知处理 ---------------------------------------------------

    async def _handle_notify_chat_message(self, payload: Any) -> None:
        """处理 notify:chat.message.* 事件。将通知转换为标准事件格式后处理。"""
        if not isinstance(payload, dict):
            return
        group_id = _str_field(payload, "groupId")
        panel_id = _str_field(payload, "converseId", "panelId")
        if not group_id or not panel_id:
            return
        if self._panel_set and panel_id not in self._panel_set:
            return

        evt = _make_synthetic_event(
            message_id=str(payload.get("_id") or payload.get("messageId") or ""),
            author=str(payload.get("author") or ""),
            content=payload.get("content"), meta=payload.get("meta"),
            group_id=group_id, converse_id=panel_id,
            timestamp=payload.get("createdAt"), author_info=payload.get("authorInfo"),
        )
        await self._process_inbound_event(panel_id, evt, "panel")

    async def _handle_notify_inbox_append(self, payload: Any) -> None:
        """处理 notify:chat.inbox.append 事件。用于检测新的会话消息。"""
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return
        detail = payload.get("payload")
        if not isinstance(detail, dict):
            return
        if _str_field(detail, "groupId"):
            return
        converse_id = _str_field(detail, "converseId")
        if not converse_id:
            return

        session_id = self._session_by_converse.get(converse_id)
        if not session_id:
            await self._refresh_sessions_directory(self._ws_ready)
            session_id = self._session_by_converse.get(converse_id)
        if not session_id:
            return

        evt = _make_synthetic_event(
            message_id=str(detail.get("messageId") or payload.get("_id") or ""),
            author=str(detail.get("messageAuthor") or ""),
            content=str(detail.get("messagePlainContent") or detail.get("messageSnippet") or ""),
            meta={"source": "notify:chat.inbox.append", "converseId": converse_id},
            group_id="", converse_id=converse_id, timestamp=payload.get("createdAt"),
        )
        await self._process_inbound_event(session_id, evt, "session")

    # ---- 游标持久化 ------------------------------------------------

    def _mark_session_cursor(self, session_id: str, cursor: int) -> None:
        """更新会话游标并触发防抖保存。游标单调递增，用于断线恢复时跳过已处理的消息。"""
        if cursor < 0 or cursor < self._session_cursor.get(session_id, 0):
            return
        self._session_cursor[session_id] = cursor
        if not self._cursor_save_task or self._cursor_save_task.done():
            self._cursor_save_task = asyncio.create_task(self._save_cursor_debounced())

    async def _save_cursor_debounced(self) -> None:
        """防抖保存：等待 0.5 秒后保存游标，避免高频写入磁盘。"""
        await asyncio.sleep(CURSOR_SAVE_DEBOUNCE_S)
        await self._save_session_cursors()

    async def _load_session_cursors(self) -> None:
        """从磁盘加载持久化的会话游标。"""
        if not self._cursor_path.exists():
            return
        try:
            data = json.loads(self._cursor_path.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read Mochat cursor file: {e}")
            return
        cursors = data.get("cursors") if isinstance(data, dict) else None
        if isinstance(cursors, dict):
            for sid, cur in cursors.items():
                if isinstance(sid, str) and isinstance(cur, int) and cur >= 0:
                    self._session_cursor[sid] = cur

    async def _save_session_cursors(self) -> None:
        """将当前会话游标保存到磁盘（JSON 格式）。"""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(json.dumps({
                "schemaVersion": 1, "updatedAt": datetime.utcnow().isoformat(),
                "cursors": self._session_cursor,
            }, ensure_ascii=False, indent=2) + "\n", "utf-8")
        except Exception as e:
            logger.warning(f"Failed to save Mochat cursor file: {e}")

    # ---- HTTP 请求辅助 ------------------------------------------------------

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Mochat API 发送 POST 请求并返回解析后的 JSON 响应。自动添加认证头。"""
        if not self._http:
            raise RuntimeError("Mochat HTTP client not initialized")
        url = f"{self.config.base_url.strip().rstrip('/')}{path}"
        response = await self._http.post(url, headers={
            "Content-Type": "application/json", "X-Claw-Token": self.config.claw_token,
        }, json=payload)
        if not response.is_success:
            raise RuntimeError(f"Mochat HTTP {response.status_code}: {response.text[:200]}")
        try:
            parsed = response.json()
        except Exception:
            parsed = response.text
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
            if parsed["code"] != 200:
                msg = str(parsed.get("message") or parsed.get("name") or "request failed")
                raise RuntimeError(f"Mochat API error: {msg} (code={parsed['code']})")
            data = parsed.get("data")
            return data if isinstance(data, dict) else {}
        return parsed if isinstance(parsed, dict) else {}

    async def _api_send(self, path: str, id_key: str, id_val: str,
                        content: str, reply_to: str | None, group_id: str | None = None) -> dict[str, Any]:
        """统一的消息发送辅助方法。支持会话和面板两种目标类型。"""
        body: dict[str, Any] = {id_key: id_val, "content": content}
        if reply_to:
            body["replyTo"] = reply_to
        if group_id:
            body["groupId"] = group_id
        return await self._post_json(path, body)

    @staticmethod
    def _read_group_id(metadata: dict[str, Any]) -> str | None:
        """从消息元数据中提取群组 ID。"""
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("group_id") or metadata.get("groupId")
        return value.strip() if isinstance(value, str) and value.strip() else None
