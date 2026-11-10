# Hermes Gateway 与会话存储来源与删减记录

整理日期：2026-11-10。适用范围：Mertina v0.2 第二阶段（会话存储、Gateway、Sessions/Runs API、SSE）。
实施范围以 [Gateway 计划](../plans/v0.2-gateway-sessions.md) 和 [ADR 0002](../adr/0002-async-relational-storage.md)
为准；本文记录代码来源及有意改变的行为，验收结果见 [开发记录](../plans/v0.2-development-log.md)。

## 固定来源与许可

- 上游：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。
- 固定提交：`4cefeed7debc7091ed65240cbc7e2c36435c0b6b`，与 [v0.1 核心循环来源](hermes-agent-core.md) 相同。
- 许可证：MIT License，`Copyright (c) 2025 Nous Research`，全文见
  [LICENSES/Hermes-Agent-MIT.txt](../../LICENSES/Hermes-Agent-MIT.txt)。
- 各模块文件头写明了来源提交、版权与本文链接。本阶段不混用其他 Hermes 版本；
  [数据库调研](reference/session-database-landscape.md) 中核验的新主干只作研究参考。

证据边界：本文依据实现时的模块头说明、[Gateway 计划](../plans/v0.2-gateway-sessions.md) 第 2 节中对固定提交的核对，
以及当前代码行为整理。整理时本机没有上游参考 clone，未重新逐行比对上游；后续触及这些模块时，
应在能访问上游时补充符号级行号核对。

## 迁移原则

- **保留职责，不搬实现细节。** 上游 Gateway 面向多平台、多 profile，SessionDB 直接绑定 SQLite。
  Mertina 保留"谁拥有 run、何时加载历史、如何互斥、如何关停"的职责划分，
  存储层按 [ADR 0001](../adr/0001-database-backends.md) 用 SQLAlchemy Core 为三种后端重写。
- **Handler 不写 SQL。** 上游 HTTP handler 存在直接 SQL 与私有写入调用；
  Mertina 的路由只调用公开的 `SessionStore` 协议。
- **终态以落库为准。** Agent 的终态回调先于返回结果发生，不能代表历史已保存；
  Gateway 在提交确认后才发布 `run.completed` / `run.stopped` 等终态。

## 模块对照

| Mertina 模块 | Hermes 来源（固定提交） | 保留 | 删减与改变 |
|---|---|---|---|
| `gateway/run.py` `GatewayRunner` | `gateway/run.py` 及启动、停服相关职责 | 按顺序装配资源、启动监听、有界排空后关闭 | 删除平台连接、profiles、kanban、relay、scale-to-zero；启动失败按逆序释放数据库与 HTTP 资源 |
| `gateway/turn_lease.py` | `gateway/turn_lease.py` | 按身份校验释放、原子发布持有者 | 删除等待者队列、轮换别名、空闲条目；P0 遇到占用立即拒绝（HTTP 409）。仅限单事件循环的进程内互斥，不是分布式锁 |
| `gateway/runs.py` `RunRegistry` | API server 中 run 的归属与停服职责 | 后台任务独立于 HTTP 连接，停止请求走协作中断 | 新增：读取历史前先取得 lease、有界 run 保留与淘汰、落库凭据核对、停服时区分"已持久"与"需核对" |
| `gateway/run_events.py` | `api_server_runs.py` 的 run transport | 事件按 run 内序号递增 | 上游是破坏性队列，断开即清理；Mertina 改为有界回放缓存加多订阅者广播，回放与实时接入在同一事件循环内原子衔接 |
| `gateway/platforms/api_server.py` | `gateway/platforms/api_server.py` | Sessions 路由路径与方法 | 删除其余平台/API 功能；请求体大小与读取超时受限；错误统一映射为不泄漏 DSN、SQL 和堆栈的安全响应 |
| `gateway/platforms/api_server_runs.py` | `gateway/platforms/api_server_runs.py`，重点是 `_handle_run_events` | Runs 路由、SSE 输出、心跳 | 断开时只释放本订阅者，不删除 run 或回放状态，因此支持按 `Last-Event-ID` 重连；游标过期时明确要求重新同步 |
| `gateway/session.py` | 无直接对应 | — | Mertina 新增：每个 run 新建一个独立 Agent，避免构造时固定的 `event_callback` 和缓存的 system prompt 跨 run 复用；工具 registry 显式注册，不使用全局单例 |
| `gateway/api_models.py` | 无直接对应 | — | Mertina 新增：严格的 HTTP 输入校验和可移植的响应序列化 |
| `state/schema.py` | `hermes_state_common.py` 的 `SCHEMA_SQL`、`hermes_state_schema.py` | sessions / messages 两张业务表与 schema 版本元数据 | 用 Core 为三种后端重建；新增 `commits` 凭据表支撑幂等提交；删除 FTS、usage 等其余表；MySQL 使用 InnoDB、utf8mb4_bin 与 LONGTEXT |
| `state/session_db.py` | `hermes_state.py` `SessionDB`、`hermes_state_sessions.py`、`hermes_state_messages.py` | 会话 CRUD、有序读取消息、按 ID 加载完整历史 | 新增：基于 `history_revision` 的 CAS、校验旧前缀后只写本轮增量、按操作 ID 与指纹去重的提交凭据 |
| `state/store.py`、`state/models.py` | 无直接对应 | — | Mertina 新增：不依赖驱动的业务协议和不可变记录 |
| `state/serialization.py` | `hermes_state_messages.py` 的消息存取 | 消息按会话内序号保存与恢复 | 改为带版本号的完整 JSON 编码，复用 `agent/transports/types.py` 校验，保留工具调用、refusal、缺失字段与 null 的区别 |
| `state/__main__.py` | 无直接对应 | — | Mertina 新增：显式 schema 初始化与校验命令；不自动执行破坏性升级 |

## 明确未迁移

- `hermes_cli/session_recovery.py`：它用于抢救损坏的 SQLite 文件，不是按 ID 续聊的入口；
  正常恢复由 `SessionStore.load_history()` 承担。ROADMAP 原表格中的这个参考已不再使用。
- 上游的数据库 lease、JSONL 等额外存储路径、聊天平台会话识别、全文搜索、usage 统计。
- 运行中崩溃后的续跑、持久化 run 队列和跨进程协调，属于 P1。

## 与上游行为的有意差异

1. **重连。** 上游 SSE 断开会清理 run transport，无法重新附着；Mertina 断开只影响订阅者本身。
2. **终态顺序。** 上游以 Agent 结束为终态；Mertina 以提交确认为终态，提交结果不明时标记 `reconciliation_required`，
   且不释放该会话的 lease，避免在未核实的历史上开启新一轮。
3. **并发策略。** 上游 lease 可以排队等待；Mertina P0 不排队，同会话第二个 run 直接返回冲突。
4. **存储后端。** 上游只支持 SQLite；Mertina 用同一套契约测试覆盖 SQLite、PostgreSQL、MySQL。
