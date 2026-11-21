# 会话持久化数据库调研：Hermes 方案与 12 种数据库

> 范围更新（2026-10-23）：用户已确定第一期仅做 SQLite、PostgreSQL、MySQL 接入适配。
> 以[第一期计划](../../plans/v0.2-phase-1-databases.md)为准；本文保留原调研结论和候选业务设计。

调研日期：2026-10-23（Asia/Shanghai）。状态：研究参考，不是已批准的实现规格。

本文整理前期讨论及用户提供的调研文本，补充 TiDB、OceanBase 社区版，并复核主要 GitHub 状态。
运行时业务契约与事务边界另见[会话存储公共基座研究](session-storage-common-foundation.md)。

## 1. 范围、结论与证据边界

调研对象是 sessions/messages 的权威持久化后端：选择一个数据库后，会话读写、列表与恢复都使用它。
不是长期记忆插件、向量库、模型调用数据库工具，也不是 SQLite 主库之外的镜像双写。

主要结论：

1. 本次核验的 Hermes 官方主干仍直接使用 SQLite；存在真实的社区 PostgreSQL 替换实现，
   但不能把提交到官方仓库的未合并 PR 当作官方已支持。
2. 可以统一业务接口、配置装配和初始化流程；无法让所有数据库共享同一份原始 SQL、DDL
   或不加区分的事务策略。
3. 建议先用 SQLite 与 PostgreSQL 检验公共契约，再按需求扩展。12 种数据库是比较样本，
   不是 v0.2 承诺的支持列表；其中 Oracle、SQL Server 也不因进入比较表而变成开源产品。
4. 一个启动配置选择一个会话主库。连接失败不自动改写到 SQLite；切换连接配置不自动搬迁历史。

证据级别：读取官方文档、GitHub 源码与测试配置，并复核 PR 的 open/closed、merged 状态。
没有安装候选项目、运行其测试、实测迁移或压测。因此“存在实现/测试”不等于“已在 Mertina 验证”。
未找到某一方案不等于证明整个 GitHub 不存在该方案。链接中的 `stable`、`current` 和 PR 状态会变化。

### 与当前项目范围的关系

[ROADMAP v0.2](../../../ROADMAP.md#v02--gateway-and-sessions)原本要求：单进程、单用户、
localhost API、同会话串行、SQLite sessions/messages、重启后恢复历史，以及运行中 SSE 重连。
多数据库是用户提出的扩展方向，尚不意味着多实例写入、持久化任务调度、运行中崩溃续跑或云部署。
本次不修改 ROADMAP、README、原开发计划或代码，也不替代后续技术决策评审。

## 2. Hermes 官方与社区方案

### 2.1 官方主干

本次核验的官方 HEAD 为 `41dfcf2c1bd7146d4ad46aea08b7dd007900602e`，提交时间为
2026-10-22 UTC。[固定版本的 SessionDB](https://github.com/NousResearch/hermes-agent/blob/41dfcf2c1bd7146d4ad46aea08b7dd007900602e/hermes_state.py#L464)
仍绑定 SQLite；源码说明了 WAL、FTS5 等实现。

这与本项目早期复制 Hermes 时使用的固定基线不是同一个版本。调研新主干不意味着自动升级
Mertina 的代码来源；实际复制时仍须固定 SHA、保留许可，并记录有意的行为差异。

### 2.2 可借鉴的真实实现与接口提案

| 方案 | 核验状态 | 实际范围 | 对 Mertina 的价值与限制 |
|---|---|---|---|
| [ykoh42/async-hermes-agent](https://github.com/ykoh42/async-hermes-agent) | 独立 MIT 开源发行版；源码存在 PG 实现 | 显式创建 PostgreSQL SessionDB 并注入 Agent；SQLAlchemy Core 异步实现 | 与异步 Agent 接近，但不是可直接装进上游的通用插件；其 library 发行范围不等于完整上游 CLI/Gateway 产品 |
| [PR #120561](https://github.com/NousResearch/hermes-agent/pull/120561) | Open，未合并 | 可选 PG 会话、消息、搜索、恢复、usage；SQLite→PG 迁移 | 可研究后端工厂及会话存储边界；本 PR 不负责将所有独立业务存储一并迁走 |
| [PR #118142](https://github.com/NousResearch/hermes-agent/pull/118142) | Open，未合并 | 分能力接口、后端工厂、PG、广泛运行路径接入；配置错误不静默回退 | 可研究接口拆分和失败策略；改动范围远大于 v0.2 |
| [PR #88889](https://github.com/NousResearch/hermes-agent/pull/88889) | Open，未合并 | 对已知 SQLite SQL 集合做 PG 翻译，搜索和锁等单独处理 | 适合兼容大量旧 SQL；翻译规则与例外会持续增加，并非任意 SQL 通用转换器 |
| [PR #39301](https://github.com/NousResearch/hermes-agent/pull/39301) | Open，未合并 | 集中选择后端；迁移时隔离 writer、复制与校验后切换 | 可参考迁移切换流程；正式采用前需重新核验当前状态与显式 db_path 路径 |
| [PR #71945](https://github.com/NousResearch/hermes-agent/pull/71945) | Open，未合并 | 从旧 SessionDB 抽取 126 个抽象方法 | 是接口重构，不含第二种数据库实现；不能直接作为精简 v0.2 接口清单 |
| [RFC #23717](https://github.com/NousResearch/hermes-agent/issues/23717) | Open，needs-decision | 提议 SQLite/PG/MySQL session provider | 是讨论，不是当前配置规范或官方交付承诺 |

本次再次查询 GitHub API 复核了上述 PR 和 RFC 的状态。
对应 head SHA 分别为：

- #120561：`b7ebae099b2e20a05de305eadff55e8f28d8b448`。
- #118142：`03b160ef1a9dd9c5f7c9919010703b63a899f4ee`。
- #88889：`b836d44f98492a93fde182020424e357501b5aa1`。
- #39301：`43e9c1081f75bb81500e3f4468feae24a4d8867a`。
- #71945：`37df4b6bb1d89a16942ea4aa15dff4f81c537548`。

#### 独立异步实现的证据

本次固定 `ykoh42/async-hermes-agent` 的 HEAD 为
`71d9c845e4a47f4c365e5a2528d6e87253fea06b`：

- [README 会话示例](https://github.com/ykoh42/async-hermes-agent/blob/71d9c845e4a47f4c365e5a2528d6e87253fea06b/README.md#sessions)
  使用 `hermes_state_postgres.SessionDB` 和 `AIAgent(session_db=db)`，属于替换所注入的会话存储，
  不是默认 SQLite/PG 双写；其他独立 memory/plugin 存储不因此自动迁移。
- [PostgreSQL 源码](https://github.com/ykoh42/async-hermes-agent/blob/71d9c845e4a47f4c365e5a2528d6e87253fea06b/hermes_state_postgres.py)
  使用 SQLAlchemy Core、异步 engine 和 psycopg，并有 schema 版本、迁移锁和 PG 专属搜索实现。
- [CI 配置](https://github.com/ykoh42/async-hermes-agent/blob/71d9c845e4a47f4c365e5a2528d6e87253fea06b/.github/workflows/ci.yml)
  包含 PG 15–18 服务矩阵及 SQLite/PG 对比、恢复、迁移等测试入口。

这些是源码和测试配置证据，不是本次执行结果，也未核实当前源码与某个 PyPI wheel 完全一致。
尤其不应为了利用它而把我们不需要的 lineage、搜索、压缩、委派、分布式租约全带进 v0.2。

#### 原生后端与 SQL 翻译路线

[#120561 的工厂](https://github.com/abulohatech/hermes-agent/blob/b7ebae099b2e20a05de305eadff55e8f28d8b448/hermes_state_backend.py)
区分 SQLite/PG；其 PG 实现采用专用 schema、驱动及维护操作。
[#88889 的翻译层](https://github.com/adamkaplan/hermes-agent/blob/b836d44f98492a93fde182020424e357501b5aa1/hermes_state_postgres.py)
则兼容旧 SQLite 语句，包括占位符和特定写入语法。

研究判断：Mertina 尚未建立庞大的存储层，优先在业务接口下共享结构化关系型查询，
通常比继续扩大 SQL 字符串翻译器更容易明确契约；最终依赖选型仍需评审。

### 2.3 不能混为主库替换的方案

| 方案 | 核验情况 | 排除原因 |
|---|---|---|
| [MySQL/MariaDB #86556](https://github.com/NousResearch/hermes-agent/pull/86556) | Closed，未合并；本次复核 | SQLite 仍是运行时真源，MySQL/MariaDB 为 best-effort 镜像 |
| [PG hot shadow #77065](https://github.com/NousResearch/hermes-agent/pull/77065) | Open，未合并；本次复核 | SQLite 权威不变；PG shadow 不负责正常历史恢复 |
| [mongodb_hermes_memory](https://github.com/alexbevi/mongodb_hermes_memory) | 社区记忆 provider | 保存记忆或捕获 turns 不等于替换 SessionDB、完整历史和 resume |
| [MySQL memory #18874](https://github.com/NousResearch/hermes-agent/pull/18874)、[Turso memory #51591](https://github.com/NousResearch/hermes-agent/pull/51591) | 记忆方向候选 | 不能据此宣称会话主库支持 MySQL/Turso |

本轮最有实质参考价值的是 PostgreSQL。没有核实到成熟的 MySQL/MongoDB 完整会话主库替换项目，
不作“整个社区不存在”的断言；TiDB/OceanBase 也只是后端候选，不是已找到 Hermes 现成适配插件。

## 3. 十二种数据库的差异

以下侧重在线会话负载：少量元数据 CRUD、有序消息追加、完整历史读取、短事务和重启后恢复。
“建议”是结合本项目阶段作出的判断，不是厂商结论，也不按市场热度排名。

| 数据库 | 形态与主要差异 | 初始化、事务和适配重点 | 阶段建议 |
|---|---|---|---|
| [SQLite](https://www.sqlite.org/whentouse.html) | 嵌入式文件型关系库，同文件同时一个 writer；WAL 并不意味着所有 reader 都被 writer 阻塞 | 文件权限、外键、busy timeout、journal/synchronous 策略、schema 版本 | 本地默认候选 |
| [PostgreSQL](https://www.postgresql.org/docs/current/transaction-iso.html) | 服务端关系库，MVCC；隔离级别与并发条件更新需明确 | 连接池、账号/schema 权限、短事务；自增序列不代表提交顺序 | 首个远端候选 |
| [MySQL](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html) | 服务端关系库；使用 InnoDB 的事务能力，不能混同不同引擎 | 引擎、utf8mb4、排序规则、事务模式和迁移；DDL 可隐式提交 | 后续常见后端候选 |
| [MariaDB](https://mariadb.com/docs/server/reference/data-types/string-data-types/json) | 与 MySQL 接近但不等价，JSON 是 LONGTEXT 别名 | 独立版本/驱动验证，不能因 MySQL 测试通过就宣称支持 | 需求驱动 |
| [SQL Server](https://learn.microsoft.com/en-us/sql/relational-databases/sql-server/transaction-locking-and-row-versioning-guide?view=sql-server-ver17) | 企业关系库；T-SQL、锁及行版本配置与并发有关 | 驱动、类型、隔离和持久性配置，分页/DDL 方言 | 企业用户有需求再做 |
| [Oracle Database](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/Nulls.html) | 企业关系库；空字符串按 NULL 处理 | schema/账号、LOB 类型、空值语义；DDL 隐式提交 | 企业用户有需求再做 |
| [CockroachDB](https://www.cockroachlabs.com/docs/stable/transaction-retry-error-reference) | 分布式关系库，PG 协议兼容不等于行为完全等价 | 可重试事务冲突、幂等、schema 变更；重试不得包括 LLM/工具 | 分布式需求驱动 |
| [MongoDB](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/) | 文档库；单文档原子，多文档事务依赖副本集/分片部署 | 独立文档模型、唯一索引、事务与读写确认；standalone 不支持跨文档事务 | 单独适配，不塞进 SQL 模板 |
| [Redis](https://redis.io/docs/latest/develop/using-commands/transactions/) | 内存数据结构库；事务没有 SQL 式回滚，持久性依配置而异 | AOF/RDB、淘汰策略、CAS/脚本、Cluster 同槽限制 | 首批不作为唯一会话主库 |
| [DuckDB](https://duckdb.org/docs/2.0/connect/concurrency) | 分析型数据库；原生文件模式与当前 Quack 服务模式并发边界不同 | 明确部署模式、冲突处理与在线小事务适用性 | 会话分析优先于在线会话主库 |
| [TiDB](https://github.com/pingcap/tidb) | 中国大陆团队自主研发的分布式关系库，MySQL 协议/常用语法兼容，事务与分析混合负载 | 复用适当 MySQL 驱动，但独立验证 DDL、隔离、冲突、排序规则 | 已有 TiDB 基础设施的用户优先 |
| [OceanBase 社区版](https://github.com/oceanbase/oceanbase) | 国内自主研发的原生分布式关系库，多租户与分布式事务，按 MySQL 兼容模式研究 | 租户、权限和资源配置与应用建表分离；独立验证方言、类型与事务 | 已有 OceanBase 基础设施的用户优先 |

补充依据：

- SQLite WAL 的并发和读快照见[隔离文档](https://www.sqlite.org/isolation.html)。不要把单 writer
  曲解为必然损坏、不能并发读取或“SQLite 不可靠”。
- [MySQL 原子 DDL](https://dev.mysql.com/doc/refman/8.0/en/atomic-ddl.html)、
  [MariaDB 隐式提交](https://mariadb.com/docs/server/reference/sql-statements/transactions/sql-statements-that-cause-an-implicit-commit)、
  [Oracle 提交行为](https://docs.oracle.com/en/database/oracle/oracle-database/19/tdddg/committing-transactions.html)
  说明“DDL 单次操作原子”不能推导成“整个迁移可放在普通业务事务里回滚”。
- Redis 的数据丢失窗口取决于[持久化策略](https://redis.io/docs/latest/management/persistence/)，
  多键原子操作还要考虑[Cluster hash slot](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)。
  同样，关系库也不能在开启异步/延迟提交后仍宣称具有相同的掉电保证。

### 3.1 国内开源数据库的边界

TiDB 由 PingCAP／平凯星辰自主研发，官方仓库采用 Apache-2.0。
见[官方中文介绍](https://docs.pingcap.com/zh/)、[公司背景](https://pingkai.cn/about/)及
[许可证](https://github.com/pingcap/tidb/blob/master/LICENSE)。
其[MySQL 兼容说明](https://docs.pingcap.com/tidb/stable/mysql-compatibility/)和
[事务说明](https://docs.pingcap.com/tidb/stable/transaction-overview/)列出差异；DDL 自动提交、不能回滚，
所以不能仅改 MySQL DSN 就宣称完成适配。

OceanBase 起源于阿里、蚂蚁团队的自主研发，见[官方发展历程](https://www.oceanbase.com/en/docs/common-oceanbase-database-standalone-1000000003577447)。
本次核验官方仓库[当前许可证](https://github.com/oceanbase/oceanbase/blob/master/LICENSE)为 Apache-2.0；
历史版本许可可能不同，实际采用时以目标 tag 为准。
本研究限定社区版及明确版本的 MySQL 兼容模式；不要把商业/云版本的 Oracle 兼容能力直接算入它。
见[MySQL 兼容性](https://en.oceanbase.com/docs/common-oceanbase-database-10000000001228196)及
[社区版租户接入说明](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_06_using_ob_for_business_development/using_mysql_for_database_development)。

## 4. 共用层与差异层

共用的是业务数据和保证：稳定 ID、明确消息顺序、完整 payload、原子提交、版本冲突、幂等、
明确的持久性确认与错误语义。不能仅暴露 `execute(sql)` 然后让业务到处判断后端类型。

建议分为：

1. 领域契约：`SessionStore`，不暴露数据库连接、SQL 或驱动异常。
2. 公共模块：消息 codec、提交命令/回执、分页规则、领域校验、错误分类、后端装配。
3. 关系型共享实现：若选用 SQLAlchemy Core，由结构化查询和方言处理常见语法差异。
4. 后端策略：连接初始化、迁移、事务启动/重试、约束行为、类型和能力检查。
5. 非关系型适配：MongoDB 等使用独立物理模型，满足相同业务契约后再声明支持。

[SQLAlchemy 方言](https://docs.sqlalchemy.org/en/21/dialects/index.html)覆盖多种关系库，
但不自动补足业务一致性、所有驱动的异步能力或每种数据库的迁移语义。
全文检索、向量检索、数据库内 JSON 查询等可独立扩展，不进入 v0.2 最小公共接口。

## 5. 初始化、结构升级与历史迁移

可以统一流程：配置校验 → 连接 → 版本/能力/权限检查 → 经策略允许的初始化或升级 → 健康检查。
不能统一成一个拥有无限权限、什么都做的 `init()`。

| 层次 | 动作 | 建议权限与策略 |
|---|---|---|
| Provisioning | 安装服务、建集群/租户/数据库/账号 | 单独入口、明确授权，不作为普通启动的默认动作 |
| Bootstrap | 在指定目标创建应用表/集合、索引与版本记录 | 对空的应用命名空间可提供显式自动初始化 |
| Migration | 升级已有结构和必要的数据转换 | 版本化、互斥、可检测失败；破坏性升级不能静默执行 |
| Runtime | 正常读写业务数据 | 最小权限，不要求管理员权限或执行 DDL |

[Alembic SQLite 批量迁移](https://alembic.sqlalchemy.org/en/latest/batch.html)说明某些变更需重建表和复制数据；
共同的是迁移目标，不一定是语句。`create_all()` 不能代替已有 schema 的升级。

跨数据库历史迁移是独立流程：停止/隔离 writer → 导出规范格式 → 导入目标 → 校验数量、内容、
顺序和可回放性 → 切换配置 → 保留可恢复源。不要在连接失败时自动迁移或生成第二个主库。

配置采用公共项加后端专属项：超时、初始化策略等共用；SQLite 路径或服务端地址、TLS、凭据、
连接池、租户等按类型验证。日志不得输出密码、完整 DSN、消息 payload 或原始 SQL 参数。

## 6. v0.2 建议与下一步决策

建议先评审[公共基座研究](session-storage-common-foundation.md)里的运行时契约，再决定实现名单。
优先顺序建议为：SQLite → PostgreSQL 的契约验证 → 有需求时 MySQL/MariaDB/TiDB/OceanBase，
而不是一次承诺十二个后端。

待确认的问题：

- 是否在 v0.2 正式扩展 ROADMAP，交付 PostgreSQL，还是只保留抽象并交付 SQLite？
- 接受运行中的本轮仅在收尾后提交，还是要求用户输入一经接受就持久化？后者会增加 admission 设计。
- 初始化是否仅允许空库 bootstrap？升级是否采用单独命令？
- 支持的数据库/驱动最低版本与实际故障保证是什么？
- 历史迁移是否纳入本版交付，还是先保留独立导出导入设计？

本文没有作出以上产品决策，也没有更改依赖或引入第三方实现。
