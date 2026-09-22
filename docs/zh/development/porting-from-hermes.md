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

1. 按规则 1 改写 import 根：`from agent.` → `from mertina.agent.`，`hermes_cli` → `mertina.cli`，`import hermes_bootstrap` → `from mertina import bootstrap`。**写在字符串里的模块路径也算**，例如 `_forward("agent.agent_runtime_helpers", ...)`、`importlib.import_module(f"agent.transports.{name}")`、`logging.getLogger("run_agent")`
2. `ruff check --fix` 和 `ruff format` 的机械改写（`Dict` → `dict`、重新换行等）
3. 为通过 mypy strict 补全类型标注（如 `**kwargs` → `**kwargs: Any`），以及把超长的 docstring 和注释折行，措辞不变
4. 为保留上游写法而加的 `# noqa` 或 `# type: ignore`，同一行写明原因（例如保留上游的参数名 `id`，或上游把两个签名不同的函数绑在同一个名字上）

删减范围外的功能不属于拷贝：它放在紧随其后的单独 commit 里，让那次 diff 只包含被删掉的东西。

删减 commit 以删除为主：删掉整行、整块，或一行中属于范围外功能的那一段（行内删减），剩下的原样保留。为了精简，允许少量改写，例如把依赖已删功能的表达式换成直接取值；**每一处改写都要在下面的偏离记录里有一行**。提交前运行 `uv run python scripts/port_check.py --cut-from <逐字搬运 commit>`，它把删减后不是整行保留的内容分成三类：`prose`（注释和 docstring）、`inline`（行内删减）、`rewrite`（改写）。前两类不用登记；除去第 3、4 类改动，每一条 `rewrite` 都必须能在偏离记录里找到。

docstring 和注释是文字说明：不做 import 改写；删减之后可以按实际代码改写，让描述和代码一致，不算偏离。删掉一段代码时，只描述这段代码的注释随它一起删掉。

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

删减 commit 里的每一处改写（`port_check --cut-from` 报为 `rewrite` 的行）记录在这里，以免被误认为是无意的漂移。单纯删除、行内删减、注释和 docstring 的改写，以及规则 2 的第 1–4 类改动都不在这里记录。

| 上游 | Mertina | 原因 |
|---|---|---|
| `chat_completions._apply_max_tokens`：依次尝试 `ephemeral_max_output_tokens` 和 `max_tokens` | 只取 `max_tokens`：`candidate = params.get("max_tokens")` | `ephemeral_max_output_tokens` 是上游内部任务（标题生成、恢复）的预算，v0.1 没有这些任务 |
| `chat_completions.convert_messages`：`reasoning_details` 只回传给 OpenRouter / Nous 路由和声明了原生类型的 profile | 发送时一律剥掉：`strip_reasoning_details = True`；响应照常解析，历史照常保存 | **行为改变。** 按路由回传属于 provider 特判，保留它还要搬 `utils.base_url_host_matches`。代价是 OpenRouter 的推理链路不跨轮 |
| `chat_completions.build_kwargs`：末尾经 `_finish_kwargs` 计算 `prompt_cache_key` 后返回 | 直接 `return api_kwargs` | prompt 缓存路由依赖 Codex transport |
| `chat_completions.normalize_response`：`finish_reason` 经 `normalize_finish_reason` 折叠整数和大写取值 | 原样保留 `_fr = ...`，下一行改为 `finish_reason = _fr or "stop"` | **行为改变。** 折叠针对 Poolside 和部分 Gemini 网关；保留它还要搬 `message_sanitization` |
| `chat_completions.validate_response`：最后 `return not is_router_timeout_shim(response)` | `return True` | **行为改变：** 不再识别「HTTP 200 + 超时提示」的路由器伪装响应。保留它要多留 4 个定义 |
| `registry.ToolRegistry.register`：`target = self._slot(scope, create=True)`，按 profile 作用域选注册表 | `target = self._tools` | 插件和 profile 作用域不在 v0.1 内，所有工具都注册到全局表 |
| `model_tools._compute_tool_definitions`：`tools_to_include = _select_tool_names(enabled_toolsets, disabled_toolsets, quiet_mode)`，按 toolset 选择 | `tools_to_include = set(registry.get_all_tool_names())` | **行为改变：** 不再按 toolset 启用或禁用，所有已注册工具都发给模型。toolset 选择依赖 `toolsets.py` 的静态表，v0.1 只有 `get_time` 一个工具 |
| `tool_executor._run_agent_tool_execution_middleware`：`state.result, _relay_args = relay_tools.execute(function_name, function_args, _hermes_pipeline, ...)`，经 Relay 和工具中间件后派发 | `state.result = _authorized_dispatch(function_args)` | Relay 和工具中间件整层不在 v0.1 内，直接派发一次 |
| `tool_executor.execute_tool_calls_sequential`：按终端审批分段，每段以 `SimpleNamespace(tool_calls=calls)` 调用 `_execute_tool_calls_sequential` | 传入 `assistant_message`，调用一次 | 终端审批批次不在 v0.1 内 |
| `turn_api_call.perform_api_call`：`response = run_llm_execution_middleware(api_kwargs, _perform_api_call, ...)`，内层经流式调用或 `relay_llm.execute(..., agent._interruptible_api_call, ...)` 发出请求 | `response = agent._interruptible_api_call(api_kwargs)` | LLM 执行中间件和 Relay 整层不在 v0.1 内，流式输出在 v0.1.1；直接发一次非流式请求 |
| `turn_context.build_api_messages`：`for idx, msg in enumerate(canonical_messages)`，遍历经 `canonicalize_replay_history` 规范化过的历史前缀 | `for msg in messages:` | 回放规范化服务于会话恢复和 prompt 缓存，v0.1 没有这两项；`idx` 只给已删除的空消息填充用 |
| `chat_completion_helpers._chat_summary_attempt`：`response = _managed_summary_call(agent, api_request_id, summary_kwargs, lambda request: summary_client.chat.completions.create(...), ...)`，经 Relay 发出总结请求 | `response = summary_client.chat.completions.create(**summary_kwargs)` | Relay 整层不在 v0.1 内，与 `perform_api_call` 同理 |

## 与上游对照

每个移植 commit 提交前，在本仓库旁边放一份 Hermes 检出，运行：

```bash
uv run python scripts/port_check.py
```

它按每个移植文件头里记录的 SHA 读取上游文件（`git show <sha>:<path>`，不受检出所在分支影响），先做规则 2 的第 1、2 类改动，再逐行比较：

- 列出所有不在上游里的行。删减不会产生这种行，所以每一行都应能归到来源头、第 3、4 类改动，或偏离记录里的一行
- 保留下来的定义不按上游顺序、import 根没改写（包括字符串里的模块路径）、包目录的 `__init__.py` 没搬时，报失败并以非零状态退出

加上 `--cut-from <逐字搬运 commit>` 时，它还会把删减后不是整行保留的内容分成 `prose`、`inline`、`rewrite` 三类列出（见规则 2）。

它按顺序匹配：每一行保留下来的代码，都去上一行匹配位置之后找第一处相同的行。如果某一行在上游更后面也出现（例如 `if agent._interrupt_requested:`、`assistant_message,`），匹配位置会跳过去，之后几十行都被报成 `rewrite`。遇到一长串连续的 `rewrite` 时，先把可疑的函数单独取出来分类核对，再决定哪些要登记。同样，拼接出来的模块名（例如 `".".join(("tools", *parts))`）脚本认不出来，要人工检查。

注意我们的副本是刻意更小的：当前里程碑范围外的特性是有意删除的，diff 很大是预期结果，不是要修复的问题。
