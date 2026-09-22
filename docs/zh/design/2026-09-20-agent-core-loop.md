# v0.1 Agent Core — 从 Hermes 移植的设计

[English](../../en/design/2026-09-20-agent-core-loop.md) | **中文**

**状态**：已确认，待实现
**对应 Roadmap 里程碑**：[v0.1 — Agent core](../../../ROADMAP.md#v01--agent-core)
**分支**：`feat/agent-core-loop`
**上游**：[Hermes Agent](https://github.com/NousResearch/hermes-agent)（MIT）。勘察基于本地检出的上游快照，具体 commit SHA 记录在[从 Hermes 移植](../development/porting-from-hermes.md)中

---

## 1. 背景

Roadmap 给的原则是「先抄，再裁」（copy, then cut）。实施前对 Hermes 源码做了量化勘察，结论是这条原则需要分层适用：对接口叶子文件成立，对核心循环不成立。本文档记录勘察数据、由此确定的拷贝边界，以及 v0.1.0 的实现方案。

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

- `AIAgent`（`run_agent.py`，1,609 行）由 14 个 mixin 拼装，`__init__` 约 70 个参数，实体逻辑转发给 `agent/agent_init.py`（2,466 行）。`agent_init` 在模块级不可达，只在 `__init__` 里延迟 import——拷了 `run_agent.py` 能 import 成功，但实例化即失败。
- `agent/conversation_loop.py`（1,745 行）本身是调度壳，真正的逻辑在 15 个 `agent/turn_*.py`（5,444 行）里。
- 对 9 个主文件做函数粒度实测（函数体内出现本里程碑范围外的特性即整函数判为可删），保留率 45%：

| 文件 | 总行 | 函数数 | 范围外函数 | 第一刀后剩 |
|---|---:|---:|---:|---:|
| `run_agent.py` | 1,609 | 88 | 54 | 708 |
| `agent/conversation_loop.py` | 1,745 | 58 | 45 | 594 |
| `agent/tool_executor.py` | 1,856 | 80 | 51 | 550 |
| `agent/prompt_builder.py` | 1,767 | 65 | 17 | 1,438 |
| `tools/registry.py` | 1,012 | 70 | 21 | 558 |
| `model_tools.py` | 987 | 48 | 30 | 343 |
| `agent/system_prompt.py` | 817 | 41 | 26 | 305 |
| `agent/transports/chat_completions.py` | 692 | 32 | 23 | 178 |
| `tools/web_tools.py` | 569 | 24 | 12 | 279 |
| **合计** | **11,054** | | | **4,953（45%）** |

第一刀之后还有级联删除（留下的代码大量调用已删函数）、摊平 mixin、瘦身提示词文本三笔要扣，预计 v0.1 最终产物 **2,000–3,000 行**。

---

## 2. 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 拷贝边界定在 L0+L1；L2 平台层不整体拷，用到的函数按需搬到同名文件 | 见 §3 |
| D2 | 先全拷进 `vendor/`，再逐模块裁剪搬出 | 主线代码任何时刻都能跑，「不可运行期」只存在于 vendor 目录内 |
| D3 | `vendor/` 不提交 git | 提交会带来 2.5 万行 diff；MIT 署名靠每文件版权头 + 开发规范中的[从 Hermes 移植](../development/porting-from-hermes.md)即可满足 |
| D4 | 扁平布局（不用 `src/`），顶层只有一个具名包 `mertina`，其内部严格镜像 Hermes | 见 §5 |
| D5 | 不发布 wheel，作为独立产品从 checkout / Docker 运行（`[tool.uv] package = false`） | 项目定位是应用不是库；也是不用 `src/` 布局的理由 |
| D6 | v0.1 拆成 0.1.0 / 0.1.1 / 0.1.2 三刀，本分支只做 v0.1.0 | 见 §8 |
| D7 | v0.1.0 用 `get_time` 占位工具跑通工具路径，`web_search` 推迟到 v0.1.2 | 让 v0.1.0 的测试完全不依赖网络 |

---

## 3. 拷贝边界

| 层 | 内容 | 行数 | 处理 |
|---|---|---:|---|
| **L0** 接口叶子 | `agent/transports/base.py`、`agent/transports/types.py`、`agent/iteration_budget.py`、`agent/retry_utils.py`、`agent/web_search_provider.py`、`tools/interrupt.py` | 601 | **语义逐字抄**（定义见[从 Hermes 移植](../development/porting-from-hermes.md)规则 2），加来源头；范围外的功能在紧随其后的单独 commit 里删除，各文件删什么见 §7.1 |
| **L1** 循环骨架 | `conversation_loop.py` + 15 个 `turn_*.py` + `tool_executor.py` + `agent_init.py` + `registry.py` + `chat_completions.py` + `model_tools.py` + `AIAgent` 的 14 个 mixin + `hermes_cli/config.py` + `plugins/web/` 的 ddgs provider 等，共 46 个文件 | 28,234 | **拷进 vendor 后裁剪**，这是需要读懂的部分 |
| **L2** 平台层 | `agent_runtime_helpers.py`(3,509)、`model_metadata.py`(2,551)、`turn_recovery.py`(1,813)、`error_classifier.py`(1,394)、`redact.py`(1,335)、`display.py`(1,118)、`hermes_constants.py`(1,515)、`hermes_logging.py`(764) 等 | ~36,000 | **不整体拷**；循环用到某个函数时，只把它逐字搬到同名文件（见[从 Hermes 移植](../development/porting-from-hermes.md)规则 5） |

L0/L1/L2 划分的是**拷贝边界**（哪些文件进 vendor、哪些不进），与 §8 的里程碑划分是两个维度。L0 的 6 个文件都在边界内，但落地时间不同：`transports/base.py`、`transports/types.py`、`iteration_budget.py` 在 v0.1.0，`retry_utils.py`、`tools/interrupt.py` 在 v0.1.1，`web_search_provider.py` 在 v0.1.2。

L2 不是 agent loop，是模型元数据表、错误分类、脱敏、终端渲染、全局常量。举例：openai 客户端的构造在 `agent_runtime_helpers.create_openai_client` 里，v0.1.0 只把这一个函数（以及它用到的延迟加载代理 `process_bootstrap.OpenAI`）搬到同名文件，这两个文件其余的几千行不动。

保留 L1 中 `turn_*` 的职责拆分（而不是合并成一个大循环文件），是因为 v0.2–v0.5 往回加功能（压缩、memory 注入、审批）时需要有地方放。

---

## 4. vendor 流程

```
vendor/hermes/        ← L0+L1 原样拷贝；不参与构建、不参与 lint、不提交 git
mertina/              ← 裁剪后搬出的代码，始终可运行（agent/ tools/ 等子包在其下）
```

搬运顺序，每步跑通再进下一步：

1. L0 叶子（语义逐字抄，删减另起一个 commit）
2. `agent/transports/`
3. `tools/registry.py` + `model_tools.py`
4. `agent/tool_executor.py` + 工具分发
5. `agent/conversation_loop.py` + `agent/turn_*.py`
6. `run_agent.py`（摊平 mixin）

最后整个删除 `vendor/`。

`.gitignore` 增加 `vendor/`。上游 SHA、命名规则和偏离记录写在开发规范的[从 Hermes 移植](../development/porting-from-hermes.md)里——那是一份长期文档。

本次搬运的逐文件进度清单是一次性的，放在 `vendor/PORT_CHECKLIST.md`（同样不进 git），随 `vendor/` 一起删除。因为 §5.1 的命名规则保证路径 1:1 对应，长期并不需要逐文件映射表。

---

## 5. 目录对齐规则

### 5.1 规则

Hermes 采用根级扁平布局：`agent/`、`tools/`、`gateway/`、`cron/`、`hermes_cli/` 直接位于仓库根，加根级单文件模块（`run_agent.py`、`model_tools.py`、`utils.py`、`hermes_state*.py`）。其 `setup.py` 主动禁止构建 wheel/sdist（Nix 构建除外），因为运行时资源依赖源码 checkout 的布局解析——这也是它敢占用 `agent`、`tools` 这类通用顶层包名的原因。

**Mertina 不照搬这一点。** 调研了同类项目后（见下），顶层只放一个具名包：

```
mertina-agent/            仓库名、分发名
├── mertina/              唯一的顶层 import 包
│   ├── agent/            ↔ hermes agent/
│   ├── tools/            ↔ hermes tools/
│   ├── run_agent.py      ↔ hermes run_agent.py
│   └── model_tools.py    ↔ hermes model_tools.py
├── tests/unit/           镜像 mertina/
└── pyproject.toml
```

映射规则三条：

1. **上游路径前加 `mertina/`**：`agent/turn_tool_round.py` → `mertina/agent/turn_tool_round.py`
2. **文件名不变**
3. **`hermes_` 前缀剥掉**（不是替换成 `mertina_`，包名已经提供命名空间）：`hermes_state.py` → `mertina/state.py`，`hermes_cli/` → `mertina/cli/`

对照时用一个 `sed` 过滤抹平 import 根的差异，剩下的就是纯语义差异：

```bash
diff <(sed 's/^from \(agent\|tools\)\./from mertina.\1./' ../hermes-agent/agent/turn_tool_round.py) \
     mertina/agent/turn_tool_round.py
```

#### 为什么不照搬 Hermes 的顶层布局

「扁平 vs src」和「顶层是一个具名包 vs 多个通用包」是两个独立的问题。Hermes 的特别之处在后者。

同类项目的做法（全部是扁平 + 单一具名顶层包）：

| 项目 | 形态 | 顶层 import 包 |
|---|---|---|
| Home Assistant（90.8k★） | 长期运行的服务，与 Mertina 最接近 | `homeassistant/` |
| Aider（49.1k★） | AI agent CLI 应用 | `aider/`（分发名是 `aider-chat`） |
| SWE-agent（20.4k★） | AI agent | `sweagent/` |
| Django | 框架 | `django/` |
| Flask / black / pip | 库 | `src/<name>/` |

SWE-agent 根目录也有 `tools/`，但那是工具资源目录、不是 import 包——和 PyPA
[src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
文档里 flat layout 示例的形态一致（示例中根目录只有一个具名 import 包，`tools/` 用来放脚本）。

而 `agent` 和 `tools` 在 PyPI 上都是**真实存在的包**（`agent 0.1.3`、`tools 1.0.35`），都提供同名顶层模块。
占用这两个名字的撞名风险是现实的，不是理论的。

不用 `src/` 的理由：src layout 的收益（防止误导入开发副本、强制使用已安装副本）是为要分发的库设计的。
按 D5 我们不发包、用 `[tool.uv] package = false`，没有安装步骤，src layout 反而使代码不加 path hack
就无法导入。上表中的三个应用类项目也都不用 `src/`。

包名取 `mertina` 而非 `mertina_agent`：import 包名短于分发名是常态（`aider-chat` → `aider`、
`scikit-learn` → `sklearn`、`Pillow` → `PIL`）；更重要的是内部结构镜像 Hermes 后第一层必然是 `agent/`，
`mertina_agent.agent.conversation_loop` 会结巴，而且这个包后续还要装下 gateway、state、cron。

对齐的价值分布并不均匀，记录在此以备后续权衡：

| 对齐什么 | 收益 | 成本 |
|---|---|---|
| 文件名 / 模块名 | 高（移植时同名文件直接可 diff） | 几乎为零 |
| import 根 | 中（可用一行 `sed` 抹平，不是结构性成本） | 若为此占用通用顶层名，则要承担撞名风险并放弃打包 |
| 目录嵌套深度 | 近乎为零（Hermes 本就是平的，无结构可继承） | — |
| accretion 模式 | 负（是债务不是资产） | — |

### 5.2 不继承 accretion

Hermes 根级 45 个 `.py` 中有 28 个是 `hermes_state_*.py`；`agent/` 下 238 个 `.py` 只有 10 个子目录，其中 31 个是 `turn_*.py`、8 个 `auxiliary_*.py`、7 个 `context_*.py`。这是就地拆分的产物——文件长到装不下就拆成兄弟文件，那个前缀就是没有被创建出来的目录。

**Mertina 不预先复制这些拆分痕迹。** 拷 `hermes_state.py` 就是一个 `mertina/state.py`；将来真的需要拆，拆成 `mertina/state/` 包，并在[从 Hermes 移植](../development/porting-from-hermes.md)的偏离表里记一条（如 `hermes_state_*.py (28) → state/`）。

Mertina 自己写的、上游没有对应物的文件，按自身合适的方式组织，不编造对应关系。

### 5.3 需要同步修改的既有文档

以下文档写于本决定之前，假设了 `src/mertina_agent/` 布局，本分支一并修正：

| 文档 | 原内容 |
|---|---|
| `README.md` | 「Planned repository layout」章节的 `src/mertina_agent/` |
| `coding-style.md` | 项目结构树、`mypy src`、「使用 src 布局」、包名 `mertina_agent`、`uv build` |
| `testing.md` | `--cov=mertina_agent`、`tests/unit/` 镜像 `src/mertina_agent/` |
| `versioning-and-release.md` | 公共接口定义中的 `mertina_agent` |

---

## 6. 依赖

对 v0.1 拷贝边界内的文件做第三方 import 扫描（含延迟 import）：

| 包 | 出现位置 | v0.1 |
|---|---|---|
| `openai` | 客户端构造在 L2 的 `agent_runtime_helpers.create_openai_client`，按需部分搬运 | **v0.1.0 需要** |
| `fire` | `run_agent.py` 的 CLI 入口 | 不需要，改用标准库 `argparse` |
| `httpx` | `tools/web_tools.py` | openai SDK 自带，不单列 |
| `ddgs` | `tools/web_tools.py` | v0.1.2（keyless search provider） |
| `pyyaml` | `hermes_cli/config.py` | v0.1.2（配置文件） |

Hermes 核心依赖 36 个（`rich`、`tenacity`、`pydantic`、`fastapi`、`Pillow`、`croniter`、`PyJWT` 等），**v0.1.0 一个都不需要，只依赖 `openai`**。注意 `tenacity` 在 Hermes 的重试路径上也没被使用——`retry_utils.py` 是手写退避。

Python 版本跟随 Hermes：`>=3.11`。

---

## 7. v0.1.0 设计

### 7.1 模块清单

| 路径 | 来源 | 说明 |
|---|---|---|
| `mertina/agent/transports/base.py` | L0 语义逐字抄 | `ProviderTransport` ABC，无删减 |
| `mertina/agent/transports/types.py` | L0 语义逐字抄 + 删减 | `ToolCall` / `Usage` / `NormalizedResponse`，裁掉 Codex / Bedrock / Anthropic 的 `provider_data` 兼容属性（`call_id`、`response_item_id`、`anthropic_content_blocks`、`bedrock_content_blocks`、`codex_reasoning_items`、`codex_message_items`）；保留 `extra_content`（Gemini 的 `thought_signature`）、`reasoning_content`、`reasoning_details`，它们在 OpenAI 兼容的 Chat Completions 上同样会出现 |
| `mertina/agent/transports/__init__.py` | L1 裁剪 | transport 注册表，只注册 `chat_completions` |
| `mertina/agent/transports/chat_completions.py` | L1 裁剪 | 保留 sanitize → build_kwargs → normalize_response 三步，去掉各家特判 |
| `mertina/agent/client_lifecycle.py` | L1 裁剪 | 共享客户端的加锁、关闭检测、关闭，以及关闭后重建；构造经 `_forward` 转发给 `agent_runtime_helpers.create_openai_client`，与上游相同 |
| `mertina/agent/agent_runtime_helpers.py` | L2 部分搬运 | 只有 `_ra()` 和 `create_openai_client`：复制 kwargs、`max_retries=0`、经延迟代理构造 |
| `mertina/agent/process_bootstrap.py` | L2 部分搬运 | 只有延迟加载的 `OpenAI` 代理 |
| `mertina/agent/lazy_forward.py` | 边界外，整文件 | `_forward` 转发器：mixin 借它把方法委托给模块级函数 |
| `mertina/agent/__init__.py`、`mertina/agent/jiter_preload.py` | 边界外，整文件 | 包导入时预加载 OpenAI SDK 的原生 JSON 解析器（部分 Windows 环境下在流式线程里首次加载会失败） |
| `mertina/tools/__init__.py` | 边界外，逐字 + 删减 | 只剩说明「导入 tools 包不能有副作用」的 docstring |
| `mertina/agent/iteration_budget.py` | L0 语义逐字抄 + 删减 | 去掉 `normalize_budget_warning_ratio` |
| `mertina/agent/conversation_loop.py` | L1 裁剪 | `run_conversation()` 入口与 turn 调度 |
| `mertina/agent/turn_api_request.py` | L1 裁剪 | 请求组装 |
| `mertina/agent/turn_response_intake.py` | L1 裁剪 | 响应归一化 |
| `mertina/agent/turn_tool_round.py` | L1 裁剪 | 一轮工具调用 |
| `mertina/agent/turn_finalizer.py` | L1 裁剪 | 收尾 |
| `mertina/agent/turn_context.py` | L1 裁剪 | 每轮准备：追加用户消息、重置迭代预算、构建 system prompt；组装发送用的消息副本 |
| `mertina/agent/turn_iteration_prep.py` | L1 裁剪 | 每次迭代开始时检查中断、消耗预算 |
| `mertina/agent/turn_request_assembly.py` | L1 裁剪 | 组装 `api_messages` |
| `mertina/agent/turn_api_call.py` | L1 裁剪 | 发出模型请求 |
| `mertina/agent/turn_response_check.py` | L1 裁剪 | 记录耗时，累加 usage |
| `mertina/agent/turn_final_response.py` | L1 裁剪 | 无工具调用时收下最终回答 |
| `mertina/agent/turn_loop_errors.py` | L1 裁剪 | 响应处理出错时补齐工具结果并结束本轮 |
| `mertina/agent/tool_executor.py` | L1 裁剪 | 并行执行独立工具调用，去掉审批网关/中间件/checkpoint/心跳 |
| `mertina/tools/registry.py` | L1 裁剪 | 保留 `ToolEntry` 形状与 `register()`，去掉插件作用域、发现缓存、`check_fn` 缓存 |
| `mertina/tools/time_tools.py` | 新写 | `get_time` 占位工具 |
| `mertina/model_tools.py` | L1 裁剪 | 工具定义收集与分发，去掉 toolset 选择、hook、bridge |
| `mertina/run_agent.py` | L1 裁剪 | `AIAgent` 摊平 14 个 mixin，`__init__` 参数收敛到本里程碑所需；步骤 7 之前只有模块 logger，供 `_ra()` 使用 |

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

v0.1.0 只做预算和基本的 stop flag；重试与流式中断在 v0.1.1。

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
- `vendor/` 已从工作树删除，开发规范中的上游 SHA 已更新
- README 的布局章节已更新为实际布局

---

## 8. 分刀

| | 内容 | 产出 |
|---|---|---|
| **v0.1.0**（本分支） | L0 叶子 + transports + registry + loop + `get_time` | fake client 下跑通完整循环 |
| v0.1.1 | 流式输出 + 重试 + 中断（`tools/interrupt.py`、`agent/prompt_builder.py`、`agent/system_prompt.py`） | Roadmap 的 stop 语义达标 |
| v0.1.2 | `web_search` + `WebSearchProvider` 接口 + 配置层（`mertina/cli/config.py`） | 真实 endpoint 跑通 Roadmap 验收场景 |

v0.1.2 完成时，Roadmap v0.1 的三条「Done when」全部满足。

---

## 9. 遗留问题

- **L2 的替代实现深度未定**：错误分类、脱敏、日志在 v0.1.0 用最简实现，P1 再评估要不要回头参考 Hermes 的对应模块。
- **`mertina/` 内部是否需要再分层**：v0.1 只有 `agent/` 和 `tools/` 两个子包，规模小。等 gateway、state、cron 进来后，是否需要在 `mertina/` 下引入更多分层，到时再看。
- **上游跟进机制未定**：`scripts/port_check.py` 保证已移植文件与文件头记录的上游 SHA 一致，但「上游在更新的 commit 上改了某个已移植文件」仍要靠人发现。给 port_check 加一个对比新 SHA 的模式是候选方案，留待 v0.1.2 之后评估。
