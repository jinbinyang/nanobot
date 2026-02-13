"""
网页工具模块 (agent/tools/web.py)

模块职责：
    提供两个网页相关工具，赋予 Agent 访问互联网信息的能力：
      - WebSearchTool: 通过 Brave Search API 进行网页搜索
      - WebFetchTool: 抓取并解析网页内容（HTML → 可读文本/Markdown）

在架构中的位置：
    这两个工具是 Agent 获取实时信息的关键通道。当 Agent 需要查询最新信息
    （天气、新闻、API 文档等）时，先用 web_search 找到相关网页，
    再用 web_fetch 获取具体内容。

技术选型：
    - 搜索引擎：Brave Search API（隐私友好的搜索引擎，需要 API Key）
    - HTTP 客户端：httpx（Python 现代异步 HTTP 库，类似 Java 的 OkHttp）
    - 内容提取：readability-lxml（Mozilla Readability 的 Python 实现）
    - 输出格式：支持 Markdown 和纯文本两种提取模式

安全设计：
    - URL 校验：只允许 http/https 协议，防止 file:// 等协议泄露本地文件
    - 重定向限制：最多 5 次重定向，防止重定向环攻击
    - 超时保护：搜索10秒、抓取30秒超时
    - 输出截断：最大 50000 字符，防止 LLM 上下文溢出

设计模式对比（Java 视角）：
    WebSearchTool 类似于 RestTemplate 调用第三方搜索 API；
    WebFetchTool 类似于 Jsoup + Readability 的组合（HTML解析+正文提取）。

二开提示（语音多智能体）：
    可基于 web_search + web_fetch 构建一个"信息检索 Agent"，
    作为多智能体系统中的专属搜索助手。
"""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool

# 共享常量
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"  # 模拟浏览器请求头
MAX_REDIRECTS = 5  # 最大重定向次数，防止重定向环 DoS 攻击


def _strip_tags(text: str) -> str:
    """
    去除 HTML 标签并解码 HTML 实体。

    处理顺序：先移除 script/style 标签及其内容，再移除所有其他标签，最后解码实体。

    参数:
        text: 包含 HTML 标签的原始文本

    返回:
        str: 纯文本内容
    """
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)  # 移除 script 块
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)    # 移除 style 块
    text = re.sub(r'<[^>]+>', '', text)  # 移除所有 HTML 标签
    return html.unescape(text).strip()   # 解码 HTML 实体（如 &amp; → &）


def _normalize(text: str) -> str:
    """
    规范化空白字符。

    将连续空格/制表符压缩为单个空格，连续3个以上换行压缩为双换行。

    参数:
        text: 需要规范化的文本

    返回:
        str: 规范化后的文本
    """
    text = re.sub(r'[ \t]+', ' ', text)      # 压缩水平空白
    return re.sub(r'\n{3,}', '\n\n', text).strip()  # 压缩多余换行


def _validate_url(url: str) -> tuple[bool, str]:
    """
    校验 URL 安全性。

    只允许 http 和 https 协议，且必须有有效的域名。
    防止 file://、ftp:// 等协议被利用来访问本地或内网资源。

    参数:
        url: 待校验的 URL 字符串

    返回:
        tuple[bool, str]: (是否合法, 错误信息)
    """
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """
    网页搜索工具，使用 Brave Search API 进行互联网搜索。

    返回搜索结果的标题、URL 和摘要片段。
    需要配置 BRAVE_API_KEY 环境变量。

    类比 Java: 类似于封装了 Brave Search REST API 的 HttpClient 调用。
    """

    # 直接作为类属性定义（而非 @property），因为值是固定的
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5):
        """
        初始化搜索工具。

        参数:
            api_key: Brave Search API 密钥，None 时从环境变量 BRAVE_API_KEY 读取
            max_results: 默认返回结果数量
        """
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        """
        执行网页搜索。

        参数:
            query: 搜索关键词
            count: 返回结果数量（1-10，默认5）

        返回:
            str: 格式化的搜索结果列表（序号 + 标题 + URL + 描述）
        """
        if not self.api_key:
            return "Error: BRAVE_API_KEY not configured"

        try:
            # 限制结果数量在 1-10 之间
            n = min(max(count or self.max_results, 1), 10)
            # 使用 httpx 异步 HTTP 客户端调用 Brave Search API
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0  # 10秒超时
                )
                r.raise_for_status()  # 非 2xx 状态码抛异常

            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"

            # 格式化搜索结果为可读文本
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """
    网页内容抓取工具，获取 URL 内容并提取可读正文。

    使用 Mozilla Readability 算法从复杂的 HTML 页面中提取核心内容，
    支持输出为 Markdown 或纯文本格式。

    类比 Java: 类似于 Jsoup.parse() + Readability 的组合。
    """

    # 类属性定义（固定值）
    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000):
        """
        初始化抓取工具。

        参数:
            max_chars: 返回内容的最大字符数，超出则截断
        """
        self.max_chars = max_chars

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        """
        抓取并解析网页内容。

        参数:
            url: 目标 URL
            extractMode: 提取模式，"markdown" 保留格式，"text" 纯文本
            maxChars: 最大返回字符数

        返回:
            str: JSON 格式的结果，包含 url、status、text 等字段

        处理流程:
            1. URL 安全校验，防止 file:// 等非法协议
            2. HTTP GET 请求（跟随重定向）
            3. 根据 Content-Type 选择解析策略：
               - JSON → 直接格式化
               - HTML → Readability 提取正文
               - 其他 → 返回原始文本
            4. 截断超长内容
        """
        from readability import Document  # 延迟导入，仅在需要时加载

        max_chars = maxChars or self.max_chars

        # 第一步：URL 安全校验，防止 file:// 等非法协议
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        try:
            # 第二步：发起 HTTP 请求
            async with httpx.AsyncClient(
                follow_redirects=True,       # 自动跟随重定向
                max_redirects=MAX_REDIRECTS,  # 限制重定向次数
                timeout=30.0                  # 30秒超时
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            # 第三步：根据内容类型选择解析策略
            if "application/json" in ctype:
                # JSON 响应：直接格式化
                text, extractor = json.dumps(r.json(), indent=2), "json"
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                # HTML 响应：使用 Readability 提取正文
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                # 其他类型：返回原始文本
                text, extractor = r.text, "raw"

            # 第四步：截断超长内容
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text})
        except Exception as e:
            return json.dumps({"error": str(e), "url": url})

    def _to_markdown(self, html: str) -> str:
        """
        将 HTML 片段转换为简化版 Markdown。

        转换规则：
        - <a href="url">text</a> → [text](url)
        - <h1>~<h6> → # ~ ######
        - <li> → - 列表项
        - <p>/<div> → 段落分隔
        - <br>/<hr> → 换行

        参数:
            html: HTML 片段字符串

        返回:
            str: Markdown 格式文本
        """
        # 链接转换：<a href="url">text</a> → [text](url)
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        # 标题转换：<h1>text</h1> → # text
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        # 列表转换：<li>text</li> → - text
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        # 块级元素结束 → 段落分隔
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        # 换行元素 → 换行符
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        # 去除剩余 HTML 标签并规范化空白
        return _normalize(_strip_tags(text))