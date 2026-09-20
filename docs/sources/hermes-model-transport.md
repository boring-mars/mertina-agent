# Hermes 模型接入层来源与删减记录

核对日期：2026-09-20。适用范围：Mertina v0.1 第二阶段模型接入层。
实现与验收边界以[开发计划书](../plans/v0.1-phase-2-model-transport.md)为准；
本文记录代码来源及有意改变的行为，不代表真实模型验收已经通过。

## 固定来源与许可

- 上游：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。
- 固定提交：`9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1`。
- 许可证：[该提交的 LICENSE][license]，MIT License。
- 原始署名：`Copyright (c) 2025 Nous Research`。
- 完整许可原文保存在 [LICENSES/Hermes-Agent-MIT.txt](../../LICENSES/Hermes-Agent-MIT.txt)。

以下引用全部指向固定提交，不以变化中的 `main` 作为迁移依据。
Mertina 原有 [LICENSE](../../LICENSE) 不替代 Hermes 的版权与许可声明。
包含派生代码的源码包与 wheel 均须随附上述完整许可文本；打包配置通过
`license-files` 纳入该文件，并应在发布前核对构建产物。

## 文件与符号对应

目标路径均相对于 `src/mertina_agent/`。这里的“迁移”是保留可追溯的主干后删减和适配，
不表示文件逐字相同，也不表示保留 Hermes 全部行为。

| 固定来源 | Mertina 目标 | 保留的主干 | 删除或适配 |
|---|---|---|---|
| [agent/transports/types.py][types] | `agent/transports/types.py` | `ToolCall`、`Usage`、`NormalizedResponse` 的共享结果边界 | 去除 provider 数据、reasoning、缓存统计及兼容属性；增加严格类型、不变性与独立 refusal |
| [agent/transports/base.py][base] | `agent/transports/base.py` | `ProviderTransport` 和四个转换职责 | 去除跨协议结束原因映射、缓存统计、任意透传参数；使用类型化契约 |
| [agent/transports/chat_completions.py][chat] | `agent/transports/chat_completions.py` | `ChatCompletionsTransport`、消息与工具转换、请求构建、结果归一化 | 仅保留标准文本与 function 工具协议；字段白名单和运行时校验替代宽松清洗 |
| [agent/transports/__init__.py][registry] | `agent/transports/__init__.py` | 共享类型和 Transport 的公共导出意图 | 静态导出替代 registry、动态导入、注册副作用和旧路径回退 |
| [agent/agent_init.py:789–797][init] | `agent/model_client.py` | `_explicit_client_kwargs` 中显式地址、密钥、超时传递 | 接现有 Settings；不迁移 CLI/provider/外部进程配置 |
| [agent/agent_runtime_helpers.py:1761–1871][runtime] | `agent/model_client.py` | `create_openai_client` 的客户端构造边界及重试归属 | 改用异步 SDK；强制单次尝试；显式处理 owned/injected 资源 |
| [agent/chat_completion_helpers.py:716–749][dispatch] | `agent/model_client.py` | `_dispatch_nonstreaming_api_request` 的标准 `chat.completions.create` 路径 | 去除多协议分派、请求线程、watchdog、MoA 和 SDK transform 内部补丁 |

前三份文件是核心迁移对象。客户端只提取上述片段的职责和标准调用路径，
不会把三个大型运行时模块整份引入。`Settings` 集成、异步生命周期、输入联合类型和异常分类
属于 Mertina 本阶段的适配，不应被描述为上游已有的相同实现。

### Chat Completions 的精确保留点

上游固定版本中的定位如下，可用于复查删减过程：

- `_base_kwargs`：第 343 行起，保留 model/messages 与非空 tools 的构建意图。
- `_sanitize_message`：第 375 行起，保留不修改输入以及省略空 assistant `tool_calls` 的协议意图。
- `ChatCompletionsTransport`：第 419 行起，保留类边界。
- `convert_messages` / `convert_tools` / `build_kwargs`：第 430 / 442 / 446 行起。
- `normalize_response` / `_normalize_tool_call` / `validate_response`：第 581 / 625 / 644 行起。

本项目不会保留上游“干净输入直接返回原对象”的 identity 优化；嵌套 Schema 和消息数据需安全复制。
上游对内部字段的静默删除也不等于本项目允许任意输入：未知字段在调用前明确报输入错误，
调用方不能借此传入多模态或厂商扩展。合法 tool 结果只输出正文与关联 ID，不添加 `name`。

## 有意改变的响应行为

这些变化是本项目契约要求，不是迁移遗漏。修改相关逻辑时应保留原因注释和回归测试。

| 上游行为 | Mertina 的选择 | 原因 |
|---|---|---|
| `ToolCall.id` 可为 `None`，由后续 Agent 补 ID | ID/name 必须是非空字符串 | 本阶段没有会话修复层，不能编造关联 ID |
| 无 function/name 的工具调用被过滤；缺失 arguments 补成 `{}` | 任一调用缺必需字段则整份响应失败 | 不能丢掉模型请求的部分操作，也不能把未知参数改成有效参数 |
| `build_tool_call` 会将字典参数 JSON 化或把其他值转为字符串 | arguments 必须是字符串，并逐字保留 | 工具参数解析、修复与执行属于后续执行层 |
| `Usage.from_openai` 将缺失/None 计数变成 0 | 缺失为 `None`；真实 0 保留；非法计数拒绝 | 未知成本不能被记成零成本 |
| 归一化可能把整数、大小写或未知结束原因映射成通用结果 | 要求非空字符串，原样保留未知值 | 不把截断或未知终止伪装成普通 `stop` |
| 单独 refusal 被填入 content，可能把 `stop` 改成 `content_filter` | refusal 单独返回，保留实际 finish_reason | 调用方可以区分正文、拒绝与过滤，不依赖不存在的重试循环 |
| `validate_response` 接受非空 choices，并识别 router 特定失败文本 | 必须恰好一个 choice，校验 message、结束原因和载荷 | 限定非流式单结果契约，不引入厂商失败文本特判 |
| 结果携带 provider_data、reasoning 及旧式访问器 | 只暴露稳定的最小结果结构 | 后续 Agent 无需依赖厂商对象或旧运行时兼容层 |

`length` 与 `content_filter` 即使没有正文也保持可辨识；工具参数可能被截断时，
本层只保留原始信息，不宣称可安全执行。没有文本、工具、拒绝或上述终止指示的空响应会失败。

## 明确删除的依赖与分支

- Provider profile、`providers/*`、动态协议发现和全局 registry。
- Kimi/Moonshot、Gemini、LM Studio、OpenRouter、TokenHub 等厂商分支。
- xAI 工具别名、Pareto router、router timeout shim，以及模型名触发的 system/developer 改写。
- 推理强度、签名回放、`reasoning_details`、自动 token 上限和厂商 `extra_body`。
- Prompt cache key、缓存计数、共享连接池、keepalive 调整、鉴权池及恢复切换。
- `prompt_builder.py`、`message_sanitization.py`、`hermes_cli/config.py`、`run_agent.py` 的依赖链。
- plugin-compat 重导出、Codex/Anthropic/Bedrock/原生 Gemini 和外部进程调用。

上游 `create_openai_client` 使用 `setdefault("max_retries", 0)`，其理由是重试由外层会话循环统一管理。
Mertina 保留这一职责划分，但必须显式覆盖为 0，包括预配了重试的注入 SDK；本阶段本身不提供外层重试。
客户端改用异步 API 后，任务取消直接传播，不能据此声称已迁移 Hermes 的产品级停止与历史修复。

### 本项目 SDK 适配说明

实际依赖锁定为 OpenAI SDK 3.16.2，测试边界使用显式 dev 依赖 HTTPX2 2.13.0 的
`MockTransport`。网络调用采用 SDK 公共 `chat.completions.with_raw_response.create`，
在取得 JSON 后交给 Transport 校验，避免依赖 SDK 宽松对象充当协议验证器；不使用上游
绕过 SDK 内部 transform 的补丁。

SDK 的 `with_options` 派生实例共享 HTTP 资源；本项目不关闭注入的实例或共享连接。
请求边界移除环境继承 headers，显式使用 Settings 凭据。对于 HTTP 层自带 auth、
自动重定向、自定义 headers、cookies 或 query 参数等无法由 SDK options 可靠覆盖的配置，
构造时拒绝。自有 HTTP 客户端禁用自动重定向，确保 307 也不会隐式触发第二次请求。
为完成此检查，仅在客户端边界只读访问已测试 SDK 的 `_client`，不修改外部对象。
该兼容性点有离线回归测试，升级 SDK 时必须复验。

与上游 `_explicit_client_kwargs` 拆分 URL query 的做法不同，本阶段只接受标准 HTTP(S)
base URL 和路径，不接受 URL 内嵌凭据、query 或 fragment，以免 SDK 把资源路径拼进 query。
完整实现与验收证据见[开发记录](../plans/v0.1-phase-2-development-log.md)。

## 测试来源与新增覆盖

参考 [test_chat_completions.py][test-chat] 中的基本请求构建、文本与工具响应、拒绝响应测试意图，
以及 [test_chat_completions_empty_tool_calls.py][test-empty] 中的空工具调用省略和输入不变性回归意图。
不引入这些测试依赖的完整 Hermes 初始化、动态 registry 或厂商 fixture。

上游测试中的 provider reasoning、拒绝内容提升、未知字段静默清洗、clean-list identity 等断言
不适用于当前契约，不应原样移入。本项目另外补充严格错误输入、多 choice、工具必需字段、
未知/零 token 区分、畸形 JSON 原文、嵌套复制，以及 HTTP 模拟边界的鉴权、重试和资源归属测试。

全部自动化验证保持离线。真实端点示例只在用户明确指定配置后显式运行，
不得将 HTTP mock 成功记录成真实模型调用成功。具体结果应查阅开发计划和阶段验收记录。

[license]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/LICENSE
[types]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/transports/types.py
[base]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/transports/base.py
[chat]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/transports/chat_completions.py
[registry]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/transports/__init__.py
[init]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/agent_init.py#L789-L797
[runtime]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/agent_runtime_helpers.py#L1761-L1871
[dispatch]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/agent/chat_completion_helpers.py#L716-L749
[test-chat]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/tests/agent/transports/test_chat_completions.py
[test-empty]: https://github.com/NousResearch/hermes-agent/blob/9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1/tests/agent/transports/test_chat_completions_empty_tool_calls.py
