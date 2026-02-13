"""
nanobot 模块入口点 - 支持通过 `python -m nanobot` 方式启动

模块概述：
    当用户执行 `python -m nanobot` 时，Python 会自动查找并执行此文件。
    本文件仅作为启动跳板，实际的 CLI 命令逻辑定义在 cli/commands.py 中。

    启动链路：
    python -m nanobot → __main__.py → cli/commands.py 中的 Typer app
    
    对于 Java 开发者的类比：
    这相当于 Spring Boot 中的 main() 方法入口，
    但 Python 使用 __main__.py 这种约定来实现模块级可执行功能。
"""

# 从 CLI 模块导入 Typer 应用实例（app 是 Typer 框架创建的命令行应用对象）
from nanobot.cli.commands import app

# 标准 Python 入口点守卫：仅在直接运行时执行，被导入时不执行
if __name__ == "__main__":
    app()
