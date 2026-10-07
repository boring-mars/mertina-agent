# v0.1 Agent Core — 从 Hermes 移植的设计

[English](../../en/design/2026-09-20-agent-core-loop.md) | **中文**

**状态**：v0.1.0、v0.1.1、v0.1.2 已发布，Roadmap v0.1 完成
**对应 Roadmap 里程碑**：[v0.1 — Agent core](../../../ROADMAP.md#v01--agent-core)
**分支**：`feat/agent-core-loop`（v0.1.0）、`feat/agent-streaming-retry-interrupt`（v0.1.1）、`feat/web-search-config`（v0.1.2）
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

第一刀之后还有级联删除（留下的代码大量调用已删函数）、删掉方法全被删完的 mixin、瘦身提示词文本三笔要扣，预计 v0.1 最终产物 **2,000–3,000 行**。

---

## 2. 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 拷贝边界定在 L0+L1；L2 平台层不整体拷，用到的函数按需搬到同名文件 | 见 §3 |
| D2 | 先全拷进 `vendor/`，再逐模块裁剪搬出 | 主线代码任何时刻都能跑，「不可运行期」只存在于 vendor 目录内 |
| D3 | `vendor/` 不提交 git | 提交会带来 2.5 万行 diff；MIT 署名靠每文件版权头 + 开发规范中的[从 Hermes 移植](../development/porting-from-hermes.md)即可满足 |
| D4 | 扁平布局（不用 `src/`），顶层只有一个具名包 `mertina`，其内部严格镜像 Hermes | 见 §5 |
| D5 | 不发布 wheel，作为独立产品从 checkout / Docker 运行（`[tool.uv] package = false`） | 项目定位是应用不是库；也是不用 `src/` 布局的理由 |
| D6 | v0.1 拆成 0.1.0 / 0.1.1 / 0.1.2 三刀，每刀一个分支 | 见 §10 |
| D7 | v0.1.0 不带内置工具：工具路径由验收脚本和测试自己注册的工具跑通，第一个内置工具是 v0.1.2 从上游搬的 `web_search` | 让 v0.1.0 的测试完全不依赖网络，同时不在仓库里放一个上游没有对应物的工具 |

---

## 3. 拷贝边界

| 层 | 内容 | 行数 | 处理 |
|---|---|---:|---|
| **L0** 接口叶子 | `agent/transports/base.py`、`agent/transports/types.py`、`agent/iteration_budget.py`、`agent/retry_utils.py`、`agent/web_search_provider.py`、`tools/interrupt.py` | 601 | **语义逐字抄**（定义见[从 Hermes 移植](../development/porting-from-hermes.md)规则 2），加来源头；范围外的功能在紧随其后的单独 commit 里删除，各文件删什么见 §7.1 |
| **L1** 循环骨架 | `conversation_loop.py` + 15 个 `turn_*.py` + `tool_executor.py` + `agent_init.py` + `registry.py` + `chat_completions.py` + `model_tools.py` + `AIAgent` 的 14 个 mixin + `hermes_cli/config.py` + `plugins/web/` 的 ddgs provider 等，共 46 个文件 | 28,234 | **拷进 vendor 后裁剪**，这是需要读懂的部分 |
| **L2** 平台层 | `agent_runtime_helpers.py`(3,509)、`model_metadata.py`(2,551)、`turn_recovery.py`(1,813)、`error_classifier.py`(1,394)、`redact.py`(1,335)、`display.py`(1,118)、`hermes_constants.py`(1,515)、`hermes_logging.py`(764) 等 | ~36,000 | **不整体拷**；循环用到某个函数时，只把它逐字搬到同名文件（见[从 Hermes 移植](../development/porting-from-hermes.md)规则 5） |

L0/L1/L2 划分的是**拷贝边界**（哪些文件进 vendor、哪些不进），与 §10 的里程碑划分是两个维度。L0 的 6 个文件都在边界内，但落地时间不同：`transports/base.py`、`transports/types.py`、`iteration_budget.py` 在 v0.1.0，`retry_utils.py`、`tools/interrupt.py` 在 v0.1.1，`web_search_provider.py` 在 v0.1.2。

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
6. `run_agent.py` + 用到的 mixin 和 `agent/agent_init.py`（保留 mixin 结构）

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
| `fire` | `run_agent.py` 的 `main()`，只在 `__main__` 里导入 | **v0.1.0 需要**，与上游同样锁 `0.7.1`（逐字保留入口，不改写成 `argparse`） |
| `httpx` | `tools/web_tools.py` | openai SDK 自带，不单列 |
| `ddgs` | `plugins/web/ddgs/provider.py` | **v0.1.2 需要**，免 key 的搜索 provider，锁 `9.16.0`（上游是可选依赖、不锁版本） |
| `pyyaml` | `hermes_cli/config.py` | **v0.1.2 需要**，读 config.yaml，与上游同样锁 `6.0.3` |
| `python-dotenv` | `hermes_cli/env_loader.py` | **v0.1.2 需要**，启动时载入 `.env`，与上游同样锁 `1.2.2` |

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
| `mertina/agent/agent_runtime_helpers.py` | L2 部分搬运 | `create_openai_client`（复制 kwargs、`max_retries=0`、经延迟代理构造）、`_ra()`、`invoke_tool`，以及步骤 7 加入的 `strip_think_blocks` / `extract_reasoning` / `copy_reasoning_content_for_api` |
| `mertina/agent/process_bootstrap.py` | L2 部分搬运 | 只有延迟加载的 `OpenAI` 代理 |
| `mertina/agent/lazy_forward.py` | 边界外，整文件 | `_forward` 转发器：mixin 借它把方法委托给模块级函数 |
| `mertina/agent/__init__.py`、`mertina/agent/jiter_preload.py` | 边界外，整文件 | 包导入时预加载 OpenAI SDK 的原生 JSON 解析器（部分 Windows 环境下在流式线程里首次加载会失败） |
| `mertina/__init__.py` | 我们自己写的 | 上游唯一没有对应物的文件：包的 docstring 和 `__version__` |
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
| `mertina/agent/message_metadata.py` | L2 部分搬运 | 只有 `append_message` 和 `stamp_message_timestamp`：给进入历史的消息盖时间戳 |
| `mertina/agent/turn_truncation.py` | L2 部分搬运 | 只有 `normalize_response_for_agent`；上游的截断恢复（`finish_reason == "length"`）不在 v0.1 内 |
| `mertina/agent/turn_usage.py` | L2 部分搬运 | 只有 `record_response_usage`：把 `response.usage` 累加进会话计数器并写一行日志 |
| `mertina/agent/usage_pricing.py` | L2 部分搬运 | 只有 `normalize_usage` 和 `CanonicalUsage`：把各家 usage 字段归一到 prompt / completion / total，不含计价 |
| `mertina/agent/message_sanitization.py` | L2 部分搬运 | `_sanitize_surrogates` 与 `close_interrupted_tool_sequence`（中断后补齐工具结果），步骤 7 又加入 `reasoning_content` 回传规则表 |
| `mertina/agent/tool_executor.py` | L1 裁剪 | 并行执行独立工具调用，去掉审批网关/中间件/checkpoint/心跳 |
| `mertina/agent/tool_dispatch_helpers.py` | L2 部分搬运 | 只有 `make_tool_result_message` 及其不可信工具输出包装：构造 `role: tool` 结果消息 |
| `mertina/agent/display.py` | L2 部分搬运 | 只有 `_detect_tool_failure`：从工具结果里识别失败；上游其余的终端渲染不在 v0.1 内 |
| `mertina/tools/daemon_pool.py` | 边界外，整文件 | `DaemonThreadPoolExecutor`：并行工具执行用的守护线程池，卡住的工具不阻塞解释器退出 |
| `mertina/tools/registry.py` | L1 裁剪 | 保留 `ToolEntry` 形状与 `register()`，去掉插件作用域、发现缓存、`check_fn` 缓存 |
| `mertina/model_tools.py` | L1 裁剪 | 工具定义收集与分发，去掉 toolset 选择、hook、bridge |
| `mertina/run_agent.py` | L1 裁剪 | `AIAgent` 保留上游的 mixin 结构：用到的 mixin 和 `agent/agent_init.py` 各自按同名路径搬，方法全被删掉的 mixin 从基类列表里去掉、文件删除；`__init__` 参数收敛到本里程碑所需。步骤 7 之前只有模块 logger，供 `_ra()` 使用 |
| `mertina/agent/agent_init.py` | L1 裁剪 | `init_agent()`：路由 → 客户端 → 工具 → 会话 → 配置段的分阶段初始化，去掉配置文件、memory、压缩、子代理等段 |
| `mertina/agent/turn_facade.py` | L1 裁剪（mixin） | 两个入口 `run_conversation()` 和 `chat()` |
| `mertina/agent/status_output.py` | L1 裁剪（mixin） | `_safe_print` / `_vprint`：受 quiet 模式约束的状态输出 |
| `mertina/agent/interrupt_control.py` | L1 裁剪（mixin） | `interrupt()` / `clear_interrupt()`：软中断标志 |
| `mertina/agent/vision_message_prep.py` | L1 裁剪（mixin） | `_get_transport()` 与工具结果内容；上游的图片分片处理不在 v0.1 内 |
| `mertina/agent/reasoning_params.py` | L1 裁剪（mixin） | 组装 assistant 消息，以及 DeepSeek / Kimi / MiMo 的 `reasoning_content` 回传判定 |
| `mertina/agent/chat_completion_helpers.py` | L2 部分搬运 | 发出请求（`interruptible_api_call` → `direct_api_call`）、`build_api_kwargs`、`build_assistant_message`，以及预算耗尽时的 `handle_max_iterations` |
| `mertina/agent/system_prompt.py` | L1，v0.1.0 部分搬运 | 只有 `build_system_prompt`，v0.1.0 只取调用方的 `system_message`（见 §7.2）。它在 L1 边界内，v0.1.0 只用到一部分，所以先按部分搬运处理；v0.1.1 按 L1 裁剪补全（见 §8.1） |
| `mertina/agent/message_content.py` | L2 部分搬运 | 只有 `flatten_message_text`：从多段内容里取出文本 |
| `mertina/agent/think_scrubber.py` | L2 部分搬运 | 只有 `THINK_TAG_NAMES`；上游的流式 think 清洗随流式输出在 v0.1.1 |
| `mertina/agent/context_compressor.py` | L2 部分搬运 | 只有 `MAX_ITERATIONS_SUMMARY_REQUEST` 这段请求文本；自动上下文压缩不在 v0.1 内 |
| `mertina/agent/turn_failure_copy.py` | L2 部分搬运 | 只有 `site_copy()` 和预算耗尽且总结失败时的那条文案 |
| `mertina/utils.py` | L2 部分搬运 | 只有 `safe_json_loads`，以及步骤 7 加入的 `base_url_hostname` / `base_url_host_matches` |

### 7.2 数据流

```
user_message
  → 组装 messages（system prompt：v0.1.0 只有调用方的 system_message；identity、工具说明、时间随 prompt_builder 在 v0.1.1）
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

工具由验收脚本和测试自己注册，不依赖网络，走的仍是真实的 registry 和执行器，工具路径可完整覆盖。

### 7.5 验收标准

- 一个脚本能用 fake client 跑完整循环，包含工具调用往返
- 上述 5 项单元测试通过
- `vendor/` 已从工作树删除，开发规范中的上游 SHA 已更新
- README 的布局章节已更新为实际布局

---

## 8. v0.1.1 设计

v0.1.1 在 v0.1.0 的循环上补齐 Roadmap v0.1 的三项：流式输出、最小重试、最小中断，以及分层的 system prompt。做法与 v0.1.0 相同：每一步先逐字搬运、再单独删减，用到的 L2 函数按需搬到同名文件。分支是 `feat/agent-streaming-retry-interrupt`。

### 8.1 模块清单

新增的文件：

| 路径 | 来源 | 说明 |
|---|---|---|
| `mertina/agent/retry_utils.py` | L0 语义逐字抄 + 删减 | `jittered_backoff` 和 `Retry-After` 解析 |
| `mertina/tools/interrupt.py` | L0 语义逐字抄 + 删减 | 按线程记录的中断位：`set_interrupt`、`is_interrupted`、`is_thread_interrupted`；去掉按线程代办和中断原因 |
| `mertina/agent/prompt_builder.py` | L1 裁剪 | 默认身份和工具使用说明这几段文本 |
| `mertina/agent/turn_api_error.py` | L1 裁剪 | 请求出错后的分类、计数和退避 |
| `mertina/agent/api_error_summary.py` | L1 裁剪（mixin） | `_summarize_api_error`、`_clean_error_message`：重试日志和失败文案用的错误摘要 |
| `mertina/agent/stream_delivery.py` | L1 裁剪（mixin） | 流式增量分发：think 清洗、工具之后换段、记录已输出的文本 |
| `mertina/agent/error_classifier.py` | L2 部分搬运 | `classify_api_error`，只按状态码和连接错误分类 |
| `mertina/agent/turn_recovery.py` | L2 部分搬运 | 失败尝试的日志、退避等待（可被中断）、重试耗尽或不可重试时的结束结果 |
| `mertina/agent/chat_completion_nonstream.py` | L2 部分搬运 | `_NonStreamRequest`：非流式请求在 worker 线程上发出，调用方轮询中断 |
| `mertina/agent/chat_completion_stream_monitor.py` | L2 部分搬运 | 流式请求的中断轮询 `_monitor_loop` |
| `mertina/agent/errors.py` | L2 部分搬运 | 只有 `EmptyStreamError` |
| `mertina/constants.py` | L2 部分搬运 | 来自 `hermes_constants.py`，只有部分输出兜底用的两个常量 |
| `mertina/time.py` | L2 部分搬运 | 来自 `hermes_time.py`，只有 `now()` 及其时区解析：system prompt 里的时间行 |

改动较大的既有文件：

| 路径 | 来源 | v0.1.1 的变化 |
|---|---|---|
| `mertina/agent/system_prompt.py` | L1 裁剪 | 从 v0.1.0 的部分搬运（只有 `build_system_prompt`）扩成三层：身份、工具说明、调用方的 system message 加时间行 |
| `mertina/agent/chat_completion_helpers.py` | L2 部分搬运 | `interruptible_api_call` 走 worker 线程；新增 `interruptible_streaming_api_call`、`_StreamingCall`、`_ToolCallAccumulator`、`_RequestClientRegistry` |
| `mertina/agent/client_lifecycle.py` | L1 裁剪（mixin） | 请求专用客户端：每次请求单独建客户端，正常结束后复用，出错后关闭，被中止后作废 |
| `mertina/agent/agent_runtime_helpers.py` | L2 部分搬运 | `force_close_tcp_sockets` 及连接池遍历：中止请求时只 `shutdown` 套接字，不跨线程 `close` |
| `mertina/agent/interrupt_control.py` | L1 裁剪（mixin） | `interrupt()` 同时设置执行线程和并发工具线程的中断位，`clear_interrupt()` 一并清除 |
| `mertina/agent/tool_executor.py` | L1 裁剪 | 登记工具 worker 线程，让中断能传到工具里；`tool_progress_callback` 的 started / completed |
| `mertina/agent/display.py` | L2 部分搬运 | 加入 `build_tool_preview`：工具进度里的单行参数预览 |
| `mertina/agent/think_scrubber.py` | L2，整文件 | `StreamingThinkScrubber`：跨分块的推理标签清洗 |
| `mertina/agent/turn_api_call.py` | L1 裁剪 | `_should_stream` 和流式分支；请求被中断时保留已输出的半句 |
| `mertina/agent/status_output.py`、`turn_failure_copy.py` | L1 裁剪（mixin） / L2 部分搬运 | 重试过程的状态缓冲，重试耗尽和不可重试的失败文案 |
| `mertina/run_agent.py`、`mertina/agent/agent_init.py` | L1 裁剪 | 接入 `StreamDeliveryMixin`；构造参数加入 `stream_delta_callback`、`tool_gen_callback`、`tool_progress_callback` |

`system_prompt.py` 一直在 L1 边界内（vendor 里有它）。v0.1.0 只需要 `build_system_prompt` 的一部分，所以当时按部分搬运处理、带 Partial 头；v0.1.1 用到了它的分层逻辑，按 L1 裁剪处理，Partial 头随之去掉。

### 8.2 数据流

```
run_conversation()
  → system prompt：身份 + 工具说明 + 调用方 system message 与时间行
  → 每次请求：重置流式记录 → perform_api_call
       默认 → _StreamingCall（worker 线程读流，调用方每 0.3s 检查中断）
           文本增量 → think 清洗 → stream_delta_callback
           工具名到达 → tool_gen_callback
       客户端是 Mock 时（测试）→ _NonStreamRequest（worker 线程发请求，调用方轮询中断）
  → 有工具调用：stream_delta_callback(None) 关闭输出框 → 执行工具
       每个调用：tool_progress_callback("tool.started", …) → 派发 → 追加结果 → ("tool.completed", …)
       下一段文字前加一个空行
  → 无工具调用：收下最终回答
```

流式是默认路径：即使没有注册回调也走流式，与上游一致。`chat()` 和 `run_conversation()` 的返回值不变。

### 8.3 错误、中断、预算

- **重试**：请求出错后先分类。429、5xx 和连接错误按 `jittered_backoff` 退避重试，尊重 `Retry-After`，默认最多尝试 3 次；其他 4xx 不重试。退避等待期间可以被中断。重试耗尽或不可重试时，以一条说明原因的文案结束这一轮。
- **流式内部的重试**：流还没输出文字就断开时，重新打开流（`MERTINA_STREAM_RETRIES`，默认 2 次）；端点拒绝 `stream_options` 时，不带它再请求一次。已经输出文字之后才断开，就返回已输出的部分，不重试，避免重复输出。例外是工具调用的参数没传完就断开：这时重试网络类错误。
- **中断**：`interrupt()` 设置停止标志，同时设置执行线程和工具线程的中断位，工具可以自己检查。模型请求在 worker 线程上进行，调用方每 0.3 秒检查一次：发现停止，就中止请求专用的客户端，并立即抛出 `InterruptedError`。之所以用 worker 线程，是因为在 Windows 上跨线程 `shutdown()` 不能唤醒阻塞的读取。这一轮以中断结束：未完成的工具调用补上结果；流式中途停止时，已输出的半句作为 assistant 消息写入历史。下一轮可以在同一段历史上继续。
- **预算**：与 v0.1.0 相同。

### 8.4 测试

全部用 fake client 或本地 HTTP 服务器，不访问外网：

- 重试：限流后重试、`Retry-After` 决定等待时间、连接错误重试、5xx 重试耗尽、4xx 不重试、退避期间停止
- 中断：工具执行中停止、工具在自己的线程上能看到停止（串行与并发）、等模型回应时停止（本地服务器挂住请求，要求 5 秒内返回）、流式输出到一半时停止（保留半句，下一轮继续）
- 流式：增量依次到达、跨分块的 think 标签不露出、工具名回调、工具之后换段、断流重试、部分输出兜底、`stream_options` 被拒后重试
- 工具进度：串行与并发都收到 started / completed、出错标记、回调异常不影响工具
- system prompt 的分层和时间行

### 8.5 验收标准

- `uv run python -m scripts.fake_loop` 通过两个场景：一轮流式的工具往返（增量拼起来等于最终回答，工具进度依次收到 started / completed）；一轮在工具开始时停止，下一轮在同一段历史上继续，假模型校验每个工具调用都有结果
- Roadmap v0.1 的「Done when」里，重试和「turn 中途 stop」两项由单元测试覆盖，停止后对话可以继续

---

## 9. v0.1.2 设计

v0.1.2 补齐 Roadmap v0.1 剩下的部分：第一个内置工具 `web_search`、它背后的搜索 provider 接口，以及读取模型端点的配置层，让循环能对真实端点跑通验收场景。做法与前两刀相同：逐字搬运、单独删减、用到的 L2 定义按需部分搬运，删减中的改写都记在[从 Hermes 移植](../development/porting-from-hermes.md)的偏差表里。分支是 `feat/web-search-config`。

### 9.1 模块清单

新增的文件：

| 路径 | 来源 | 说明 |
|---|---|---|
| `mertina/agent/web_search_provider.py` | L0 语义逐字抄 + 删减 | `WebSearchProvider` 接口，只保留 search 部分（见 §11） |
| `mertina/agent/provider_base.py` | L2 部分搬运 | `ProviderBase`：各类 provider 共用的名字和可用性 |
| `mertina/agent/provider_registry.py` | L2 部分搬运 | `ProviderRegistry`：按 Mertina home 分作用域的注册表，供插件卸载时还原 |
| `mertina/agent/web_search_registry.py` | L2 部分搬运 | 当前搜索 provider 的解析：配置的 `web.search_backend` / `web.backend` 优先，其次唯一可用的、再按 ddgs 回退；被 `plugins.disabled` 禁用的插件可以在报错里点名 |
| `mertina/agent/secret_scope.py` | L2 部分搬运 | `.env` 的分词器（`load_env_file`），`get_env_value` 和 env loader 共用 |
| `mertina/registration_lifecycle.py` | L1，整文件 | 插件注册的租约和还原协调 |
| `mertina/cli/config.py` | L1 裁剪 | 来自 `hermes_cli/config.py`：`load_config`（默认值 + config.yaml + `${VAR}` 展开，按文件签名缓存）、只读用户实际写下内容的 `read_raw_config_readonly`、`get_env_value`（进程环境，否则 `.env`） |
| `mertina/cli/config_defaults.py` | L2 部分搬运 | `DEFAULT_CONFIG` 里 v0.1 读到的几节：`model`、`agent`、`web`、`plugins` |
| `mertina/cli/env_loader.py` | L1 裁剪 | `load_mertina_dotenv`：启动时把 Mertina home 的 `.env` 载入进程环境 |
| `mertina/cli/plugins.py`、`plugins_discovery.py`、`plugins_manifest.py`、`plugins_loader.py`、`plugins_ledger.py` | L2 部分搬运 | 插件发现：扫描内置插件目录、校验 `plugin.yaml`、按 `plugins.enabled` / `plugins.disabled` 放行、导入模块并调用 `register(ctx)`，记录每项注册以便还原 |
| `mertina/plugins/web/_common.py` | L1 裁剪 | `BaseWebSearchProvider`，只保留 search 部分 |
| `mertina/plugins/web/ddgs/`（`provider.py`、`_search_worker.py`、`plugin.yaml`） | L1，整目录 + 删减 | 免 key 的 DuckDuckGo provider。每次搜索在一次性子进程里跑，30 秒超时、可被中断，子进程的环境去掉凭据 |
| `mertina/tools/environments/local.py`、`local_env_policy.py` | L2 部分搬运 | `_sanitize_subprocess_env`：给 ddgs 子进程去掉各家 provider 的 key 和内部密钥 |
| `mertina/tools/web_tools.py` | L1 裁剪 | `web_search` 工具：后端选择、派发到当前 provider、`check_web_api_key` 门控和注册 |
| `mertina/tools/tool_backend_helpers.py` | L2 部分搬运 | `read_selection`、`selection_exists`、`selection_error`：config.yaml 里是否存了后端选择，以及选了却不可用时的报错 |
| `mertina/tools/web_tools_extract.py` | L2 部分搬运 | 只有搜索也用到的两个报错函数 |

改动较大的既有文件：

| 路径 | v0.1.2 的变化 |
|---|---|
| `mertina/run_agent.py` | `main()` 先载入 `.env`，再按 参数 → config.yaml `model` → `OPENAI_BASE_URL` / `OPENAI_API_KEY` 补齐端点（偏差表有记录；不搬 `runtime_provider`，见 §11） |
| `mertina/agent/agent_init.py` | 恢复上游从 config 读 `agent` 一节（重试次数、工具使用和执行纪律提示） |
| `mertina/time.py` | 恢复上游从 config.yaml 读 `timezone`，`MERTINA_TIMEZONE` 仍然优先 |
| `mertina/constants.py`、`mertina/utils.py` | 部分搬运 Mertina home 的解析、`mertina_home_key`、`atomic_replace` 等 |

### 9.2 数据流

```
run_agent.main()
  → load_mertina_dotenv()：Mertina home 的 .env 载入 os.environ
  → _endpoint_from_config()：参数 → config.yaml model → OPENAI_BASE_URL / OPENAI_API_KEY
  → AIAgent(...)
       get_tool_definitions → web_search 的 check_web_api_key
           → 插件发现（第一次）：mertina/plugins/web/ddgs → register(ctx) → web_search_registry
  → 模型调用 web_search
       web_search_tool → 后端：web.search_backend > web.backend > 自动探测（ddgs）
           → provider.search(query, limit)
               → ddgs 子进程（凭据已去掉，30 秒超时，轮询中断）→ JSON 信封
       → {"success": true, "data": {"web": [{title, url, description, position}, …]}}
```

配置文件在 Mertina home 下：Windows 是 `%LOCALAPPDATA%\mertina`，其他系统是 `~/.mertina`，`MERTINA_HOME` 可以改。`config.yaml` 放设置，`.env` 放 key；`.env` 载入后，config.yaml 里的 `${VAR}` 也能引用它。`AIAgent` 本身仍然不读端点配置，直接构造时与 v0.1.1 相同。

### 9.3 错误、中断、预算

- **没有可用的 provider**：返回 `{"success": false, "error": ...}`，指向 config.yaml 的 `web.search_backend`。插件被 `plugins.disabled` 禁用时，报错说明是哪个插件。
- **严格选择**：config.yaml 里选了一个没注册的后端时，直接报错，不会悄悄换成别的 provider。
- **provider 出错**：ddgs 超时、被限流或子进程异常，都变成工具结果里的错误，这一轮继续。
- **中断**：搜索在子进程里跑，主进程每隔一小段时间检查中断；一旦停止就杀掉子进程，工具结果是「已中断」，下一轮照常继续。
- **`.env` 的编码**：BOM、UTF-16 和 NUL 会先清理；UTF-32 与上游一致，不改写文件，加载时报错（见 §11）。
- **预算**：与 v0.1.0 相同。

### 9.4 测试

单元测试都不访问外网，ddgs 用上游的测试钩子跑真实子进程：

- 配置层：默认值合并、`${VAR}` 展开、解析失败时退回默认值、缓存失效、`.env` 读取、`model` 为字符串或映射
- env loader：载入并覆盖 shell、`${VAR}` 重复载入不叠加、BOM / UTF-16 / NUL / UTF-32、凭据去掉非 ASCII、载入后 config 能引用
- 插件发现：内置插件加载并注册、只发现一次、`plugins.enabled` / `plugins.disabled`、导入失败和没有 `register()` 被记录、安全模式、按 home 缓存
- 注册表和 provider：按名字和作用域查找、快照与还原、当前 provider 的解析顺序、ddgs 结果整理、子进程超时和中断、信封解析
- `web_search`：后端选择、limit 截断、中断、各类报错、门控和注册；子进程环境去凭据；`atomic_replace` 的各个回退分支
- `run_agent.main()`：端点的来源和优先级、先载入 `.env` 再读 config

### 9.5 验收标准

- `uv run python -m scripts.real_loop` 对真实端点通过两个场景：模型调用 `web_search`、拿到结果并据此回答（流式增量拼起来等于最终回答）；一轮在工具开始时停止，下一轮在同一段历史上继续，真实 API 接受这段历史
- `uv run python -m scripts.fake_loop` 照常通过
- 至此 Roadmap v0.1 的三条「Done when」全部满足

验收结果：2026-10-07 在 `8f5e6ec` 上，对 DeepSeek 的 OpenAI 兼容端点（`https://api.deepseek.com`，模型 `deepseek-flash`）运行 `scripts.real_loop`，两个场景都通过，退出码 0：

- 搜索后回答：模型在同一步并发调用两次 `web_search`，据结果回答 Python 3.14.8，流式输出拼起来等于最终回答。其中一次搜索失败：ddgs 这次选中的 Brave 连接被重置（Windows `os error 10054`），失败作为工具结果里的错误返回，这一轮照常结束，模型用另一次的结果作答
- 停止后继续：第一个工具开始时停止，工具结果为 `Interrupted`，这一轮以「Operation interrupted.」结束；下一轮在同一段历史上发出，真实 API 接受这段历史，模型重新搜索后作答
- `scripts.fake_loop` 同时通过

---

## 10. 分刀

| | 内容 | 产出 |
|---|---|---|
| **v0.1.0**（`feat/agent-core-loop`） | L0 叶子 + transports + registry + loop + `AIAgent` | fake client 下跑通完整循环 |
| **v0.1.1**（`feat/agent-streaming-retry-interrupt`，见 §8） | 流式输出 + 重试 + 中断（`tools/interrupt.py`、`agent/prompt_builder.py`、`agent/system_prompt.py`） | Roadmap 的 stop 语义达标 |
| **v0.1.2**（`feat/web-search-config`，见 §9） | `web_search` + `WebSearchProvider` 接口 + 插件发现 + 配置层（`mertina/cli/config.py`、`env_loader.py`） | 真实 endpoint 跑通 Roadmap 验收场景 |

v0.1.2 完成时，Roadmap v0.1 的三条「Done when」全部满足。

---

## 11. 遗留问题

- **L2 的替代实现深度未定**：错误分类、脱敏、日志在 v0.1.0 用最简实现，P1 再评估要不要回头参考 Hermes 的对应模块。
- **`mertina/` 内部是否需要再分层**：v0.1 只有 `agent/` 和 `tools/` 两个子包，规模小。等 gateway、state、cron 进来后，是否需要在 `mertina/` 下引入更多分层，到时再看。
- **上游跟进机制未定**：`scripts/port_check.py` 保证已移植文件与文件头记录的上游 SHA 一致，但「上游在更新的 commit 上改了某个已移植文件」仍要靠人发现。给 port_check 加一个对比新 SHA 的模式是候选方案，留待 v0.1.2 之后评估。
- **不搬 `runtime_provider`**：上游的 `hermes_cli/runtime_provider.py` 按配置和环境解析服务商、端点和 key，覆盖各家官方服务商、OAuth 和本地模型探测。v0.1.2 只在 `run_agent.main()` 里读 config.yaml 的 `model` 和 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，只支持自定义的 OpenAI 兼容端点；`AIAgent` 本身不读配置。等 v0.2 gateway 需要从配置构造 agent 时再搬。
- **UTF-32 编码的 `.env` 会让启动失败**：与上游一致，`load_mertina_dotenv` 不改写 UTF-32 文件，随后按 latin-1 解析出带 NUL 的变量名，写入 `os.environ` 时抛 `ValueError`。常见编辑器不会存出 UTF-32，暂不处理。
- **不搬 `web_extract`**：v0.1.2 只搬 Roadmap 点名的 `web_search`。上游的 `web_extract` 会多出约 900–1,200 行：`tools/web_tools_extract.py`、`tools/web_tools_truncate.py`、`tools/url_safety.py` 的 SSRF 防护、`tools/website_policy.py`、`tools/web_result_cache.py` 的 extract 部分，以及一个能抽取正文的 provider。ddgs 只能搜索，所以这个 provider 要么是需要 key 的（tavily、exa、firecrawl 等），要么是 `plugins/web/keyless_mcp.py` 的免 key 轮换。`WebSearchProvider` 和 registry 里 extract 相关的部分随之删掉；等出现读网页正文的需求时（最早 P1），再从上游同名文件搬回来。
- **搜索接口只覆盖 search**：抽象层都在（`ProviderBase` → `WebSearchProvider` → `BaseWebSearchProvider`、provider registry、插件发现），但只有 ddgs 一个 provider，接口里只服务于需要 key、免 key 或 extract 的 provider 的部分随之删掉了：`agent/web_search_provider.py` 的 `WebSearchProvider.supports_extract` / `extract` / `is_keyless_available` 和 `get_provider_env`；`plugins/web/_common.py` 里 `BaseWebSearchProvider` 的 `KEY_ENV` / `EXTRACT` / `KEYLESS` 开关、按 key 判断的 `is_available`、`is_keyless_available`、`supports_extract`，以及 `provider_env`；`agent/provider_base.py` 的 `ProviderBase.get_setup_schema`；`agent/web_search_registry.py` 的 `get_active_extract_provider` 和回退链里的免 key 一级。接入需要 key 的 provider（tavily、brave-free）、searxng（`SEARXNG_URL`）或任何 extract provider 之前，要先从上游同名文件把它们搬回来。
