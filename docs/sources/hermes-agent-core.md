# Hermes Agent 核心循环来源与删减记录

核对日期：2026-09-21。适用范围：Mertina v0.1 第三阶段（Agent 循环、工具执行、提示词、流式、web_search）。
实施顺序与验收以[开发计划书](../plans/v0.1-phase-3-agent-loop.md)为准；本文记录代码来源及有意改变的行为，
不代表对应检查点已经完成。

## 固定来源与许可

- 上游：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。
- 固定提交：`4cefeed7debc7091ed65240cbc7e2c36435c0b6b`（本地参考 clone 的 HEAD）。
- 许可证：[该提交的 LICENSE](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/LICENSE)，MIT License，`Copyright (c) 2025 Nous Research`。
  LICENSE 的 Git blob 为 `75410e73319c72cd3e991a501c5455eb78f38375`，与第二阶段保存的
  [LICENSES/Hermes-Agent-MIT.txt](../../LICENSES/Hermes-Agent-MIT.txt) 一致，无需更新许可文本。

本阶段所有引用都指向上述固定提交。实现过程中不混用其他 Hermes 版本；如需换基线，另开记录。

### 与第二阶段固定提交的关系

[第二阶段来源记录](hermes-model-transport.md)固定在 `9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1`，
但该对象不在本地 clone 中（`git cat-file` 报 bad object），无法在本地复核。其引用的九处
`agent/transports/chat_completions.py` 行号与 LICENSE blob 均与 `4cefeed7de` 完全一致。
建议：在能访问上游时 `git fetch` 并核对该 SHA；若仍不可得，下次触及第二阶段文档时改钉到 `4cefeed7de`。
本阶段不修改第二阶段文档。

## 迁移原则

- **保留结构**：沿用 Hermes 的模块名、phase 拆分和 verdict 返回模式
  （`fallthrough` / `continue` / `break` / `return`），常量文本与关键算法尽量原样。
- **只删分支**：删去 v0.1 不需要的分支；删去的每一类都在下文列出理由。
- **两处结构性偏离**：
  1. `AIAgent` 的 14 个 Mixin 合并为普通类 `Agent`；phase 函数接显式参数，不用 `_run_phase`
     对 `_LoopState` 做反射传参。
  2. **异步**：Hermes 循环是线程同步的；Mertina 第二阶段的 `ModelClient` 是异步的，因此循环整体改为
     asyncio。机制改变、语义不变，对照见下文“异步改写”。
- **历史契约服从第二阶段 transport**：历史中只保存 transport 接受的字段。Hermes 在 tool 消息中写入
  `name`/`tool_name`、在 assistant 消息中写入 `finish_reason`/`reasoning`，发送前再删除；
  Mertina 直接不写入，finish_reason 保存在每轮状态中。

## 文件与符号对应

目标路径均相对于 `src/mertina_agent/`，“检查点”一列对应计划书 §4。

### Agent 与循环

| 固定来源 | Mertina 目标 | 检查点 | 保留 | 删减 |
|---|---|---|---|---|
| [run_agent.py:229 `AIAgent`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/run_agent.py#L229)、[agent/turn_facade.py:19 `TurnFacadeMixin`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_facade.py#L19)、[agent/interrupt_control.py:109 `interrupt` / :215 `clear_interrupt`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/interrupt_control.py#L109) | `agent/core.py` `Agent` | CP2 / CP4 | `run_conversation`、`chat`、`interrupt`、`clear_interrupt`、`is_interrupted`、每实例 system prompt 缓存、`tools` / `valid_tool_names` | Mixin、上百个构造参数、turn lease、relay、accounting、steer/redirect、fallback、凭据池、SessionDB |
| [agent/conversation_loop.py:1594 `run_conversation`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L1594)、[:1437 `_run_conversation_turn`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L1437)、[:1405 `_run_api_retry_loop`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L1405)、[:1289 `_LoopState`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L1289)、[:1642 `_close_durable_failed_turn`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L1642)、[:962 `_invalid_tool_name_error_content`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/conversation_loop.py#L962) | `agent/conversation_loop.py` | CP2 / CP4 | while 骨架、重试内循环、每轮状态、失败轮次补 assistant 边界、未知工具错误文本 | 压缩与 preflight、MoA、codex_app_server、prompt cache、billing 文案、plugin-compat、`_run_phase` |
| [agent/turn_context.py:401 `TurnContext`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_context.py#L401)、[:879 `build_turn_context`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_context.py#L879)、[:1072 `build_api_messages`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_context.py#L1072) | `agent/turn_context.py` | CP2 | 复制调用方历史、追加 user、记录 `current_turn_user_idx`、首次构建 system prompt、每次请求 clone 并前置 system | api_content sidecar、memory prefetch、插件上下文、压缩、会话标题、stdio 保护、reasoning 回放 |
| [agent/turn_iteration_prep.py:302 `begin_iteration`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_iteration_prep.py#L302)、[:387 `apply_retry_restarts`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_iteration_prep.py#L387) | `agent/turn_iteration_prep.py` | CP2 | 中断退出、调用计数、`iteration_budget.consume()`；restart 只留“已中断”与“全部重试无响应” | `prepare_iteration`、spinner、redirect/压缩/fallback/长度四种 restart、grace call、预算提醒 |
| [agent/turn_api_call.py:68 `perform_api_call`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_api_call.py#L68)、[:171 `handle_api_interrupt`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_api_call.py#L171) | `agent/turn_api_call.py` | CP2 / CP4 / CP5 | 流式与非流式分派、请求中可中断、保留已流出的部分文本、`INTERRUPT_WAITING_FOR_MODEL_PREFIX` 文案 | Nous 限流守卫、middleware、relay、MoA 握手、redirect 交叉检查 |
| [agent/turn_api_error.py:51 `handle_api_error`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_api_error.py#L51)、[:248 `settle_unrecovered_error`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_api_error.py#L248)、[agent/turn_recovery.py:1205 `abort_turn_on_interrupt`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_recovery.py#L1205)、[:1223 `interruptible_backoff_sleep`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_recovery.py#L1223)、[:1273 `compute_error_backoff`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_recovery.py#L1273)、[agent/turn_response_check.py:234 `retry_invalid_response`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_response_check.py#L234) | `agent/turn_api_error.py` | CP4 | 可重试判定、`retry_count` 上限、Retry-After 优先（封顶 600s，≤0 视为缺失）、否则 `jittered_backoff(n, 2, 60)`；无效响应 `jittered_backoff(n, 5, 120)`；退避中可中断 | 完整错误分类器、凭据刷新与轮换、fallback 链、auto-recovery、厂商特判、上下文溢出恢复、诊断缓冲 |
| [agent/turn_response_check.py:92 `check_api_response`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_response_check.py#L92)、[agent/turn_response_intake.py:119 `normalize_model_response`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_response_intake.py#L119) | `agent/turn_response_intake.py` | CP2 | finish_reason 分流：`content_filter`、`length` 以 partial 结束，截断的工具调用不执行；refusal 独立；usage 汇总 | 长度续写、Codex incomplete、scratchpad 重试、`post_api_request` hook、provider projection |
| [agent/turn_tool_validation.py:70 `validate_tool_calls`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_tool_validation.py#L70) | `agent/turn_tool_validation.py` | CP2 | 未知工具（混合批只报无效调用，全无效三次即 partial 退出）；非法 JSON 先不追加地重试请求 3 次，再注入错误结果；空参数写为 `"{}"`；截断参数 partial 退出 | 工具名模糊修复、tool_call ID 去重改写 |
| [agent/turn_tool_round.py:45 `run_tool_round`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_tool_round.py#L45)、[:217 `stage_tool_call_message`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_tool_round.py#L217) | `agent/turn_tool_round.py` | CP2 | 校验 → 追加 assistant(tool_calls) → 混合批错误结果 → 执行 → continue | 执行前持久化、guardrail halt、execute_code 退款、housekeeping 静音、压缩、去重、delegate 截断 |
| [agent/turn_final_response.py:46 `finish_text_response`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_final_response.py#L46) | `agent/turn_final_response.py` | CP2 | 追加最终 assistant，`turn_exit_reason = text_response(finish_reason=…)` | 空响应阶梯、stall/degenerate/ack 续写、dropped-tool-call 催促、stop gates、reasoning 提升 |
| [agent/turn_loop_errors.py:35 `handle_outer_loop_error`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_loop_errors.py#L35) | `agent/turn_loop_errors.py` | CP4 | 计数、为未应答的 tool_call 补错误结果、达到 `min(8, max_iterations)` 后终止 | traceback 模块分类、解释器关闭特判 |
| [agent/turn_finalizer.py:447 `finalize_turn`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_finalizer.py#L447)、[:122 `_resolve_budget_fallback`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_finalizer.py#L122)、[:226 `_close_transcript_tail`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/turn_finalizer.py#L226)、[agent/chat_completion_helpers.py:2255 `handle_max_iterations`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/chat_completion_helpers.py#L2255) | `agent/turn_finalizer.py` | CP2 / CP4 | 预算耗尽时追加 summary 请求并做一次无工具调用；中断时关闭 tool 结尾；“已交付 final_response 则历史必有 assistant 行”；组装结果；清除中断 | trajectory、持久化、micro-compaction、输出 hook、memory 同步、后台 review、kanban、文件变更脚注 |
| [agent/message_sanitization.py:260 `close_interrupted_tool_sequence`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/message_sanitization.py#L260) | `agent/message_sanitization.py` | CP2 | 原样 | 该模块其余清洗函数 |
| [agent/iteration_budget.py:25 `IterationBudget`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/iteration_budget.py#L25) | `agent/iteration_budget.py` | CP1 | 原样（非阻塞 `threading.Lock` 在异步下安全） | `normalize_budget_warning_ratio` |
| [agent/retry_utils.py:30 `parse_retry_after_seconds`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/retry_utils.py#L30)、[:121 `jittered_backoff`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/retry_utils.py#L121) | `agent/retry_utils.py` | CP4 | 原算法；时钟与随机源改为参数注入（测试规范要求） | quota/reset 文本语法、Z.AI 自适应退避 |
| [tools/interrupt.py:37 `set_interrupt` 等](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/interrupt.py#L37) | `agent/interrupt.py` | CP1 | 工具侧 `is_interrupted()` 接口 | 线程 ident 集合改为 contextvar；yield、`acting_for_tid`、调试追踪 |
| agent/turn_retry_state.py `TurnRetryState` | 不迁移 | — | — | 删减后无剩余字段（全是认证、格式恢复与 restart 标志） |

### 工具层

| 固定来源 | Mertina 目标 | 检查点 | 保留 | 删减 |
|---|---|---|---|---|
| [tools/registry.py:181 `ToolEntry`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L181)、[:421 `ToolRegistry`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L421)、[:649 `register`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L649)、[:824 `get_definitions`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L824)、[:874 `dispatch`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L874)、[:999 `tool_error`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/registry.py#L999) | `tools/registry.py` | CP1 | 注册时 schema 校验、check_fn 过滤、结果类型规范化、错误长度上限 2048、模块级默认 `registry` 与导入即注册；`Agent` 可注入独立 registry | 插件 overlay/scope/override、AST discovery 与磁盘缓存、check_fn TTL 缓存、toolset alias、MCP、动态 schema 覆盖 |
| [model_tools.py:213 `get_tool_definitions`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/model_tools.py#L213)、[:867 `handle_function_call`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/model_tools.py#L867)、[:630 `_sanitize_tool_error`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/model_tools.py#L630) | `tools/model_tools.py` | CP1 | 按 toolset 选工具、调度 registry、错误去结构标记 | 定义缓存、Tool Search、schema 重写器、bridge/connector、hook 与 middleware、`_run_async` 桥 |
| [agent/tool_executor.py:159 `_parse_tool_arguments`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L159)、[:310 `_append_skipped_tool_results`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L310)、[:1433 `_unfinished_tool_result`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L1433)、[:1761 `_execute_tool_calls_sequential`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L1761)、[:1491 `execute_tool_calls_concurrent`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L1491)、[:1818 `execute_tool_calls_segmented`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_executor.py#L1818) | `agent/tool_executor.py` | CP1 / CP3 / CP4 | 参数只解析不修复；按调用顺序写回；未开始调用补 skipped 结果；并行中被中断补 cancelled 结果 | 审批与授权门、middleware、start-order gate、spinner 与打印、结果落盘、checkpoint、guardrail 观测、DB flush、批次超时配置 |
| [agent/tool_dispatch_helpers.py:31 `_PARALLEL_SAFE_TOOLS`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_dispatch_helpers.py#L31)、[:164 `_plan_tool_batch_segments`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_dispatch_helpers.py#L164)、[:400 `make_tool_result_message`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_dispatch_helpers.py#L400)、[:515 `_maybe_wrap_untrusted`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/tool_dispatch_helpers.py#L515) | `agent/tool_executor.py` | CP1 / CP3 | 并行段与串行段规划（安全集合只含 `web_search`）；web_search 结果包 untrusted 定界符 | 路径冲突检测、破坏性命令识别、MCP 并行、风险元数据、时间戳、上游截断提示 |

### 搜索

| 固定来源 | Mertina 目标 | 检查点 | 保留 | 删减 |
|---|---|---|---|---|
| [agent/web_search_provider.py:43 `WebSearchProvider`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/web_search_provider.py#L43)（含 `ProviderBase.name`） | `agent/web_search_provider.py` | CP3 | `name`、`is_available()`、`search(query, limit)`、结果 envelope 约定 | extract、keyless、setup schema、`get_provider_env` |
| [tools/web_tools.py:268 `web_search_tool`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/web_tools.py#L268)、[:454 `WEB_SEARCH_SCHEMA`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/tools/web_tools.py#L454) | `tools/web_tools.py` | CP3 | limit 夹紧 1–100、调用前检查中断、无 provider 错误、JSON 结果、schema 原文 | web_extract、结果缓存与 rescue、多后端选择、debug 文件 |
| [plugins/web/_common.py:37 `search_ok` 等](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/plugins/web/_common.py#L37)、[plugins/web/ddgs/provider.py:130 `_run_ddgs_search_bounded`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/plugins/web/ddgs/provider.py#L130)、plugins/web/ddgs/_search_worker.py | `plugins/web/_common.py`、`plugins/web/ddgs/` | CP3 | 一次性子进程、30s 硬超时、terminate 后 kill 回收、stdout JSON envelope 校验 | 插件发现与注册、test hook 环境变量；子进程改用 `asyncio.create_subprocess_exec` |

### 提示词与流式

| 固定来源 | Mertina 目标 | 检查点 | 保留 | 删减 |
|---|---|---|---|---|
| [agent/prompt_builder.py:158 `DEFAULT_AGENT_IDENTITY`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/prompt_builder.py#L158)、[:345 `TOOL_USE_ENFORCEMENT_GUIDANCE`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/prompt_builder.py#L345)、[:380 `TASK_COMPLETION_GUIDANCE`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/prompt_builder.py#L380)、[:408 `PARALLEL_TOOL_CALL_GUIDANCE`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/prompt_builder.py#L408) | `agent/prompt_builder.py` | CP2 | guidance 原文；identity 只替换产品名与出品方 | context files、SOUL、memory/skills/kanban 指导、平台提示、环境探测、steer 说明 |
| [agent/system_prompt.py:659 `build_system_prompt_parts`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/system_prompt.py#L659)、[:551 `_guidance_parts`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/system_prompt.py#L551)、[:487 `_timestamp_line`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/system_prompt.py#L487)、[:41 `_model_gate`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/system_prompt.py#L41) | `agent/system_prompt.py` | CP2 | stable/context/volatile 三层顺序；有工具才注入工具指导；日期级时间行与 Model 行；时钟注入 | skills 索引、memory 块、插件段、coding posture、静态前缀重建 |
| [agent/chat_completion_helpers.py:2611 `_ToolCallAccumulator`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/chat_completion_helpers.py#L2611)、[:2964 `_call_chat_completions`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/chat_completion_helpers.py#L2964)、[:3133 `_assemble_tool_calls`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/chat_completion_helpers.py#L3133)、[:3159 `_finish_chat_stream`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/chat_completion_helpers.py#L3159) | `agent/transports/chat_completions.py`、`agent/model_client.py` | CP5 | 同 index 不同 id 分槽、name 赋值而非拼接、参数分片最后拼接、无 finish_reason 的结尾判定、截断参数不可执行、refusal 分片 | reasoning 与 reasoning_details、SSE echo 缓冲、router shim、relay、stale-stream watchdog、参数修复 |
| [agent/stream_delivery.py:19 `StreamDeliveryMixin`](https://github.com/NousResearch/hermes-agent/blob/4cefeed7debc7091ed65240cbc7e2c36435c0b6b/agent/stream_delivery.py#L19) | `agent/events.py` | CP2 / CP5 | 文本增量、工具生成、工具开始与完成的投递语义，改为单一 `event_callback` + 类型化事件 | TUI/TTS、单写入器、interim 去重 |

## 异步改写

| Hermes 机制 | Mertina 机制 | 保持的语义 |
|---|---|---|
| `_interrupt_requested` 布尔，其他线程调用 `interrupt()` | 布尔 + 每轮 `asyncio.Event`；跨线程经 `call_soon_threadsafe` | 迭代开始、请求中、退避中、工具启动前都能观察到停止 |
| `_interruptible_api_call` 在线程中请求并轮询中断 | 请求 task 与中断事件 `asyncio.wait(FIRST_COMPLETED)`，中断则取消 | 停止后不等待模型返回 |
| `interruptible_backoff_sleep` 以 200ms 切片 sleep | `asyncio.wait_for(event.wait(), timeout)` | 退避期间停止立即生效 |
| `ThreadPoolExecutor` 并行执行工具 | `asyncio.gather`；同步 handler 用 `asyncio.to_thread` | 结果按调用顺序写回；未开始的调用补 skipped 结果 |
| `tools/interrupt.py` 按线程 ident 记录中断 | contextvar 指向当前 run 的中断状态 | 工具侧 `is_interrupted()` 用法不变 |
| `KeyboardInterrupt` / `InterruptedError` | `asyncio.CancelledError` 始终传播 | 取消不被伪装成模型错误 |

## 明确不迁移的整类功能

| 类别 | 理由 |
|---|---|
| SessionDB、持久化、会话恢复 | v0.2 的会话层；v0.1 由调用方持有历史 |
| 上下文压缩、prompt cache、token 估算 | P1 健壮性 |
| fallback、凭据池、各厂商错误特判 | P1；v0.1 只有一个 OpenAI 兼容端点 |
| MoA、Codex、Anthropic、Bedrock 路径 | 只实现 Chat Completions |
| hook、middleware、插件、MCP | P1 扩展性 |
| steer / redirect、消息排队 | ROADMAP 明确排除（P1） |
| tool guardrails、调用去重、工具名修复 | P1 tool-loop guardrails |
| 长度续写、空响应阶梯、stop gates、stall 续写 | P1；v0.1 以明确的 finish_reason 结束 |
| spinner、vprint、状态缓冲等显示层 | 由事件接口替代，渲染属于调用方 |
| memory、skills、kanban、delegate、checkpoint | v0.4 及以后 |
| turn lease、relay、accounting | 多进程网关特性，不在 P0 |

## 测试来源

只迁移测试意图，不引入依赖完整 Hermes 初始化的 fixture：
`tests/agent/test_run_agent.py`、`test_iteration_budget_race.py`、`test_sequential_tool_interrupt.py`、
`test_turn_api_call_interrupt.py`、`test_turn_finalizer_interrupt_alternation.py`、
`test_turn_finalizer_iteration_limit_exit.py`、`test_streaming.py`、`test_streaming_tool_call_repair.py`、
`tests/tools/test_registry.py`、`tests/tools/test_model_tools.py` 中与上表保留行为直接相关的场景。
