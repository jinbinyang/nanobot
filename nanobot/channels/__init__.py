"""
消息渠道模块 - 实现多平台即时通讯渠道的接入与管理。

本模块采用插件式架构，通过统一的 BaseChannel 抽象基类定义渠道接口，
各平台渠道（Telegram、Discord、WhatsApp、飞书、钉钉、Slack、Email、QQ、Mochat）
分别实现该接口，由 ChannelManager 统一管理。

【架构定位】
渠道层是 nanobot 的"感官系统"——负责接收外部消息并将 Agent 的回复发送出去。
渠道通过消息总线（MessageBus）与 Agent 核心解耦，实现了"渠道无关"的对话处理。

消息流向：
  用户消息 → 渠道 → MessageBus → Agent 处理 → MessageBus → 渠道 → 用户

【Java 开发者类比】
- BaseChannel 相当于 Java 接口（Interface），定义了 start/stop/send 方法契约
- 各渠道实现类相当于 Interface 的不同实现（如 TelegramChannel implements BaseChannel）
- ChannelManager 相当于 Spring 的 ApplicationContext，管理所有渠道 Bean 的生命周期

【二开提示】
要添加语音交互渠道，可以参照现有渠道的模式创建 VoiceChannel：
1. 继承 BaseChannel
2. 在 start() 中启动语音输入监听（如 WebSocket 接收音频流）
3. 在 send() 中调用 TTS 引擎将文本转为语音输出
4. 在 ChannelManager._init_channels() 中注册新渠道
"""

from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
