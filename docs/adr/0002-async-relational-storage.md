# 0002：异步关系型接入与第二阶段存储契约

日期：2026-10-23。状态：按用户本轮两阶段实现指令采用，待代码 PR 评审。
扩展 [ADR 0001](0001-database-backends.md) 的实施范围；本期继续实施 Gateway 计划。

## 技术选择

采用 SQLAlchemy Core 的 async engine，共享短事务和结构化查询，不引入 ORM。
SQLite 使用 aiosqlite（线程执行文件 I/O），PostgreSQL 使用 asyncpg，MySQL 使用 asyncmy。
MySQL 8 的 caching_sha2_password 未命中认证缓存时需要 cryptography 完成 RSA 握手，
因此显式声明此依赖，而不是依赖开发库的认证缓存偶然可用。

本次解析版本及许可证：SQLAlchemy 2.1.6（MIT）、aiosqlite 0.22.1（MIT）、
asyncpg 0.32.0（Apache-2.0）、asyncmy 0.2.17（Apache-2.0）、cryptography 50.0.2
（Apache-2.0 OR BSD-3-Clause）。许可证已核对安装包发行元数据；精确版本见 uv.lock。

参考：[SQLAlchemy 异步 Core](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)、
[asyncpg](https://pypi.org/project/asyncpg/)、[aiosqlite](https://pypi.org/project/aiosqlite/)、
[asyncmy](https://pypi.org/project/asyncmy/)。

## 接入边界

- `database/` 管理连接配置、池、超时、事务与异常；三个后端独立子包维护专属策略。
- Settings 中只校验配置，不加载 SQLAlchemy 或驱动，Agent 不依赖数据库运行时。
- `MERTINA_DATABASE_URL` 为 SecretStr；SQLAlchemy 不 echo，不记录参数；CLI 只显示安全错误。
- PostgreSQL/MySQL 默认使用 READ COMMITTED；MySQL 要求 InnoDB/utf8mb4。
- SQLite 文件使用 WAL、FULL synchronous、foreign_keys 与 busy_timeout；写事务 BEGIN IMMEDIATE。
- 不自动重试写操作；提交时失败以 unknown 报告，业务层用操作 ID 和提交凭据核实。
- 关闭先拒绝新工作、等待有界事务退出，再释放连接池；取消向上传播并回滚。
- SSL 开关使用系统信任根和主机校验；本地 Docker 可关闭 TLS。

## 第二阶段采用的计划选项

- 业务层保留 `state/`、`gateway/` 与 aiohttp 结构，使用 SessionStore 协议。
- 原子提交正常收尾的完整轮次增量；模型运行期间不持有数据库事务。
- 两张业务表 sessions/messages，另加 schema 版本和幂等提交凭据技术表。
- 初始化只在显式命令或显式 bootstrap 配置下执行；版本不匹配时拒绝启动，不自动破坏性升级。
- 同会话进程内互斥；运行记录和有界 SSE 缓存驻内存；重启恢复已提交历史。
- 模型和 prompt 使用当前启动配置；不导入 Hermes 旧库，不跨库迁移，不恢复运行中的模型调用。

阶段二以上契约尚待实现和测试，不能以本 ADR 代替验收。
