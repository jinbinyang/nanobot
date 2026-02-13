"""
会话管理模块 - 管理用户对话的会话状态和历史记录持久化。

本模块提供了会话（Session）的完整生命周期管理，包括创建、加载、保存、删除和列表。
每个会话对应一个用户在某个渠道中的对话窗口，会话数据以 JSONL 格式存储在磁盘上。

【架构定位】
会话管理器位于 Agent 循环和消息总线之间：
- 消息总线接收到入站消息后，通过 session_key (channel:chat_id) 定位会话
- Agent 循环从会话中获取历史消息构建上下文
- Agent 回复后，新消息追加到会话并持久化

【Java 开发者类比】
- SessionManager 类似于 Spring Session 的 SessionRepository
- Session 类似于 HttpSession，存储对话上下文
- JSONL 持久化类似于 JDBC + 文件型数据库（每行一个 JSON 对象）
- _cache 字典类似于 Spring Cache 的一级缓存

【二开提示】
在多 Agent 系统中，每个领域 Agent 可以有独立的会话，
管家 Agent 维护一个"路由会话"记录用户的意图分发历史。
"""

from nanobot.session.manager import SessionManager, Session

__all__ = ["SessionManager", "Session"]
