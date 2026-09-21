# 从 Hermes 移植

[English](../../en/development/porting-from-hermes.md) | **中文**

Mertina Agent 是 Nous Research 的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
的精简重建版，上游以 MIT 许可发布。本文档长期记录我们从上游取了什么，以及取用时遵守的规则。
规则背后的推理见[v0.1 Agent Core 移植设计](../design/2026-09-20-agent-core-loop.md)。

## 上游

| | |
|---|---|
| 仓库 | https://github.com/NousResearch/hermes-agent |
| 许可 | MIT |
| 已移植至 | `fbc4ea8b96c784c9bb28dd27541c14514bba302d`（2026-09-20） |

「已移植至」指当前代码读取自哪个上游 commit。每次从更新的 commit 移植代码后，更新这一行。

我们不持续跟进上游。代码是在一个明确的 commit 上有意识地拷贝过来的，此后作为我们自己的代码维护。

## 规则

### 1. 路径和文件名与上游一致，统一置于 `mertina` 包下

所有可导入的代码都在唯一的顶层包 `mertina` 下面，所以移植文件的路径就是上游路径前加 `mertina/`：

| Hermes | Mertina |
|---|---|
| `agent/turn_tool_round.py` | `mertina/agent/turn_tool_round.py` |
| `agent/transports/base.py` | `mertina/agent/transports/base.py` |
| `tools/registry.py` | `mertina/tools/registry.py` |
| `run_agent.py` | `mertina/run_agent.py` |
| `hermes_state.py` | `mertina/state.py` |
| `hermes_cli/config.py` | `mertina/cli/config.py` |

`hermes_` 前缀是剥掉而不是替换：包名已经提供了命名空间，`mertina/mertina_state.py` 会结巴。

因为映射是机械的，不需要逐文件的映射表——一个已移植文件的来源就是它自己的路径。

**包目录的 `__init__.py` 也要搬。** 搬一个文件时，它所在每一级包目录的上游 `__init__.py` 一起搬过来。`mertina/` 下任何包目录，只要上游有对应的 `__init__.py`，就不能手写占位；`scripts/port_check.py` 会把这种情况报为失败。上游没有对应物的只有 `mertina/__init__.py` 本身。

我们自己写的、上游没有对应物的文件，按自身合适的方式组织，不编造对应关系。

### 2. 语义逐字，并注明来源

「逐字拷贝」指**语义逐字**：行为和结构不变，形式服从本仓库的规范。允许的改动只有四类：

1. 按规则 1 改写 import 根：`from agent.` → `from mertina.agent.`，`hermes_cli` → `mertina.cli`，`import hermes_bootstrap` → `from mertina import bootstrap`。**写在字符串里的模块路径也算**，例如 `_forward("agent.agent_runtime_helpers", ...)`、`importlib.import_module(f"agent.transports.{name}")`
2. `ruff check --fix` 和 `ruff format` 的机械改写（`Dict` → `dict`、重新换行等）
3. 为通过 mypy strict 补全类型标注（如 `**kwargs` → `**kwargs: Any`），以及把超长的 docstring 和注释折行，措辞不变
4. 为保留上游写法而加的 `# noqa` 或 `# type: ignore`，同一行写明原因（例如保留上游的参数名 `id`，或上游把两个签名不同的函数绑在同一个名字上）

删减范围外的功能不属于拷贝：它放在紧随其后的单独 commit 里，让那次 diff 只包含被删掉的东西。

每个移植文件开头用两行注明来源：

```python
# Ported from hermes-agent agent/transports/base.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
```

读懂后重写、而不是拷贝的文件，把第一行的 `Ported from` 换成 `Derived from`。

上游的 `.py` 文件本身没有版权头，版权声明只在上游根目录的 `LICENSE` 里。本仓库的 `LICENSE`
同时列出了 Nous Research 和我们的版权行，满足 MIT 要求的「副本附带版权和许可声明」。

### 3. 保留抽象，不保留实现

上游的某个接口有多个实现时（模型 transport、搜索 provider），移植接口，只实现我们需要的那一个。

### 4. 不继承 accretion

Hermes 仓库根有 28 个 `hermes_state_*.py`，`agent/` 一个平铺目录下有 31 个 `turn_*.py`。
这是就地拆分文件的产物，那个共同前缀是一个从未被创建出来的目录。

我们不复制这些拆分痕迹。移植 `hermes_state.py` 得到一个 `mertina/state.py`；
将来确实需要拆分，就拆成 `state/` 包，并在下面记录这条偏离。

### 5. 边界外（L2）的文件：只搬用到的部分

L2 平台层不整体拷贝。循环确实调用到某个 L2 函数时，把这个函数（连同它在同一文件里调用的辅助函数）逐字搬到**上游的同名路径**，而不是塞进调用方的文件，也不自己重写。这样以后从同一个文件再搬别的函数，只需要往里加。这类文件：

- 文件头多一行 `# Partial: only the parts ported so far. Upstream order is kept.`
- 搬过来的部分保持上游顺序
- 模块头只保留搬过来的部分用到的 import，`ruff --fix` 会去掉其余的
- 同样先逐字搬、再单独提交删减

例如 `agent/agent_runtime_helpers.py` 目前只有 `_ra()` 和 `create_openai_client`，`agent/process_bootstrap.py` 只有延迟加载的 OpenAI 代理，`run_agent.py` 在步骤 7 之前只有模块 logger。

## 偏离记录

Mertina 的结构或行为与上游不同的地方，记录下来以免被误认为是无意的漂移。单纯删掉范围外的功能不算偏离，那些在删减 commit 里可以看到。

| 上游 | Mertina | 原因 |
|---|---|---|
| `chat_completions.py`：`reasoning_details` 只回传给 OpenRouter 和 Nous | 发送时一律剥掉；响应照常解析，历史照常保存 | 按路由回传属于 provider 特判。代价是 OpenRouter 的推理链路在多轮之间不连续 |
| `chat_completions.py`：把整数和大写的 `finish_reason` 归一化 | 原样使用，为空时补 `"stop"` | 归一化针对 Poolside 和部分 Gemini 网关，属于 provider 特判 |
| `chat_completions.py`：`_STRIP_MSG_KEYS` 有 9 个键 | 保留 5 个持久化用的键，删掉 codex / anthropic / bedrock 的 4 个 | 那 4 个只在会话中途从对应 transport 切换过来时出现；持久化键要等步骤 6 确认循环是否写入 |
| `client_lifecycle.py`：`_is_openai_client_closed` 把 `unittest.mock.Mock` 视为未关闭 | 去掉这个特例 | 这是上游为自己的测试写进生产代码的 |

## 与上游对照

每个移植 commit 提交前，在本仓库旁边放一份 Hermes 检出，运行：

```bash
uv run python scripts/port_check.py
```

它按每个移植文件头里记录的 SHA 读取上游文件（`git show <sha>:<path>`，不受检出所在分支影响），先做规则 2 的第 1、2 类改动，再逐行比较：

- 列出所有不在上游里的行。删减不会产生这种行，所以每一行都应能归到来源头、第 3、4 类改动，或删减带来的连带改写
- 保留下来的定义不按上游顺序、import 根没改写（包括字符串里的模块路径）、包目录的 `__init__.py` 没搬时，报失败并以非零状态退出

注意我们的副本是刻意更小的：当前里程碑范围外的特性是有意删除的，diff 很大是预期结果，不是要修复的问题。
