# 会话存储的“最大公约数”：运行时业务、事务与公共基座

> 范围更新（2026-10-23）：用户已确定第一期仅做 SQLite、PostgreSQL、MySQL 接入适配。
> 以[第一期计划](../../plans/v0.2-phase-1-databases.md)为准；本文保留原调研结论和候选业务设计。

调研日期：2026-10-23（Asia/Shanghai）。状态：供评审的设计研究，未实现、未批准为 API。

配套材料：[Hermes 方案与 12 种数据库调研](session-database-landscape.md)。
项目依据：[ROADMAP v0.2](../../../ROADMAP.md#v02--gateway-and-sessions)、
[编码规范](../../zh/development/coding-style.md)、[测试规范](../../zh/development/testing.md)。
本地源码核验基线：`84b65f4e6e23f67b5ba788a1da7c2e6972394119`。

## 1. 结论：共用业务契约和事务算法，不共用数据库的全部行为

最适合本项目的公共基座不是一个拥有 `connect/execute/commit` 的万能 `BaseDatabase`，
而是“会话快照读取 + 原子回合提交 + 少量会话 CRUD”的业务接口，以及真正可共享的实现模块。

建议结构为：

- `SessionStore`：定义调用者可依赖的业务保证，不泄漏 SQL 或驱动类型。
- 公共纯逻辑：消息编解码、历史前缀校验、提交命令、指纹、序号、错误分类和契约测试。
- `SqlSessionStore`：若批准 SQLAlchemy Core 选型，共享关系型查询及短事务算法。
- 小型后端策略：连接、schema、事务启动/重试、类型映射和能力验证。
- 非关系型适配器：实现同一业务契约，但不假装自己拥有 SQL 的物理模型。

“最大公约数”指满足本业务最低可靠性要求的公共契约，不是把可靠性降低到十二个产品中最弱的水平。
某数据库或部署方式不能满足契约，就暂不列为可用主库，而不是让其他后端一起降级。

本文的接口、字段和文件名都是候选设计。ROADMAP 当前只指定 SQLite，多数据库范围、依赖、
初始化权限与额外技术元数据都需要实施前确认。本次仅创建研究文档。

## 2. 从当前 Agent 的真实行为出发

| 核验事实 | 本地证据 | 对持久化的约束 |
|---|---|---|
| 调用者传入旧历史，返回完整历史，包含本轮 | [core.py](../../../src/mertina_agent/agent/core.py) 的 `run_conversation`；[turn_result.py](../../../src/mertina_agent/agent/turn_result.py) 的 `ConversationResult` | 不能把完整返回列表再次追加到旧消息后面 |
| 历史容器被克隆，本轮 user 消息由 Agent 追加 | [turn_context.py](../../../src/mertina_agent/agent/turn_context.py) 的 `build_turn_context` | Gateway 不应先塞入同一条 user 再作为 `user_message` 传入 |
| 当前旧前缀不变，可提取新增后缀 | [test_core.py](../../../tests/unit/agent/test_core.py) 的历史不变测试 | v0.2 可以采用前缀验证后的增量写入，未来压缩不能静默沿用这个假设 |
| 核心先发 terminal event，再返回结果 | `core.py` 的 `emit_event(..., _final_event(result))` | Agent 结束事件不表示数据库提交成功 |
| callback 同步，异常仅记录日志 | [events.py](../../../src/mertina_agent/agent/events.py) 的 `EventCallback`、`emit_event` | 不在回调中承担必须成功的异步落库；Gateway 必须显式 await |
| 停止、普通失败、partial 也可返回完整历史 | [turn_result.py](../../../src/mertina_agent/agent/turn_result.py)、[conversation_loop.py](../../../src/mertina_agent/agent/conversation_loop.py) | 不能只保存 `completed=True` 的回合 |
| 工具调用和结果需对应，失败/停止路径会尽量闭合历史 | [turn_tool_round.py](../../../src/mertina_agent/agent/turn_tool_round.py)、[tool_executor.py](../../../src/mertina_agent/agent/tool_executor.py)、[turn_finalizer.py](../../../src/mertina_agent/agent/turn_finalizer.py) | 以规范化结果消息为真源，不能只存最终回答字符串 |
| 强制取消、校验失败或未覆盖异常可能没有结果 | `core.py`、[turn_api_call.py](../../../src/mertina_agent/agent/turn_api_call.py) | 不能假设任何异常都有可提交 transcript |
| system prompt 在 API 请求副本中额外插入 | `turn_context.py` 的 `build_api_messages` | 保存历史不等于精确复现旧 prompt/config；配置快照是另外的承诺 |

`RunCompleted` 还可能对应 partial，不能仅从事件类名推导 `completed=True`。
`agent.interrupt()` 是协作停止；`Task.cancel()` 可以直接传播取消，两者不能视为同一个收尾路径。
模型重试会让已展示的 delta 失效，因此 SSE 事件拼接不是可靠的会话历史。

## 3. 先划定持久化承诺

### 3.1 建议的最小模式：收尾提交

已正常返回 `ConversationResult` 的成功、停止、失败和 partial 回合，都提交规范化历史。
在提交得到确认后，才对外发布“该结果已经保存”的终态。

本模式允许进程被强杀或未形成结果时丢失尚未提交的当前轮，包括该轮用户输入。
HTTP 接受请求、返回 run ID、发送文本 delta，都不等于确认本轮已经持久化。
这必须体现在 API/UI 文案与测试里，不能靠“用了数据库”暗示未承诺的保障。

ROADMAP 的重启恢复是恢复已保存的会话；运行中的 SSE 重连不是重启后续跑旧模型调用。
运行中崩溃恢复在 P1，v0.2 不默认引入 durable run queue、事件日志或分布式 lease。

### 3.2 若要求“接受输入后就不能丢”，是一个独立增强

需要增加接收事务 `T_accept`：保存请求幂等标识及 pending input，然后才返回已持久化接受。
运行仍在数据库事务外，结束后通过第二个短事务提交完整 transcript 与接收记录的终态。

pending input 应与可回放历史有清楚边界。不能先把 user 写进 history，又将该 history 和同一
`user_message` 传给当前 Agent，造成重复输入。接收幂等与结果提交幂等也不是同一个接口。

这一增强不是免费开关，需要额外状态、恢复策略和验收。保存 pending input 也不等于能自动
安全重跑外部工具；本文不把它偷偷纳入最小模式。

## 4. 每个运行时动作真正需要什么事务

| 业务动作 | 最小存储保证 | 协调和事务边界 |
|---|---|---|
| 创建 session | 唯一 ID，一条完整元数据记录 | 一次短写；重复 ID 明确冲突，不再创建第二个会话 |
| 会话列表 | 明确排序与游标 | 一次查询；默认不承诺跨所有分页的全局快照 |
| 读取详情 | 一致的已提交元数据 | 单次读取；运行中状态来自当前进程注册表 |
| 恢复完整历史 | revision、消息数量和消息来自同一个一致状态 | 一次短读取单元；结束后归还连接 |
| 消息分页 | 固定会话和高水位内的明确序号 | 游标携带截止序号；不能下一页悄悄混入新回合 |
| 修改标题 | 单记录原子更新，不覆盖历史字段 | 短写，按 `metadata_version` 检查竞争 |
| 删除 session | 会话、消息、提交凭据一起不可见 | 与运行协调后，短事务删除；运行中建议拒绝删除 |
| 开始 run | 同会话不并发执行，读取旧历史 | 进程内 gate；最小模式不强制新增 DB 写入 |
| 推理、调用工具、发送 delta | 不持有 DB 事务或专属连接 | LLM 和工具属于外部操作，不可由 DB rollback 撤销 |
| 提交收尾回合 | 新消息、历史版本、相关计数和提交凭据原子生效 | 本文的核心短写事务 `T_commit` |
| 请求停止 | 设置中断信号 | 不需要持久事务；返回 stopped 结果后仍走 `T_commit` |
| SSE 重连 | 重新附着同一个仍运行的任务 | 内存事件缓存/广播，不重跑 Agent、不重复插入 user |

### 4.1 应用层串行不等于持有数据库锁

```text
取得 session gate
  ├─ 短读取：取得 HistorySnapshot，归还连接
  ├─ 长运行：LLM → 工具 → LLM；只发暂态进度，没有 DB 事务
  ├─ 纯逻辑：校验返回历史，构造稳定的 TurnCommit
  ├─ 短写入：消息 + revision + receipt 原子提交
  └─ 提交确认：登记终态，再对外发布已保存结果
释放 session gate
```

gate 覆盖运行和提交，是进程内协调；不是跨几分钟推理持有数据库连接、行锁或写事务。
不同 session 可并发。CAS 只能防历史提交覆盖，不能防两个实例已经重复执行工具；
因此多数据库支持不自动升级成多 Gateway 共享主库运行。

### 4.2 标题、删除与正在运行的回合

建议拆分 `history_revision` 与 `metadata_version`。修改标题只推进后者；回合提交只修改明确的
历史字段和活动时间，不把运行开始时的整个 session 对象覆盖回去。否则一个改名就可能使几分钟
后的历史提交冲突，或被旧对象覆盖。共享 `save(session)` 不适合表达这个边界。

v0.2 删除活动会话建议返回冲突，或者由显式工作流先停止、等待提交再删除。检查占用与执行删除
必须和新 run 取得同一协调入口，不能先检查再留下竞态窗口。不存在的 session 不允许提交时 upsert
重建；外键级联也不能替代这一业务规则。

## 5. 公共领域模型：定义数据语义，不预先规定所有物理表

| 概念 | 候选字段和规则 |
|---|---|
| Session | 应用生成的稳定 ID、标题、创建时间、历史活动时间、`history_revision`、`metadata_version`、`message_count` |
| HistorySnapshot | session ID、历史 revision、截止序号、完整规范化消息；来自一致的已提交状态 |
| Message | 应用生成的 ID、session ID、显式 seq、payload 版本及完整消息 payload |
| TurnCommit | 操作 ID、预期历史 revision、旧消息数、本轮新增消息、结果分类和必要元数据、固定请求指纹 |
| CommitReceipt | session/operation ID、请求指纹、实际新 revision、首尾序号和可重复返回的提交结果 |

建议初始 revision 为 0，seq 从 1 连续增长；一次成功收尾提交推进一次历史 revision，不按 token
或模型请求次数推进。message_count 与新增 seq 范围在同一个原子操作里更新。

数据库自增 ID、时间戳与自然行序都不是对话顺序。时间可用统一的 UTC 微秒整数表达，再由适配器
映射存储类型；它用于展示/排序，不用于代替 seq 或并发控制。ID 采用统一规范化编码，避免不同
排序规则造成身份比较差异。

### 5.1 编解码是所有后端真正共享的模块

公共 codec 需要保持现有 `ChatMessage` 的字段和值，而不是把所有消息压成 role/content：

- 保留 tool call ID、tool_call_id、工具名称、arguments 原文和结果。
- 保留缺失字段、显式 null、空字符串的差别；工具 arguments 即使不是合法 JSON，也仍是原始字符串。
- 保留 refusal、合法的 system/developer 消息，以及停止/失败生成的规范化尾部。
- usage 如果纳入结果元数据，未知值保持未知，不能把 null 转为 0。
- 外层存储 payload 采用版本化编码，读取时校验；未知版本或损坏不能当空历史继续运行。
- 不为数据库兼容而擅自重写 Unicode、字符串内部空白或工具参数。消息/回合大小上限在公共入口校验，
  超限明确拒绝，不静默截断；具体上限需按首批驱动和负载测试定稿。

关系型实现可把规范 JSON 存为 TEXT/LOB，不要求所有后端支持原生 JSON 查询。
这也能保留 Oracle 外层空字符串语义下的消息内部空串；标题等独立标量字段仍需单独定义空值规则。
不要求所有数据库采用同一列类型，但要求解码后同义、无损。

### 5.2 当前只追加，未来重写历史单独设计

当前可复用的纯逻辑是：

1. 保存进入 Agent 前的历史快照及 revision、消息数。
2. 得到完整 `result.messages` 后验证长度不少于旧历史，且旧前缀完全一致。
3. 只提取新增后缀，分配稳定 message ID 和连续 seq。
4. 固定结果分类、时间和 operation ID，生成提交命令；重试复用同一命令。

前缀不一致应报 `HistoryMismatch`，而不是静默覆盖旧历史或猜测差异。
完整 snapshot 替换、不可变 generation + 指针切换也是可行实现，但会增加当前不需要的重写/回收成本。
因此本研究优先建议“运行时完整历史 in/out，存储层验证后增量提交”。
未来压缩、历史编辑或分支才引入独立的 `replace_history` 契约，不能悄悄改变 append 的意义。

## 6. 可以公用的一套接口和模块

### 6.1 运行时接口轮廓

以下是类型化接口示意，不是可直接运行的模块。DTO 名称对应上一节，CRUD 请求还需在实现评审中
细化；不传任意 SQL、通用 dict patch 或连接对象。

```python
class SessionStore(Protocol):
    async def create_session(self, request: CreateSession) -> SessionRecord: ...

    async def get_session(self, session_id: str) -> SessionRecord: ...

    async def list_sessions(self, query: SessionListQuery) -> SessionPage: ...

    async def rename_session(self, request: RenameSession) -> SessionRecord: ...

    async def delete_session(self, request: DeleteSession) -> None: ...

    async def load_history(self, session_id: str) -> HistorySnapshot: ...

    async def read_messages(self, query: MessagePageQuery) -> MessagePage: ...

    async def commit_turn(self, command: TurnCommit) -> CommitReceipt: ...

    async def get_commit_receipt(
        self, session_id: str, operation_id: str
    ) -> CommitReceipt | None: ...
```

`TurnCommit` 是已经通过公共 builder 校验的新增后缀，不是完整 `ConversationResult.messages`。
适配器仍校验命令形状与数据库内的 revision/count，不能把纯 Python 校验当成并发保护。
提交元数据使用封闭、精确的类型；结果分类保留 completed/failed/interrupted/partial 的语义，
不要求把全部诊断文本、完整模型配置或每次 run 事件落库。

接口不要暴露 `begin()` 让 HTTP handler 自行拼接“先存消息、再改 session、再记成功”。
`commit_turn` 本身就是原子边界。可以有关系型内部 Unit of Work，但不把 SQL 事务对象变成所有
调用方、MongoDB 和 Redis 都必须模拟的公共接口。

### 6.2 生命周期接口单独定义

管理侧可以有 `open / inspect / plan_schema / apply_schema / healthcheck / close`。
初始化或升级通过显式策略授权；普通 SessionStore CRUD 不创建账号、升级表结构或选择备用主库。

后端工厂只在装配阶段根据配置选择实现，返回运行时 store 和生命周期资源句柄。
上层 Agent 不依赖工厂、SQLAlchemy、Gateway 或数据库配置。

### 6.3 建议职责分布

下列为将来实施时的候选路径；本次不创建这些代码文件，也不要求先搭空目录。

| 候选模块 | 真正共享的内容 | 不应包含 |
|---|---|---|
| `state/models.py`、`state/store.py` | 精确 DTO、领域协议、错误语义约定 | 驱动类型、SQL、模型调用 |
| `state/serialization.py` | 版本化消息编码、解码、校验与规范指纹输入 | 改写工具参数或恢复损坏内容的猜测 |
| `state/turn_commit.py` | 旧前缀校验、提取后缀、固定提交命令 | 开事务、重跑 Agent、重播 SSE |
| `state/session_db.py`、`state/schema.py` | 关系型共享查询、约束及提交事务算法 | 运行时到处散落的 backend 判断 |
| `state/backend.py` | 配置分派、连接资源所有权、小型后端策略 | 自动回退 SQLite、全局共享可变事务 |
| `state/migrations/` | 必要的版本化 schema 变更 | 每次请求中自动迁移 |
| 现有根 `exceptions.py` | 集中扩展存储领域异常 | 向 API 泄漏原始 DSN/SQL 错误 |
| `gateway/session.py`、`gateway/runs.py` | session gate、运行登记、等待提交后发布终态 | 数据库专属 SQL |

只有当共享代码足够明确时再引入基类。首批更适合 `SessionStore` 协议 + 可组合公共模块 +
一个 `SqlSessionStore`，不需要先建一棵 `BaseDatabase → BaseSQLDatabase → BaseSessionDB` 继承树。
不同业务如 future memory/cron 可以复用连接、生命周期、迁移和错误基础设施，但各自的事务命令
应单独定义，不把它们的方法持续塞进 SessionStore。

## 7. 核心事务算法与提交幂等

### 7.1 关系型提交算法

以下是逻辑步骤，不是要求每个数据库运行同一字符串 SQL：

1. 开启短写事务，查询 `(session_id, operation_id)` 的已有提交凭据。
2. 已有凭据且指纹相同：返回原回执，不修改消息、revision 或更新时间。
3. 已有凭据但指纹不同：拒绝，报告幂等键复用冲突。
4. 尚无凭据：条件更新 session，要求旧 `history_revision` 和 `message_count` 均匹配。
   同时推进 revision/count，并仅更新该命令拥有的历史字段。
5. 插入新增消息，满足 `(session_id, seq)` 和 message ID 的唯一约束。
6. 写入提交凭据与结果摘要；与前两项写入处于同一个事务。
7. 提交成功后返回回执。任何提交前失败必须回滚整个单元。

条件更新失败或唯一键竞争时，先结束当前失败事务，再在新读取单元检查是否是同一操作已提交；
同指纹重复返回原回执，否则明确区分 session 不存在、版本冲突或幂等冲突。
不能只用“先查不存在，再插入”实现幂等；并发下还需要唯一约束及正确的竞争处理。
不同 operation ID 竞争同一 revision 时至多一个成功。

PostgreSQL 的 READ COMMITTED 条件 UPDATE 会在等待竞争写入后重新检查 WHERE，适合这类 CAS。
SQLite WAL 读后升级写可能出现 BUSY_SNAPSHOT，短写事务的启动/重试需单独策略。
这不是要求所有数据库把默认隔离级别调到 Serializable，也不把 CAS 当成跨任意业务的不变量证明。
依据：[PostgreSQL 隔离](https://www.postgresql.org/docs/current/transaction-iso.html)、
[SQLite 隔离](https://www.sqlite.org/isolation.html)。

### 7.2 提交凭据属于正确性元数据，不是持久化运行系统

建议以独立技术表或等价记录保存 receipt，至少包含操作键、规范指纹、实际 revision/序号范围。
它与 sessions/messages 一起提交，但不存正在执行的任务、SSE、调度租约或可重跑步骤。
这一小项技术元数据需要在 schema 评审确认，不等同于新增完整 runs 业务表。

operation ID 可以绑定 gateway run ID，但不能绑定 SSE event ID 或 Agent 内部每次生成的新 turn ID。
也不能用 payload hash 代替 operation ID：用户连续两次问相同问题可以是两个真实操作。
同一次存储重试必须复用稳定 ID、消息 ID、序号、时间、结果和序列化内容。

指纹包括会影响写入结果的完整命令及编码版本，不包含凭据或瞬时连接状态。
外层 JSON 可以采用统一键排序；嵌套工具 arguments 是字符串，不重新解析或格式化它。
正常 receipt 生命周期至少覆盖承诺的提交重试窗口；最小方案可随会话保留并在删除时一并清理，
不复用已删除 session ID。若要清理旧 receipt 或支持删除后的历史审计，必须另定过期契约。

记录操作 ID 与业务效果同事务、同 ID 不同参数拒绝的设计，有
[AWS 幂等 API 工程说明](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)作为参考。
此处只是提交幂等，不宣称模型或外部工具 exactly-once。

### 7.3 提交结果未知不是已回滚

网络在 COMMIT 附近断开，可能是“数据库已提交，回复丢失”，也可能未提交。
不能统一当失败然后再次生成回答，不能假定错误时数据必然不存在。

处理顺序：

1. 保留原提交命令，报告持久化待确认，不发布 durable success。
2. 在当前权威库核对 receipt，或按适配器允许的方式以同一 ID 有界重试提交。
3. 匹配 receipt 才确认成功；不同指纹拒绝。一次未查到不能证明原事务绝不会稍后提交，
   后续仍必须走相同幂等键、唯一约束和 revision 保护。
4. 超过预算仍无法确认：标记该 session 写入阻塞，拒绝基于旧快照继续新 run。
   不必永远挂着一个锁或连接，但协调层要保留阻塞状态并提供明确错误。
5. 重新可用后核对权威历史再解除阻塞；进程重启只加载已提交历史，不自动重新执行旧工具。

[MongoDB 驱动事务说明](https://www.mongodb.com/docs/v8.3/core/transactions-in-applications/)
明确区分可重试整个事务的 transient error 与需要重试 commit 的 unknown commit result。
公共错误模型应保留这类差别，而不是所有数据库异常都返回 `False`。

### 7.4 对外状态是两个维度

| 模型/回合结果 | 持久化结果 | 对外含义 |
|---|---|---|
| completed / stopped / failed / partial | confirmed | 可报告该结果及已保存的历史 revision |
| 任意正常返回结果 | not committed | 不承诺已保存；可明确报告存储失败 |
| 任意正常返回结果 | unknown | 待确认，禁止当成已回滚或成功 |
| 未形成结果（强杀/直接取消等） | 无提交 | 最小模式不保证保留本轮；不能伪造完整 transcript |

“模型失败但失败历史已保存”与“模型成功但结果未保存”是不同情况。
Gateway 暂存核心 terminal event，显式等待存储，再构造外部终态；callback 仅负责进度观察。

## 8. 读取、分页与持久性的共同保证

原子写入不足以自动保证正确读取。READ COMMITTED 下分别查询 session 元数据和 messages，
两次 SELECT 可能跨过两个提交。`load_history` 必须返回同一版本的完整视图。

适配器可采用短一致快照事务、单查询，或在当前 append-only 模型下固定 message_count/seq 高水位
并验证版本和记录完整性。删除或未来历史重写仍需显式处理；不能返回一半旧、一半新的内容。
P0 应用层 gate 简化了恢复与写入的竞争，但不能作为适配器错误读法的永久替代。

建议列表先以 `(created_at, session_id)` 这样的确定顺序分页；这是排序规则，不承诺跨分页
保持整个数据库不变。若要按最近活跃时间排序，必须另外说明更新导致跨页移动的语义。
消息游标固定 session、截止 seq 与当前读取位置，不允许以客户端随意拼接的 SQL 表达分页。

持久性承诺至少区分：应用进程重启、数据库进程故障、主机掉电、集群故障转移。
首版验收覆盖已确认提交后的应用重启；后几类必须明确配置和故障模型后实测，不能默认承诺零丢失。
主库不能启用会淘汰会话的 TTL/缓存淘汰策略。读后写确认和 receipt 核对不能走未经处理的滞后副本。

## 9. 各类后端如何复用基座

| 后端族 | 可以共享 | 必须独立验证或实现 |
|---|---|---|
| SQLite、PostgreSQL | schema 意图、结构化 CRUD、CAS/receipt 算法、codec、契约测试 | 驱动、连接 PRAGMA/配置、事务启动、busy/网络失败、DDL |
| MySQL、MariaDB、TiDB、OceanBase | 关系型模块与合适的 MySQL 协议驱动基础 | 引擎/版本、类型、排序规则、DDL、冲突/重试、租户；兼容协议不是支持证明 |
| SQL Server、Oracle | 业务协议和大部分关系型查询意图 | 方言/驱动、空串、LOB、隔离、约束和迁移 |
| CockroachDB | 业务协议及部分 PG 生态工具 | 分布式事务重试；只重试存储，不重跑 LLM/工具 |
| MongoDB | DTO、codec、提交指纹、错误语义、契约测试 | 文档模型与多文档事务、索引、确认等级；不能把无限长会话随意塞进一个文档 |
| Redis | 领域契约/测试思路 | 数据结构、无回滚事务的失败策略、持久性/淘汰/分区；未证明契约前不列为主库 |
| DuckDB | 领域接口、codec | 在线负载适用性和部署模式；不是首批在线主库 |

SQLAlchemy 能统一不少 SQL 表达，但必须选择适合异步运行的驱动。共享 engine/factory，不跨并发
任务共享一个可变 AsyncSession；Core 模式每个短操作也要取得自己的 connection/transaction。
事务上下文的存在不能掩盖驱动处于 AUTOCOMMIT、语句实际没有进入预期事务的问题。
依据：[异步文档](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html)、
[连接和事务](https://docs.sqlalchemy.org/en/21/core/connections.html)。

MongoDB 多文档事务需要合适部署，standalone 不支持；Redis EXEC 后个别命令错误不会自动回滚
其他命令。这两项直接说明“CRUD 方法名相同”不足以证明共同原子提交保证。
依据：[MongoDB 部署限制](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)、
[Redis 事务](https://redis.io/docs/latest/develop/using-commands/transactions/)。

## 10. 统一初始化流程，而非统一管理员脚本

建议流程：验证配置 → 连接 → 检查后端/驱动/能力/schema → 生成行动计划 → 按策略执行 → 验证 → 服务。

- Provisioning：服务、数据库、账号、OceanBase 租户等，需要独立授权和权限。
- Bootstrap：只对指定的空应用命名空间建表/集合、索引与 schema 版本。
- Migration：从明确旧版本升级，记录状态；事务性 DDL 和隐式提交 DDL 采用不同恢复策略。
- Runtime：只使用必需的数据访问权限；不默认带建库/建用户权限。

默认不能静默执行破坏性升级。旧 schema、更新于当前程序的 schema、缺少驱动或权限，应给出
明确诊断并拒绝不安全启动。初始化需可重入，迁移需要互斥，失败后不能误标“已升级完成”。
只在初始化成功后启用运行时流量；关闭时停止接受 run，协作停止并给提交有界时间，再释放连接。

建表不是已有结构升级，跨数据库搬迁也不是结构升级。
[Alembic batch 文档](https://alembic.sqlalchemy.org/en/latest/batch.html)展示了 SQLite 某些变更的
复制重建流程；不能把一个通用 `create_all()` 当作迁移系统。

后端能力分成“必须满足的契约”和“可选扩展”。可选项包括全文/向量搜索、CDC、分布式 lease、
运行中 checkpoint、历史修订和导出导入。不能把 `atomic_commit=False` 当作仍能运行基础会话业务的选项。

## 11. 开发验收：同一份行为测试，而不只是同一个父类

每个宣称支持的后端必须执行同一套契约测试，LLM 与外部工具使用 fake。
数据库集成测试单独标记并使用隔离的测试实例，不接触用户生产库。

| 测试组 | 核心断言 |
|---|---|
| 消息 round-trip | 工具参数原文、call ID、null/缺失/空串、Unicode、refusal、失败/停止尾部不变 |
| 全量结果与增量提交 | 旧前缀保留且只存一份；前缀变化明确失败；不重复追加 user |
| CRUD 与分页 | 空会话、多个会话隔离、确定顺序、高水位、删除后不可恢复出孤儿消息 |
| 原子性故障注入 | 在消息写入、计数更新、receipt 插入任一阶段失败，不出现部分提交 |
| 幂等 | 同 ID 同命令返回原 receipt，不推进 revision；同 ID 不同命令拒绝 |
| 并发提交 | 同 revision 不同操作仅一个成功；同操作并发不会产生两份消息 |
| 结果未知 | 数据已提交但响应丢失，重试识别原结果；未确认前不发布持久化成功 |
| 一致读取 | 读者看到旧版本或新版本，不看到混合 metadata/messages |
| 改名/删除竞争 | 改名不丢失、不干扰历史 revision；删除不会被迟到提交复活 |
| 运行时终态 | completed/failed/stopped/partial 都走持久化；强制取消与协作停止分别测试 |
| SSE 断线重连 | 不新建 run、不追加第二条输入；运行任务与订阅生命周期独立 |
| 重启与初始化 | 已确认提交可恢复；配置/权限/schema 错误明确失败，不自动回退 SQLite |
| 资源与异步 | 并发任务不共享事务、连接释放、有界重试和关闭；不阻塞事件循环 |

内存 fake 适合验证 Gateway 编排，但无法代替真实数据库的约束、隔离、DDL 和断连测试。
SQLite 与 PG 同一套契约通过后才增加后端；测试结果必须注明数据库/驱动版本和配置。

## 12. 实施前应决定的最小事项

1. 采用收尾提交，还是额外承诺 durable admission？本文建议首版采用前者并明确限制。
2. 是否批准 SQLite + PostgreSQL 与 SQLAlchemy Core 作为首批方案？当前只是建议，未加依赖。
3. 是否接受独立 history/metadata 版本，以及小型技术提交凭据？需定稿其生命周期和 schema。
4. title 空值、消息大小、列表顺序、结果摘要范围、初始化策略与故障保证需要明确默认值。
5. 数据库迁移、历史重写、运行中 checkpoint、分布式调度不默认混入本轮。

建议的开发顺序是：领域 DTO/codec 与合同 → 精简 SQLite 实现 → Gateway 提交时序 →
PG 对照验证并提取有证据的共用逻辑 → 初始化与迁移验收。沿用项目 copy-then-cut 原则，
参考 Hermes 的成熟行为，但不复制与本阶段无关的上百接口。实际复制需另做固定来源与许可记录。

最终交付目标不是“大家继承同一个类”，而是“同一个运行时在任何已认证后端上，都得到相同的
会话顺序、提交结果、失败边界和恢复行为”。
