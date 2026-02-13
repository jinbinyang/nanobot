"""
工具基类模块 (agent/tools/base.py)

模块职责：
    定义所有 Agent 工具的抽象基类 Tool。
    每个工具必须实现4个核心接口：name、description、parameters、execute。
    基类还提供了参数校验（validate_params）和 OpenAI Function Calling 格式转换（to_schema）的通用能力。

在架构中的位置：
    Tool 是工具系统的最底层抽象，所有具体工具（文件操作、Shell执行、Web搜索等）都继承自它。
    ToolRegistry 持有 Tool 实例的集合，Agent 循环通过 registry 间接调用 Tool。

设计模式对比（Java 视角）：
    相当于 Java 中的 interface + 模板方法模式：
    - Tool 相当于一个抽象接口（类似 Java 的 abstract class）
    - name/description/parameters 相当于抽象的 getter 方法
    - execute() 相当于核心业务方法
    - validate_params() 是模板方法，提供通用的 JSON Schema 校验逻辑
    - to_schema() 将工具转换为 OpenAI API 所需的函数描述格式

二开提示（语音多智能体）：
    自定义新工具时继承此类即可，例如：
    class TTSTool(Tool):
        name = "text_to_speech"
        ...
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    Agent 工具的抽象基类。

    所有工具必须实现以下抽象属性和方法：
      - name: 工具名称，LLM 在 function call 中使用此名称来调用工具
      - description: 工具功能描述，帮助 LLM 理解何时该调用此工具
      - parameters: JSON Schema 格式的参数定义，告诉 LLM 需要传入哪些参数
      - execute(): 实际执行工具逻辑的异步方法

    类比 Java：类似于定义了一个 Tool 接口 + AbstractTool 抽象类。
    """

    # JSON Schema 类型 -> Python 类型的映射表，用于参数校验
    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),  # number 类型同时接受整数和浮点数
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于 LLM function call 中的函数名标识。"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，LLM 据此判断何时调用该工具。"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema 定义，描述工具接受哪些输入参数。"""
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        执行工具的核心逻辑（异步方法）。

        参数:
            **kwargs: 工具特定的参数，由 LLM 生成并经过校验后传入。

        返回:
            str: 工具执行结果的文本表示，会回传给 LLM 作为后续推理的上下文。
        """
        pass

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """
        根据 JSON Schema 校验工具参数。

        参数:
            params: LLM 传入的参数字典

        返回:
            list[str]: 错误信息列表，空列表表示校验通过。

        类比 Java: 类似于 javax.validation 的参数校验机制。
        """
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        """
        递归校验单个值是否符合 JSON Schema。

        参数:
            val: 待校验的值
            schema: 该值对应的 JSON Schema 片段
            path: 当前字段路径（用于错误信息定位，如 "address.city"）

        返回:
            list[str]: 校验错误列表
        """
        t, label = schema.get("type"), path or "parameter"
        # 类型检查：根据 _TYPE_MAP 映射检查 Python 类型是否匹配
        if t in self._TYPE_MAP and not isinstance(val, self._TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors = []
        # 枚举值检查
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        # 数值范围检查
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        # 字符串长度检查
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        # 对象类型：递归校验每个属性，并检查必填字段
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {path + '.' + k if path else k}")
            for k, v in val.items():
                if k in props:
                    errors.extend(self._validate(v, props[k], path + '.' + k if path else k))
        # 数组类型：递归校验每个元素
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]"))
        return errors

    def to_schema(self) -> dict[str, Any]:
        """
        将工具转换为 OpenAI Function Calling 格式的 JSON Schema。

        返回值示例:
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the contents of a file",
                    "parameters": { ... JSON Schema ... }
                }
            }

        这个格式会传给 LLM API 的 tools 参数，告诉 LLM 可以调用哪些工具。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }