# 编码规范

[English](../../en/development/coding-style.md) | **中文**

目标是写出任何贡献者都能看懂、能安全修改的代码。下面的大部分规则由工具强制执行，
所以与其死记规则，不如在推送前把工具跑一遍。

## 工具

| 工具 | 用途 | 命令 |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python 版本、虚拟环境、依赖 | `uv sync`、`uv add`、`uv lock` |
| [Ruff](https://docs.astral.sh/ruff/) | 代码检查和格式化 | `uv run ruff check .`、`uv run ruff format .` |
| [mypy](https://mypy.readthedocs.io/) | 静态类型检查 | `uv run mypy mertina` |
| [pytest](https://docs.pytest.org/) | 测试 | `uv run pytest` |
| [pre-commit](https://pre-commit.com/) | 每次提交前自动运行检查 | `uv run pre-commit install` |

所有工具的配置都放在 `pyproject.toml` 里。除非某个工具没有其他配置方式，否则不要额外添加
`setup.py`、`setup.cfg`、`requirements.txt` 或工具专用的配置文件。

## 项目结构

```
mertina-agent/
├── pyproject.toml        # 项目元数据、依赖、工具配置
├── uv.lock               # 锁定的依赖版本（需要提交）
├── mertina/              # 唯一的顶层 import 包
│   ├── __init__.py
│   └── ...
├── tests/                # 测试，目录结构与包对应
├── docs/                 # 文档
└── .github/              # CI 工作流、模板、CODEOWNERS
```

- 顶层 import 包**有且只有一个**：`mertina`。所有可导入的代码都在它下面，
  因此不会与第三方包在通用名字上撞车
- 包名（import 用）是 `mertina`，仓库名和发布名是 `mertina-agent`
- 不使用 `src/` 目录：本项目作为应用发布而非库，从 checkout 或 Docker 镜像运行，
  不需要安装（`[tool.uv] package = false`）

## Python 版本

- 最低支持的 Python 版本以 `pyproject.toml` 中的 `requires-python` 为准
- 使用该版本支持的现代语法：用 `list[str]` 而不是 `List[str]`，用 `X | None` 而不是 `Optional[X]`
- CI 会同时测试最低版本和最新支持的版本

## 格式

- 格式以 `ruff format` 的输出为准，不要和它较劲
- 行宽：100
- 字符串使用双引号
- import 顺序由 Ruff 排序：标准库、第三方库、项目自身

## 命名

| 对象 | 风格 | 示例 |
|---|---|---|
| 模块、包 | `snake_case`，尽量简短 | `session_store.py` |
| 函数、变量 | `snake_case` | `load_session()` |
| 类、类型别名 | `PascalCase` | `SessionStore` |
| 常量 | `UPPER_SNAKE_CASE` | `DEFAULT_TIMEOUT_S` |
| 私有成员 | 下划线开头 | `_parse_header()` |

- 用清晰的命名，而不是用注释去解释含糊的命名
- 需要时在名字里带上单位：`timeout_s`、`max_size_bytes`
- 避免缩写，常见的缩写除外（`id`、`url`、`http`、`db`）

## 类型标注

- 所有公共函数、方法和类属性都要有类型标注
- mypy 必须通过。添加 `# type: ignore` 时必须写明错误码和原因：
  `# type: ignore[import-untyped]  # library ships no stubs`
- 优先使用精确的类型（`Literal`、`TypedDict`、dataclass、Pydantic 模型），而不是 `dict[str, Any]`
- 公共接口中避免使用 `Any`

## 文档字符串和注释

- 公共模块、类和函数要写 **Google 风格**的文档字符串
- 文档字符串说明函数做什么，以及不明显的行为（会抛出的异常、副作用），不要重复类型标注
- 注释解释*为什么*，而不是*做了什么*。注释掉的代码直接删掉，git 会记住它
- 代码、注释、文档字符串和日志信息都使用英文

```python
def load_session(session_id: str, *, timeout_s: float = 5.0) -> Session:
    """Load a session from the configured store.

    Raises:
        SessionNotFoundError: If no session with this ID exists.
        StorageTimeoutError: If the store does not respond within ``timeout_s``.
    """
```

## 错误处理

- 抛出具体的异常。项目自定义的异常集中定义在一处，并继承自同一个基类
- 永远不要使用裸的 `except:`。只在最外层（请求处理器、后台任务）捕获 `Exception`，并在那里记录日志
- 不要静默吞掉异常。捕获了就要处理、重新抛出，或者带上下文记录日志
- 重新抛出时保留异常链：`raise StorageError("...") from exc`

## 日志

- 使用标准库 `logging`，每个模块定义自己的 logger：`logger = logging.getLogger(__name__)`
- 库和服务代码中不要使用 `print()`
- 选择合适的级别：`DEBUG` 用于调试诊断，`INFO` 用于重要事件，`WARNING` 用于可恢复的问题，
  `ERROR` 用于需要关注的失败
- **绝不记录密钥、token、包含用户数据的完整提示词或个人数据**
- 在高频路径上使用惰性格式化（`logger.info("loaded %s", session_id)`）或结构化字段，不要用 f-string

## 配置

- 配置来自**环境变量**（遵循 12-factor 原则），并为本地开发提供安全的默认值
- 启动时在一个地方统一读取和校验配置（例如使用 `pydantic-settings`），配置无效就立即报错退出
- 环境变量统一使用 `MERTINA_` 前缀：`MERTINA_LOG_LEVEL`、`MERTINA_DATABASE_URL`
- 每个配置项都要有文档说明，`.env.example` 列出所有配置项并使用占位值

## 异步与 I/O

- 不要在异步代码中混入阻塞 I/O。使用异步客户端，或者把阻塞调用放到线程池中执行
- 每个网络调用都必须设置超时
- 重试要使用指数退避并设置上限，并且只对可以安全重试的操作进行重试

## 跨平台

服务在生产环境中运行在 Linux 上，但贡献者会在 Windows、macOS 和 Linux 上开发。
在本地运行的代码，包括 CLI、测试和脚本，必须在这三个平台上都能工作。

- 路径使用 `pathlib.Path`，不要用 `/` 或 `\` 拼接字符串来构造路径
- 打开文本文件时显式指定编码：`open(path, encoding="utf-8")`
- 不要假设存在 POSIX shell、`/tmp` 或仅限 Unix 的模块和信号。使用 `tempfile` 和 `shutil`
- 辅助脚本尽量用 Python 编写（`uv run python scripts/...`），而不是 shell 脚本
- 如果某个测试确实依赖特定操作系统，用 `pytest.mark.skipif` 标记并写明原因

## 依赖

- 使用 `uv add <包名>`（运行时依赖）或 `uv add --group dev <包名>`（仅开发依赖）添加，并提交 `uv.lock`
- 添加依赖前，确认它仍在积极维护、被广泛使用、许可证与 MIT 兼容
- 为了一个小功能，优先使用标准库或已有的依赖，而不是引入新依赖
- `pyproject.toml` 中每个直接依赖都锁死到精确版本（`httpx==0.28.1`），版本的抬升是一次有意的改动。
  声明区间意味着 PyPI 上的新版本不经我们 review 就能进入用户的安装；`uv.lock` 只堵住了在本仓库里
  开发的人这一侧，而本项目是从 checkout 运行的（`package = false`），不是到处都带着 lock 文件
- 每次移动某个 pin，都用 `uv lock` 重新生成 `uv.lock`，保持传递依赖的解析一致
- 新增依赖的 PR 要在描述中说明原因
