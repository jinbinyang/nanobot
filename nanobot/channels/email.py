"""
邮件渠道实现 - 基于 IMAP 轮询 + SMTP 回复。

本模块实现了邮件机器人的收发功能：
- 入站：通过 IMAP 协议轮询邮箱中的未读邮件
- 出站：通过 SMTP 协议将回复发送给原始发件人

特性：
- 支持 SSL/TLS 加密连接
- 自动解析邮件主题、正文（纯文本和 HTML）
- 支持按日期范围查询历史邮件（用于摘要任务）
- UID 去重机制防止重复处理
- 可配置的自动回复功能
- 线程化回复（通过 In-Reply-To / References 头部）
"""

import asyncio
import html
import imaplib
import re
import smtplib
import ssl
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import EmailConfig


class EmailChannel(BaseChannel):
    """
    邮件渠道实现。

    入站流程：
    - 定期轮询 IMAP 邮箱中的未读消息
    - 将每封邮件转换为入站事件发布到消息总线

    出站流程：
    - 通过 SMTP 将回复发送回原始发件人地址
    - 支持线程化回复（自动关联原始邮件）

    去重机制：
    - 使用 IMAP UID 集合防止重复处理
    - UID 集合上限为 100000，超出后清空重置
    """

    name = "email"  # 渠道标识名称

    # IMAP 日期格式所需的英文月份缩写
    _IMAP_MONTHS = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )

    def __init__(self, config: EmailConfig, bus: MessageBus):
        """
        初始化邮件渠道。

        参数:
            config: 邮件配置对象，包含 IMAP/SMTP 连接参数
            bus: 消息总线，用于发布和订阅消息事件
        """
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._last_subject_by_chat: dict[str, str] = {}  # 按发件人缓存最后邮件主题（用于回复线程）
        self._last_message_id_by_chat: dict[str, str] = {}  # 按发件人缓存最后 Message-ID（用于回复引用）
        self._processed_uids: set[str] = set()  # 已处理的邮件 UID 集合（去重用）
        self._MAX_PROCESSED_UIDS = 100000  # UID 集合上限，防止无限增长

    async def start(self) -> None:
        """
        启动 IMAP 邮件轮询。

        流程：
        1. 检查用户授权同意（consent_granted）
        2. 验证 IMAP/SMTP 配置完整性
        3. 进入轮询循环，定期检查新邮件
        4. 将新邮件通过 _handle_message 发布到消息总线
        """
        # 安全检查：必须获得用户明确授权
        if not self.config.consent_granted:
            logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return

        # 验证配置完整性
        if not self._validate_config():
            return

        self._running = True
        logger.info("Starting Email channel (IMAP polling mode)...")

        # 轮询间隔（最小 5 秒）
        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                # 在线程池中执行 IMAP 操作（阻塞 IO）
                inbound_items = await asyncio.to_thread(self._fetch_new_messages)
                for item in inbound_items:
                    sender = item["sender"]
                    subject = item.get("subject", "")
                    message_id = item.get("message_id", "")

                    # 缓存最后的主题和 Message-ID，用于后续回复线程
                    if subject:
                        self._last_subject_by_chat[sender] = subject
                    if message_id:
                        self._last_message_id_by_chat[sender] = message_id

                    # 发布到消息总线
                    await self._handle_message(
                        sender_id=sender,
                        chat_id=sender,
                        content=item["content"],
                        metadata=item.get("metadata", {}),
                    )
            except Exception as e:
                logger.error(f"Email polling error: {e}")

            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        """停止邮件轮询循环。"""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 SMTP 发送邮件回复。

        流程：
        1. 检查授权和自动回复配置
        2. 构建回复邮件（自动关联原始邮件线程）
        3. 通过 SMTP 发送

        参数:
            msg: 出站消息对象，chat_id 为收件人地址
        """
        # 安全检查：必须获得用户授权
        if not self.config.consent_granted:
            logger.warning("Skip email send: consent_granted is false")
            return

        # 检查是否允许自动回复（可通过 metadata.force_send 强制发送）
        force_send = bool((msg.metadata or {}).get("force_send"))
        if not self.config.auto_reply_enabled and not force_send:
            logger.info("Skip automatic email reply: auto_reply_enabled is false")
            return

        if not self.config.smtp_host:
            logger.warning("Email channel SMTP host not configured")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            logger.warning("Email channel missing recipient address")
            return

        # 构建回复主题：优先使用 metadata 中的自定义主题，否则自动添加 "Re:" 前缀
        base_subject = self._last_subject_by_chat.get(to_addr, "nanobot reply")
        subject = self._reply_subject(base_subject)
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        # 构建邮件对象
        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(msg.content or "")

        # 设置线程化回复头部（In-Reply-To / References）
        in_reply_to = self._last_message_id_by_chat.get(to_addr)
        if in_reply_to:
            email_msg["In-Reply-To"] = in_reply_to
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
        except Exception as e:
            logger.error(f"Error sending email to {to_addr}: {e}")
            raise

    def _validate_config(self) -> bool:
        """
        验证邮件配置的完整性。

        检查 IMAP 和 SMTP 的必要配置项是否都已设置。

        返回:
            配置完整返回 True，否则返回 False 并记录缺失项
        """
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if missing:
            logger.error(f"Email channel not configured, missing: {', '.join(missing)}")
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        """
        通过 SMTP 发送邮件（同步方法，在线程池中调用）。

        根据配置选择连接方式：
        - smtp_use_ssl: 使用 SMTP_SSL 直接建立加密连接（端口通常为 465）
        - smtp_use_tls: 先建立普通连接再通过 STARTTLS 升级（端口通常为 587）
        - 都不启用: 使用普通 SMTP 连接（不推荐）

        参数:
            msg: 已构建的 EmailMessage 对象
        """
        timeout = 30
        if self.config.smtp_use_ssl:
            # SSL 模式：直接建立加密连接
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        # 普通/TLS 模式
        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _fetch_new_messages(self) -> list[dict[str, Any]]:
        """
        轮询 IMAP 获取未读邮件。

        使用 UNSEEN 搜索条件获取所有未读邮件，
        并根据配置决定是否将邮件标记为已读。

        返回:
            解析后的邮件字典列表
        """
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=self.config.mark_seen,
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        按日期范围获取邮件。

        用于历史邮件摘要任务（如"昨天的邮件"）。
        使用 IMAP 的 SINCE/BEFORE 搜索条件，范围为 [start_date, end_date)。

        参数:
            start_date: 起始日期（包含）
            end_date: 结束日期（不包含）
            limit: 最大返回邮件数量，默认 20

        返回:
            解析后的邮件字典列表
        """
        if end_date <= start_date:
            return []

        return self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )

    def _fetch_messages(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        通用 IMAP 邮件获取方法。

        流程：
        1. 连接 IMAP 服务器（SSL 或普通）
        2. 选择邮箱文件夹
        3. 执行搜索条件
        4. 逐条获取并解析邮件
        5. 执行去重和已读标记

        参数:
            search_criteria: IMAP 搜索条件元组
            mark_seen: 是否将获取的邮件标记为已读
            dedupe: 是否启用 UID 去重
            limit: 最大返回数量（0 表示不限制）

        返回:
            解析后的邮件字典列表，每项包含 sender/subject/message_id/content/metadata
        """
        messages: list[dict[str, Any]] = []
        mailbox = self.config.imap_mailbox or "INBOX"

        # 根据配置选择 SSL 或普通 IMAP 连接
        if self.config.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            status, _ = client.select(mailbox)
            if status != "OK":
                return messages

            # 执行搜索
            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            # 如果设置了数量限制，只取最新的 N 封
            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]

            for imap_id in ids:
                # 使用 BODY.PEEK[] 获取完整邮件（不自动标记为已读）
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                # UID 去重检查
                uid = self._extract_uid(fetched)
                if dedupe and uid and uid in self._processed_uids:
                    continue

                # 解析邮件内容
                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))
                date_value = parsed.get("Date", "")
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                # 截断过长的正文
                body = body[: self.config.max_body_chars]
                # 格式化为标准消息内容
                content = (
                    f"Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n\n"
                    f"{body}"
                )

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                }
                messages.append(
                    {
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                        "content": content,
                        "metadata": metadata,
                    }
                )

                # 更新去重集合
                if dedupe and uid:
                    self._processed_uids.add(uid)
                    # mark_seen 是主要的去重手段；UID 集合是安全网
                    if len(self._processed_uids) > self._MAX_PROCESSED_UIDS:
                        self._processed_uids.clear()

                # 将邮件标记为已读
                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass

        return messages

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """
        将日期格式化为 IMAP 搜索所需的格式。

        IMAP 要求使用英文月份缩写，格式为 DD-Mon-YYYY。

        参数:
            value: 日期对象

        返回:
            IMAP 格式的日期字符串，如 "01-Jan-2024"
        """
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: list[Any]) -> bytes | None:
        """
        从 IMAP fetch 响应中提取原始邮件字节。

        参数:
            fetched: IMAP fetch 返回的数据列表

        返回:
            邮件的原始字节数据，或提取失败时返回 None
        """
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: list[Any]) -> str:
        """
        从 IMAP fetch 响应中提取邮件 UID。

        参数:
            fetched: IMAP fetch 返回的数据列表

        返回:
            邮件 UID 字符串，或提取失败时返回空字符串
        """
        for item in fetched:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                head = bytes(item[0]).decode("utf-8", errors="ignore")
                m = re.search(r"UID\s+(\d+)", head)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """
        解码邮件头部值（处理 RFC 2047 编码的非 ASCII 字符）。

        参数:
            value: 原始头部值

        返回:
            解码后的字符串
        """
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """
        尽力提取邮件的可读正文文本。

        处理策略：
        - 多部分邮件：优先提取 text/plain 部分，其次提取 text/html 并转为纯文本
        - 单部分邮件：直接提取内容，HTML 类型自动转换
        - 跳过附件部分

        参数:
            msg: 解析后的邮件对象

        返回:
            提取的纯文本正文
        """
        if msg.is_multipart():
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for part in msg.walk():
                # 跳过附件
                if part.get_content_disposition() == "attachment":
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    # 回退：手动解码 payload
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)
            # 优先返回纯文本，其次将 HTML 转为纯文本
            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        # 单部分邮件
        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            return ""
        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        """
        简易 HTML 转纯文本。

        处理规则：
        - <br> 标签转换为换行符
        - </p> 标签转换为换行符
        - 移除所有其他 HTML 标签
        - 反转义 HTML 实体

        参数:
            raw_html: 原始 HTML 字符串

        返回:
            转换后的纯文本
        """
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _reply_subject(self, base_subject: str) -> str:
        """
        构建回复邮件的主题。

        如果原始主题已以 "Re:" 开头则不重复添加。

        参数:
            base_subject: 原始邮件主题

        返回:
            带有回复前缀的主题字符串
        """
        subject = (base_subject or "").strip() or "nanobot reply"
        prefix = self.config.subject_prefix or "Re: "
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"