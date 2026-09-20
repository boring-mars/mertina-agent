# v0.1 Agent Core — 从 Hermes 移植的设计

[English](../../en/design/2026-09-20-agent-core-loop.md) | **中文**

**状态**：已确认，待实现
**对应 Roadmap 里程碑**：[v0.1 — Agent core](../../../ROADMAP.md#v01--agent-core)
**分支**：`feat/agent-core-loop`
**上游**：[Hermes Agent](https://github.com/NousResearch/hermes-agent)（MIT）。勘察基于本地检出的上游快照，具体 commit SHA 记录在 port manifest 中

---

## 1. 背景

Roadmap 给的原则是「先抄，再裁」（copy, then cut）。实施前对 Hermes 源码做了量化勘察，结论是这条原则需要分层适用：对接口叶子文件成立，对核心循环不成立。本文档记录勘察数据、由此确定的拷贝边界，以及 v0.1a 的实现方案。

### 1.1 勘察数据

Hermes 全仓 6,796 个 Python 文件 / 1,980,894 行。

从 Roadmap v0.1 参考表点名的 14 个文件出发做传递闭包：

| 口径 | 文件数 | 行数 |
|---|---:|---:|
| 只拷点名的 14 个文件 | 14 | 15,600 |
| 模块级 import 闭包（import 时必须存在） | 200 | 105,871 |
| 含函数内延迟 import（跑到那条路径才需要） | 1,281 | 568,183 |

两种 import 必须分开看。`plugins/platforms/telegram/adapter.py`、`hermes_cli/kanban_db.py`、`cron/scheduler.py`、`tui_gateway/server.py` 之所以出现在闭包里，全部是经由函数体内的延迟 import，走的是本里程碑不需要的代码路径：

```
tools/registry → agent.secret_scope → gateway.config_loader
              → gateway.platforms._shared → tools.send_message_senders → plugins.platforms.telegram.adapter
model_tools    → agent.delegation_context → hermes_cli.kanban_db        （子代理，P1）
tool_executor  → hermes_cli.config → cron.jobs → cron.scheduler          （cron，v0.5）
run_agent      → tools.delegate_tool → tools.delegate_tool_registry → tui_gateway.server
```

因此拷贝边界是可以切的，不存在「必须拷 56 万行」这回事。

### 1.2 为什么核心循环不能整文件照抄

- `AIAgent`（`run_agent.py`，1,592 行）由 14 个 mixin 拼装，`__init__` 约 70 个参数，实体逻辑转发给 `agent/agent_init.py`（2,406 行）。`agent_init` 在模块级不可达，只在 `__init__` 里延迟 import——拷了 `run_agent.py` 能 import 成功，但实例化即失败。
- `agent/conversation_loop.py`（1,745 行）本身是调度壳，真正的逻辑在 14 个 `agent/turn_*.py`（约 5,000 行）里。
- 对 9 个主文件做函数粒度实测（函数体内出现本里程碑范围外的特性即整函数判为可删），保留率 45%：

| 文件 | 总行 | 函数数 | 范围外函数 | 第一刀后剩 |
|---|---:|---:|---:|---:|
| `run_agent.py` | 1,592 | 88 | 54 | 708 |
| `agent/conversation_loop.py` | 1,745 | 58 | 45 | 594 |
| `agent/tool_executor.py` | 1,849 | 80 | 51 | 550 |
| `agent/prompt_builder.py` | 1,767 | 65 | 17 | 1,438 |
| `tools/registry.py` | 1,007 | 70 | 21 | 558 |
| `model_tools.py` | 987 | 48 | 30 | 343 |
| `agent/system_prompt.py` | 817 | 41 | 26 | 305 |
| `agent/transports/chat_completions.py` | 672 | 32 | 23 | 178 |
| `tools/web_tools.py` | 549 | 24 | 12 | 279 |
| **合计** | **10,985** | | | **4,953（45%）** |

第一刀之后还有级联删除（留下的代码大量调用已删函数）、摊平 mixin、瘦身提示词文本三笔要扣，预计 v0.1 最终产物 **2,000–3,000 行**。

---

## 2. 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 拷贝边界定在 L0+L1，L2 平台层不拷 | 见 §3 |
| D2 | 先全拷进 `vendor/`，再逐模块裁剪搬出 | 主线代码任何时刻都能跑，「不可运行期」只存在于 vendor 目录内 |
| D3 | `vendor/` 不提交 git，改为提交 port manifest | 提交会带来 1.5 万行 diff；MIT 署名靠 manifest + 每文件版权头即可满足 |
| D4 | 目录采用根级扁平布局，与 Hermes 文件名和 import 根一致 | 见 §5 |
| D5 | 不发布 wheel，作为独立产品从 checkout / Docker 运行 | D4 的前提，且项目定位本就不是被当作库依赖 |
| D6 | v0.1 拆成 a/b/c 三刀，本分支只做 v0.1a | 见 §8 |
| D7 | v0.1a 用 `get_time` 占位工具跑通工具路径，`web_search` 推迟到 v0.1c | 让 v0.1a 的测试完全不依赖网络 |

---

## 3. 拷贝边界

| 层 | 内容 | 行数 | 处理 |
|---|---|---:|---|
| **L0** 接口叶子 | `agent/transports/base.py`、`agent/transports/types.py`、`agent/iteration_budget.py`、`agent/retry_utils.py`、`agent/web_search_provider.py`、`tools/interrupt.py` | 601 | **逐字抄**，保留 MIT 版权头 |
| **L1** 循环骨架 | `conversation_loop.py` + 14 个 `turn_*.py` + `tool_executor.py` + `registry.py` + `chat_completions.py` + `model_tools.py` + `AIAgent` 的 mixin | ~15,000 | **拷进 vendor 后裁剪**，这是需要读懂的部分 |
| **L2** 平台层 | `agent_runtime_helpers.py`(3,509)、`model_metadata.py`(2,551)、`turn_recovery.py`(1,813)、`error_classifier.py`(1,394)、`redact.py`(1,335)、`display.py`(1,118)、`hermes_constants.py`(1,515)、`hermes_logging.py`(764) 等 | ~36,000 | **不拷**，需要什么自己写什么 |

L0/L1/L2 划分的是**拷贝边界**（哪些文件进 vendor、哪些不进），与 §8 的里程碑划分是两个维度。L0 的 6 个文件都在边界内，但落地时间不同：`transports/base.py`、`transports/types.py`、`iteration_budget.py` 在 v0.1a，`retry_utils.py`、`tools/interrupt.py` 在 v0.1b，`web_search_provider.py` 在 v0.1c。

L2 不是 agent loop，是模型元数据表、错误分类、脱敏、终端渲染、全局常量。举例：openai 客户端的构造在 `agent_runtime_helpers.create_openai_client` 里，Mertina 自己写约 30 行即可。

保留 L1 中 `turn_*` 的职责拆分（而不是合并成一个大循环文件），是因为 v0.2–v0.5 往回加功能（压缩、memory 注入、审批）时需要有地方放。

---

## 4. vendor 流程

```
vendor/hermes/        ← L0+L1 原样拷贝；不参与构建、不参与 lint、不提交 git
agent/ tools/ ...     ← 裁剪后搬出的代码，始终可运行
```

搬运顺序，每步跑通再进下一步：

1. L0 叶子（逐字抄）
2. `agent/transports/`
3. `tools/registry.py` + `model_tools.py`
4. `agent/tool_executor.py` + 工具分发
5. `agent/conversation_loop.py` + `agent/turn_*.py`
6. `run_agent.py`（摊平 mixin）

最后整个删除 `vendor/`。

`.gitignore` 增加 `vendor/`。取而代之提交 `docs/hermes-port-manifest.md`，记录：上游 commit SHA、拷贝的文件清单、每个文件的处置（逐字抄 / 裁剪重写 / 丢弃）、以及所有偏离上游的结构性决定（见 §5.2）。

---

## 5. 目录对齐规则

### 5.1 规则

Hermes 采用根级扁平布局：`agent/`、`tools/`、`gateway/`、`cron/`、`hermes_cli/` 直接位于仓库根，加根级单文件模块（`run_agent.py`、`model_tools.py`、`utils.py`、`hermes_state*.py`）。其 `setup.py` 主动禁止构建 wheel/sdist（Nix 构建除外），因为运行时资源依赖源码 checkout 的布局解析——这也是它敢占用 `agent`、`tools` 这类通用顶层包名的原因。

Mertina 采用同样形态，规则两条：

1. **路径与文件名原样保留**：`agent/turn_tool_round.py` ↔ `agent/turn_tool_round.py`
2. **`hermes_*` 前缀机械替换为 `mertina_*`**：`hermes_state.py` → `mertina_state.py`，`hermes_cli/` → `mertina_cli/`

这样 `diff hermes-agent/agent/turn_tool_round.py mertina-agent/agent/turn_tool_round.py` 得到的是纯语义差异，import 语句两边一字不差。

对齐的价值分布并不均匀，记录在此以备后续权衡：

| 对齐什么 | 收益 | 成本 |
|---|---|---|
| 文件名 / 模块名 | 高（移植时同名文件直接可 diff） | 几乎为零 |
| import 根（`agent.` 而非 `mertina_agent.agent.`） | 高（import 行在文件开头，不对齐则每次 diff 都是噪音） | 放弃 pip 安装 |
| 目录嵌套深度 | 近乎为零（Hermes 本就是平的，无结构可继承） | — |
| accretion 模式 | 负（是债务不是资产） | — |

### 5.2 不继承 accretion

Hermes 根级 45 个 `.py` 中有 28 个是 `hermes_state_*.py`；`agent/` 下 238 个 `.py` 只有 10 个子目录，其中 31 个是 `turn_*.py`、8 个 `auxiliary_*.py`、7 个 `context_*.py`。这是就地拆分的产物——文件长到装不下就拆成兄弟文件，那个前缀就是没有被创建出来的目录。

**Mertina 不预先复制这些拆分痕迹。** 拷 `hermes_state.py` 就是一个 `mertina_state.py`；将来真的需要拆，拆成 `state/` 包，并在 port manifest 里记一条偏离（如 `hermes_state_*.py (28) → state/`）。

Mertina 自己写的、上游没有对应物的文件，按自身合适的方式组织，不编造对应关系。

### 5.3 与 README 的冲突

README 第 76–89 行「Planned repository layout」写的是 `src/mertina_agent/` 布局，与 D4 冲突。本分支一并修改该章节。

---

## 6. 依赖

对 v0.1 拷贝边界内的文件做第三方 import 扫描（含延迟 import）：

| 包 | 出现位置 | v0.1 |
|---|---|---|
| `openai` | 不在边界内（客户端构造在 L2 的 `agent_runtime_helpers.py`） | **v0.1a 需要**，自己写约 30 行构造 |
| `fire` | `run_agent.py` 的 CLI 入口 | 不需要，改用标准库 `argparse` |
| `httpx` | `tools/web_tools.py` | openai SDK 自带，不单列 |
| `ddgs` | `tools/web_tools.py` | v0.1c（keyless search provider） |
| `pyyaml` | `hermes_cli/config.py` | v0.1c（配置文件） |

Hermes 核心依赖 36 个（`rich`、`tenacity`、`pydantic`、`fastapi`、`Pillow`、`croniter`、`PyJWT` 等），**v0.1a 一个都不需要，只依赖 `openai`**。注意 `tenacity` 在 Hermes 的重试路径上也没被使用——`retry_utils.py` 是手写退避。

Python 版本跟随 Hermes：`>=3.11`。

---

## 7. v0.1a 设计

### 7.1 模块清单

| 路径 | 来源 | 说明 |
|---|---|---|
| `agent/transports/base.py` | L0 逐字抄 | `ProviderTransport` ABC |
| `agent/transports/types.py` | L0 逐字抄 | `ToolCall` / `Usage` / `NormalizedResponse`，裁掉 codex/bedrock/anthropic 的 `provider_data` 兼容属性 |
| `agent/transports/__init__.py` | L1 裁剪 | transport 注册表，只注册 `chat_completions` |
| `agent/transports/chat_completions.py` | L1 裁剪 | 保留 sanitize → build_kwargs → normalize_response 三步，去掉各家特判 |
| `agent/iteration_budget.py` | L0 逐字抄 | 去掉 `normalize_budget_warning_ratio` |
| `agent/conversation_loop.py` | L1 裁剪 | `run_conversation()` 入口与 turn 调度 |
| `agent/turn_api_request.py` | L1 裁剪 | 请求组装 |
| `agent/turn_response_intake.py` | L1 裁剪 | 响应归一化 |
| `agent/turn_tool_round.py` | L1 裁剪 | 一轮工具调用 |
| `agent/turn_finalizer.py` | L1 裁剪 | 收尾 |
| `agent/tool_executor.py` | L1 裁剪 | 并行执行独立工具调用，去掉审批网关/中间件/checkpoint/心跳 |
| `tools/registry.py` | L1 裁剪 | 保留 `ToolEntry` 形状与 `register()`，去掉插件作用域、发现缓存、`check_fn` 缓存 |
| `tools/time_tools.py` | 新写 | `get_time` 占位工具 |
| `model_tools.py` | L1 裁剪 | 工具定义收集与分发，去掉 toolset 选择、hook、bridge |
| `run_agent.py` | L1 裁剪 | `AIAgent` 摊平 14 个 mixin，`__init__` 参数收敛到本里程碑所需 |

### 7.2 数据流

```
user_message
  → 组装 messages（system prompt：identity + tools + time）
  → transport.build_kwargs()      → OpenAI 兼容 endpoint
  → transport.normalize_response() → NormalizedResponse
  → finish_reason == "tool_calls" ?
       是 → registry 查找 → 并行执行独立调用 → append tool results → 回到模型调用
       否 → 返回最终文本
  每轮循环前 IterationBudget.consume()，耗尽则以「预算用尽」收尾
```

两个入口，与 Roadmap 一致：`chat()` 返回最终文本；`run_conversation()` 返回 messages、metadata 与 usage。

### 7.3 错误、中断、预算

- **重试**：429 / 5xx 走 `jittered_backoff`，尊重 `Retry-After` 响应头，重试上限可配。完整错误分类与 fallback 留给 P1。
- **中断**：stop flag 在每次模型调用前检查。停止时，已发出但未完成的 tool_call 补一条 `"interrupted"` 结果，保证 history 对模型 API 合法。
- **预算**：每轮 `IterationBudget.consume()`。

v0.1a 只做预算和基本的 stop flag；重试与流式中断在 v0.1b。

### 7.4 测试

全部使用 fake model client，不发起网络请求：

- 纯文本回答（无工具调用）
- 单个工具调用后回答
- 并行工具调用（多个独立 tool_call 在一轮内）
- 迭代预算耗尽时的收尾
- turn 中途 stop，且停止后对话可继续（history 合法）

`get_time` 工具本身不依赖网络，工具路径可完整覆盖。

### 7.5 验收标准

- 一个脚本能用 fake client 跑完整循环，包含工具调用往返
- 上述 5 项单元测试通过
- `vendor/` 已从工作树删除，`docs/hermes-port-manifest.md` 记录了全部拷贝来源
- README 的布局章节已更新为实际布局

---

## 8. 分刀

| | 内容 | 产出 |
|---|---|---|
| **v0.1a**（本分支） | L0 叶子 + transports + registry + loop + `get_time` | fake client 下跑通完整循环 |
| v0.1b | 流式输出 + 重试 + 中断（`tools/interrupt.py`、`agent/prompt_builder.py`、`agent/system_prompt.py`） | Roadmap 的 stop 语义达标 |
| v0.1c | `web_search` + `WebSearchProvider` 接口 + 配置层（`mertina_cli/config.py`） | 真实 endpoint 跑通 Roadmap 验收场景 |

v0.1c 完成时，Roadmap v0.1 的三条「Done when」全部满足。

---

## 9. 遗留问题

- **L2 的替代实现深度未定**：错误分类、脱敏、日志在 v0.1a 用最简实现，P1 再评估要不要回头参考 Hermes 的对应模块。
- **`agent`/`tools` 顶层包名的撞名风险**：在 venv 中理论上可能与同名第三方包冲突。Hermes 长期承受此风险。若将来确有冲突，回退方案是加 `src/mertina_agent/` 前缀（D4 的备选），代价是所有 import 行与上游产生固定差异。
- **上游跟进机制未定**：port manifest 记录了上游 SHA，但「如何发现上游某个已移植文件发生了变更」还没有工具支持。候选方案是一个 `scripts/diff-hermes.sh`，留待 v0.1c 之后评估。
