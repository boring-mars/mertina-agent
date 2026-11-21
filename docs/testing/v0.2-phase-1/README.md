# v0.2 第一期测试证据

记录日期：2026-10-23。阶段一的接入层完成基础验收，阶段二尚未完成。

| 检查 | 命令 | 结果 |
|---|---|---|
| 原有基线 | `.venv/bin/pytest -m 'not integration'` | 599 passed，1 deselected；[JUnit](baseline.xml) |
| 阶段一单元回归 | `.venv/bin/pytest -m 'not integration'` | 625 passed，25 deselected；[JUnit](stage-1-unit.xml) |
| 三数据库真实契约 | `.venv/bin/pytest tests/integration/database -q` | 24 passed，无跳过；[JUnit](database.xml) |
| Ruff lint / format | `.venv/bin/ruff check .` / `format --check .` | 通过，134 文件 |
| 类型 | `.venv/bin/mypy src` | 通过，52 源码文件 |
| 构建 | `uv build --out-dir /private/tmp/mertina-v02-stage-1-dist` | wheel / sdist 成功 |
| 本机配置 | `.venv/bin/python -m mertina_agent.database --env-file .env` | SQLite connection healthy |

测试源码位于 `tests/unit/database/` 与 `tests/integration/database/`。
网络测试显式设置 `MERTINA_TEST_POSTGRESQL_URL`、`MERTINA_TEST_MYSQL_URL`；
没有设置时会 skip，对外声明三后端通过必须使用无跳过运行结果。
目标是项目独立的 `mertina_dev` 数据库，测试仅创建并清理随机 `mertina_probe_*` 表。
SQLite 使用 pytest 临时目录，不向 Desktop 开发库写入测试数据。

共同验证：提交、异常回滚、取消回滚、超时回滚、Unicode/参数绑定、唯一约束、
外键、并发事务、关闭后拒绝新操作、关闭排空、错误鉴权/文件路径及重新连接后的数据。
MySQL 额外核对 utf8mb4/InnoDB；PostgreSQL 核对隔离级别；SQLite 核对 PRAGMA。

环境：Python 3.12.13；SQLite 3.53.1；PostgreSQL 18.6；MySQL 8.0.46。
这些结果不证明 Gateway、会话业务或真实模型可用。最新真实模型请求被 provider 的令牌 IP
白名单拒绝（HTTP 403），详情见[开发记录](../../plans/v0.2-development-log.md)。
