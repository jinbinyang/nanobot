"""
Telegram 渠道实现模块 - 基于 python-telegram-bot 库的长轮询模式。

本模块实现了 nanobot 与 Telegram 平台的对接，采用长轮询（Long Polling）模式，
无需公网 IP 或 Webhook，部署非常简单。

【核心功能】
1. 接收用户的文本、图片、语音、文档消息
2. 语音消息自动调用 Groq Whisper 进行转录（STT）
3. 将 Markdown 格式的回复转换为 Telegram 兼容的 HTML
4. 支持"正在输入..."状态指示器
5. 支持 /start、/new、/help 等命令

【Java 开发者类比】
类似于一个 Spring Boot 的 WebSocket 客户端：
- Application 对象类似于 Spring 的 ApplicationContext
- Handler 注册类似于 @Controller + @RequestMapping
- 长轮询类似于 HTTP 长连接轮询模式
- 消息总线发布类似于 Spring 的 ApplicationEventPublisher

【消息处理流程】
1. Telegram 服务器 → python-telegram-bot 库接收消息
2. _on_message() 解析消息内容（文本/媒体/语音转录）
3. _handle_message()（继承自 BaseChannel）发布到消息总线
4. AgentLoop 处理后通过 send() 方法回复
5. send() 将 Markdown 转为 HTML 后发送给 Telegram

【二开提示】
语音交互系统可以在此模块扩展：
- 当前已有 STT（语音转文字）：Groq Whisper 转录
- 可增加 TTS（文字转语音）：在 send() 中将文本转为语音文件后发送
- Telegram 支持发送语音消息 (send_voice)，可直接复用
"""

from __future__ import annotations

import asyncio
import re
from loguru import logger
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import TelegramConfig


def _markdown_to_telegram_html(text: str) -> str:
    """
    将 Markdown 格式文本转换为 Telegram 兼容的 HTML。

    Telegram 的 HTML 支持有限（仅支持 <b>、<i>、<code>、<pre>、<a> 等），
    因此需要进行定制化转换，而非使用通用 Markdown 解析器。

    转换策略采用"保护-转换-恢复"三步法：
    1. 先将代码块和行内代码提取并用占位符替换（保护代码内容不被误处理）
    2. 对剩余文本进行 Markdown → HTML 转换
    3. 最后将代码块恢复并包裹在 HTML 标签中

    参数:
        text: Markdown 格式的原始文本

    返回:
        Telegram 兼容的 HTML 格式文本
    """
    if not text:
        return ""
    
    # ===== 第1步：提取并保护代码块（防止代码内容被后续正则误处理） =====
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        """将代码块替换为占位符 \x00CB{index}\x00"""
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    # 匹配 ```language\n...\n``` 格式的代码块
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # ===== 第2步：提取并保护行内代码 =====
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        """将行内代码替换为占位符 \x00IC{index}\x00"""
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    # 匹配 `code` 格式的行内代码
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # ===== 第3步：转换 Markdown 语法为 HTML =====
    
    # 标题：# Title → 纯文本（Telegram 不支持 <h1> 等标签）
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 引用块：> text → 纯文本（Telegram HTML 不支持 blockquote）
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 转义 HTML 特殊字符（必须在其他 HTML 标签生成之前）
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 超链接：[text](url) → <a href="url">text</a>（必须在粗体/斜体之前处理）
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 粗体：**text** 或 __text__ → <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 斜体：_text_ → <i>text</i>（排除变量名中的下划线，如 some_var_name）
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 删除线：~~text~~ → <s>text</s>
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 无序列表：- item 或 * item → • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # ===== 第4步：恢复行内代码，包裹 HTML 标签 =====
    for i, code in enumerate(inline_codes):
        # 代码内容也需要转义 HTML 特殊字符
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # ===== 第5步：恢复代码块，包裹 HTML 标签 =====
    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


class TelegramChannel(BaseChannel):
    """
    Telegram 渠道实现 - 基于长轮询（Long Polling）模式。

    长轮询模式简单可靠，无需公网 IP 和 Webhook 配置。
    机器人主动向 Telegram 服务器请求新消息，适合开发和小规模部署。

    属性:
        config: Telegram 渠道配置（token、代理等）
        groq_api_key: Groq API 密钥，用于语音转录
        _app: python-telegram-bot 的 Application 实例
        _chat_ids: 发送者ID到聊天ID的映射（用于回复定位）
        _typing_tasks: 聊天ID到"正在输入"指示器任务的映射
    """
    
    name = "telegram"
    
    # 注册到 Telegram 命令菜单的命令列表（用户可以在输入框看到命令提示）
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),       # 启动机器人
        BotCommand("new", "Start a new conversation"),  # 开始新对话
        BotCommand("help", "Show available commands"),   # 显示帮助
    ]
    
    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
    ):
        """
        初始化 Telegram 渠道。

        参数:
            config: Telegram 配置（包含 bot token、代理设置等）
            bus: 消息总线实例
            groq_api_key: Groq API 密钥，用于语音消息转录
        """
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # 发送者ID → 聊天ID 映射表
        self._typing_tasks: dict[str, asyncio.Task] = {}  # 聊天ID → 输入指示器任务
    
    async def start(self) -> None:
        """
        启动 Telegram 机器人（长轮询模式）。

        启动流程：
        1. 构建 Application 实例并配置连接池
        2. 注册命令处理器和消息处理器
        3. 初始化并开始轮询
        4. 注册命令菜单到 Telegram
        5. 进入主循环等待消息
        """
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        
        self._running = True
        
        # 构建 HTTP 请求客户端，配置较大的连接池避免长时间运行时的池超时
        req = HTTPXRequest(connection_pool_size=16, pool_timeout=5.0, connect_timeout=30.0, read_timeout=30.0)
        builder = Application.builder().token(self.config.token).request(req).get_updates_request(req)
        # 如果配置了代理，同时为普通请求和轮询请求设置代理
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)
        
        # 注册命令处理器（类似 Spring MVC 的 @RequestMapping）
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("new", self._forward_command))
        self._app.add_handler(CommandHandler("help", self._forward_command))
        
        # 注册通用消息处理器：处理文本、图片、语音、音频、文档（排除命令消息）
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL) 
                & ~filters.COMMAND, 
                self._on_message
            )
        )
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # 初始化并启动 Application
        await self._app.initialize()
        await self._app.start()
        
        # 获取机器人信息并注册命令菜单
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")
        
        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning(f"Failed to register bot commands: {e}")
        
        # 开始长轮询（持续运行直到停止）
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True  # 启动时忽略积压的旧消息
        )
        
        # 主循环：保持运行直到 _running 被设为 False
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """
        停止 Telegram 机器人。

        按顺序清理：取消输入指示器 → 停止轮询 → 停止应用 → 释放资源。
        """
        self._running = False
        
        # 取消所有聊天的"正在输入"指示器
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 Telegram 发送消息。

        先尝试将 Markdown 转为 HTML 发送，如果 HTML 解析失败
        则回退为纯文本发送（容错机制）。

        参数:
            msg: 出站消息对象，包含目标 chat_id 和内容
        """
        if not self._app:
            logger.warning("Telegram bot not running")
            return
        
        # 发送前停止该聊天的"正在输入"指示器
        self._stop_typing(msg.chat_id)
        
        try:
            chat_id = int(msg.chat_id)  # Telegram 的 chat_id 是整数
            # 将 Markdown 转换为 Telegram 兼容的 HTML
            html_content = _markdown_to_telegram_html(msg.content)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=html_content,
                parse_mode="HTML"
            )
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
        except Exception as e:
            # HTML 解析失败时回退为纯文本发送
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                await self._app.bot.send_message(
                    chat_id=int(msg.chat_id),
                    text=msg.content
                )
            except Exception as e2:
                logger.error(f"Error sending Telegram message: {e2}")
    
    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理 /start 命令 - 向新用户发送欢迎消息。

        参数:
            update: Telegram 更新对象
            context: 回调上下文
        """
        if not update.message or not update.effective_user:
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )
    
    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        转发斜杠命令到消息总线，由 AgentLoop 统一处理。

        /new 和 /help 等命令不在 Telegram 层面处理，而是作为普通消息
        转发给 Agent，由 Agent 根据命令内容执行相应逻辑。

        参数:
            update: Telegram 更新对象
            context: 回调上下文
        """
        if not update.message or not update.effective_user:
            return
        await self._handle_message(
            sender_id=str(update.effective_user.id),
            chat_id=str(update.message.chat_id),
            content=update.message.text,
        )
    
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理所有非命令消息（文本、图片、语音、文档等）。

        这是 Telegram 渠道最核心的消息处理方法，处理流程：
        1. 提取发送者信息和聊天ID
        2. 解析文本内容和媒体附件
        3. 下载媒体文件到本地
        4. 语音/音频消息自动转录为文字（STT）
        5. 启动"正在输入"指示器
        6. 转发到消息总线

        参数:
            update: Telegram 更新对象
            context: 回调上下文
        """
        if not update.message or not update.effective_user:
            return
        
        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        
        # 使用数字 ID 作为主标识，同时附带用户名（用于白名单兼容）
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        
        # 缓存 chat_id 用于后续回复（sender_id → chat_id 映射）
        self._chat_ids[sender_id] = chat_id
        
        # ===== 构建消息内容 =====
        content_parts = []
        media_paths = []
        
        # 提取文本内容
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)  # 媒体消息的标题文字
        
        # ===== 检测媒体类型 =====
        media_file = None
        media_type = None
        
        if message.photo:
            media_file = message.photo[-1]  # 取最大尺寸的图片
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"
        
        # ===== 下载媒体文件 =====
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))
                
                # 保存到 ~/.nanobot/media/ 目录
                from pathlib import Path
                media_dir = Path.home() / ".nanobot" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                
                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))
                
                media_paths.append(str(file_path))
                
                # 语音/音频消息：调用 Groq Whisper 进行语音转文字（STT）
                if media_type == "voice" or media_type == "audio":
                    from nanobot.providers.transcription import GroqTranscriptionProvider
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        # 将转录结果作为文本内容附加
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    # 非语音媒体：附加文件路径信息
                    content_parts.append(f"[{media_type}: {file_path}]")
                    
                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")
        
        # 合并所有内容部分
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")
        
        str_chat_id = str(chat_id)
        
        # 在处理消息前启动"正在输入"指示器，提升用户体验
        self._start_typing(str_chat_id)
        
        # 将消息转发到消息总线，由 AgentLoop 处理
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"  # 是否为群组消息
            }
        )
    
    def _start_typing(self, chat_id: str) -> None:
        """
        启动"正在输入..."状态指示器。

        创建一个异步任务，每隔4秒向 Telegram 发送一次 typing action，
        让用户看到机器人正在处理消息。

        参数:
            chat_id: 聊天 ID
        """
        self._stop_typing(chat_id)  # 先取消已有的指示器
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))
    
    def _stop_typing(self, chat_id: str) -> None:
        """
        停止"正在输入..."状态指示器。

        参数:
            chat_id: 聊天 ID
        """
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
    
    async def _typing_loop(self, chat_id: str) -> None:
        """
        持续发送"正在输入"动作的循环（每4秒一次）。

        Telegram 的 typing 状态会在5秒后自动消失，
        因此每4秒发送一次以保持连续显示。

        参数:
            chat_id: 聊天 ID
        """
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)  # Telegram typing 状态5秒后消失，4秒间隔确保连续
        except asyncio.CancelledError:
            pass  # 正常取消，不记录日志
        except Exception as e:
            logger.debug(f"Typing indicator stopped for {chat_id}: {e}")
    
    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        全局错误处理器 - 记录轮询/处理器中的异常。

        避免异常被静默吞掉导致难以排查问题。

        参数:
            update: 触发错误的更新对象
            context: 错误上下文，包含异常信息
        """
        logger.error(f"Telegram error: {context.error}")

    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """
        根据媒体类型和 MIME 类型推断文件扩展名。

        优先使用 MIME 类型映射，回退到媒体类型默认扩展名。

        参数:
            media_type: 媒体类型（image/voice/audio/file）
            mime_type: MIME 类型字符串（如 "image/jpeg"）

        返回:
            文件扩展名（如 ".jpg"、".ogg"）
        """
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        # MIME 类型不匹配时，按媒体类型给出默认扩展名
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")