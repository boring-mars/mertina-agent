# v0.2 第二期测试证据

记录日期：2026-11-10。范围：会话存储、Gateway、Sessions/Runs API、SSE 重连、停服与重启恢复。

| 检查 | 命令 | 结果 |
|---|---|---|
| Ruff lint / format | `uv run ruff check .` / `uv run ruff format --check .` | 通过，172 文件 |
| 类型 | `uv run mypy src` | 通过，70 源码文件 |
| 单元 | `uv run pytest -m "not integration"` | 840 passed，0 skipped；[JUnit](unit.xml) |
| 集成（三后端） | `uv run pytest -m integration` | 146 passed，0 skipped；[JUnit](integration.xml) |
| 构建 | `uv build --out-dir <临时目录>` | wheel / sdist 成功 |

集成测试显式设置了 `MERTINA_TEST_POSTGRESQL_URL`、`MERTINA_TEST_MYSQL_URL`，
指向 [dev/databases](../../dev/databases/README.md) 的本地容器；无跳过即表示三种后端都实际执行。
146 项包含第一期数据库接入契约、会话存储契约、HTTP 运行与 SSE、重启恢复、停服，以及 ddgs 实时搜索。

覆盖要点：

- 会话存储：CRUD、分页、消息顺序、工具消息与 refusal 无损往返、CAS 冲突、幂等提交凭据、事务回滚。
- Gateway：同会话互斥（409）、跨会话并发、落库确认后才发布终态、停止竞态、
  SSE 断开不取消 run、按 `Last-Event-ID` 重连、游标过期要求重新同步。
- 生命周期：端口占用时启动失败并释放资源、协作与非协作 Agent 的有界停服、停服后重开数据库读取已提交历史。
- 重启恢复：两次 Gateway 生命周期之间续接含工具调用的历史。

环境：Python 3.12.13；SQLite 3.53.1；PostgreSQL 18.6；MySQL 8.0.46。所有模型调用均为 fake。

未覆盖：

- 真实模型端到端对话。最近一次真实请求被 provider 的令牌 IP 白名单拒绝（HTTP 403），
  详见 [开发记录](../../plans/v0.2-development-log.md)。
- CI workflow 已编写，但尚未在 GitHub Actions 上运行过。
- Python 3.13 只在 CI 矩阵中声明，本机未验证。
