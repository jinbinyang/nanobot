"""
工具注册表模块 (agent/tools/registry.py)

模块职责：
    管理所有 Agent 可用工具的注册表（Registry）。
    提供工具的注册、注销、查找、执行等核心能力。
    是 Agent 循环与具体工具实现之间的中间层。

在架构中的位置：
    Agent 核心循环 (agent/loop.py) 持有一个 ToolRegistry 实例：
    1. 启动时，将所有内置工具注册到 registry
    2. 构建 LLM 请求时，调用 get_definitions() 获取所有工具的 JSON Schema
    3. LLM 返回 tool_calls 时，调用 execute(name, params) 执行对应工具

设计模式对比（Java 视角）：
    类似于 Spring 中的 BeanFactory / ServiceLocator 模式：
    - register() 相当于注册一个 Bean
    - get() 相当于 getBean()
    - execute() 相当于查找 Bean 并调用其方法
    区别是这里是运行时动态注册，而非编译时注入。

二开提示（语音多智能体）：
    扩展新工具只需：tool = MyNewTool(); registry.register(tool)
"""

from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Agent 工具注册表。

    负责管理所有工具实例的生命周期，提供按名称查找和执行工具的能力。
    内部使用 dict[str, Tool] 存储，以工具名称为键。

    类比 Java: 类似于一个轻量级的 ServiceRegistry<Tool>。
    """

    def __init__(self):
        # 工具存储字典：{工具名称 -> 工具实例}
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """
        注册一个工具到注册表。

        参数:
            tool: 工具实例，其 name 属性作为注册键

        注意: 如果同名工具已存在，会被新工具覆盖（后注册的优先）。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """
        按名称注销一个工具。

        参数:
            name: 工具名称。如果不存在则静默忽略（不抛异常）。
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """
        按名称获取工具实例。

        参数:
            name: 工具名称

        返回:
            Tool | None: 工具实例，未找到返回 None
        """
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """检查指定名称的工具是否已注册。"""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """
        获取所有已注册工具的 OpenAI Function Calling 格式定义。

        返回:
            list[dict]: 工具定义列表，每个元素是 Tool.to_schema() 的输出。
            此列表直接传给 LLM API 的 tools 参数，告知 LLM 可调用的工具清单。
        """
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        按名称执行工具。

        这是 Agent 循环调用工具的核心入口：
        1. 根据名称查找工具
        2. 校验参数合法性
        3. 调用工具的 execute() 方法
        4. 捕获所有异常，确保不会因工具错误导致 Agent 循环崩溃

        参数:
            name: 工具名称（由 LLM 返回的 function name）
            params: 工具参数（由 LLM 返回的 function arguments）

        返回:
            str: 执行结果文本，会回传给 LLM 作为后续推理的上下文
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        try:
            # 先校验参数，避免传入非法参数导致工具内部崩溃
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            return await tool.execute(**params)
        except Exception as e:
            # 捕获所有异常，返回错误信息而非抛出，保护 Agent 循环的稳定性
            return f"Error executing {name}: {str(e)}"

    @property
    def tool_names(self) -> list[str]:
        """获取所有已注册工具的名称列表。"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        """返回已注册工具的数量，支持 len(registry)。"""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """支持 'tool_name' in registry 语法检查工具是否存在。"""
        return name in self._tools