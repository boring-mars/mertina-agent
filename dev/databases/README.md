# 第一期数据库开发环境

本目录提供开发基础设施；三种数据库接入适配和配置已实现，会话 schema 在第二阶段开发。
第一期范围见[开发计划](../../docs/plans/v0.2-phase-1-databases.md)。
Mertina 和测试在本地 Python 环境中运行，SQLite 使用本地文件。
从仓库根目录执行下列命令；只有 PostgreSQL/MySQL 需要 Docker Engine 和 Compose v2 或更新版本。

## 启动与连接

```bash
docker compose -f dev/databases/compose.yaml up -d --wait postgres mysql
docker compose -f dev/databases/compose.yaml ps
```

PostgreSQL/MySQL 复用本机已有镜像，首次启动若缺少镜像，Docker 会下载。
SQLite 使用本地 Python 标准库，无需镜像或独立服务。

| 后端 | 开发环境 | 本地应用连接地址 | 容器网络连接地址 |
|---|---|---|---|
| SQLite | 本地 Python 标准库 sqlite3 | `/Users/mac/Desktop/Metina/mertina_dev.sqlite3` | 不适用 |
| PostgreSQL | `postgres:18` | `127.0.0.1:55432` | `postgres:5432` |
| MySQL | `mysql:8.0` | `127.0.0.1:53306` | `mysql:3306` |

两个服务端数据库均使用库 `mertina_dev`、用户 `mertina`、密码 `mertina_local_dev`。
MySQL root 密码为 `mertina_root_local_dev`，应用应使用普通用户。
这些是公开的本地开发凭据；端口仅绑定 loopback，不用于部署环境。
端口冲突时可设置 `MERTINA_DEV_POSTGRES_PORT` / `MERTINA_DEV_MYSQL_PORT` 再执行 Compose。
镜像标签是开发基线，不代表已验证整个数据库版本系列；正式兼容范围随适配器测试确定。

项目名固定为 `mertina-agent-dev`，使用独立网络和两个命名卷：
`mertina-agent-dev_postgres-data`、`mertina-agent-dev_mysql-data`。
不复用其他项目的容器或数据库。

## SQLite 本地路径

SQLite 由 Mertina 进程直接打开文件，无需启动服务、监听端口或安装单独的数据库程序。
本机开发库使用 `/Users/mac/Desktop/Metina/mertina_dev.sqlite3`，WAL/SHM 等伴随文件也保留在该目录。
这是本机开发路径约定，通过忽略的 `.env` 中 `MERTINA_DATABASE_URL` 指定；应用默认
使用 `~/.mertina/mertina.sqlite3`，不硬编码开发机路径。

```bash
mkdir -p /Users/mac/Desktop/Metina
.venv/bin/python -m sqlite3 /Users/mac/Desktop/Metina/mertina_dev.sqlite3 'PRAGMA quick_check;'
```

需要交互查询时：

```bash
.venv/bin/python -m sqlite3 /Users/mac/Desktop/Metina/mertina_dev.sqlite3
```

开发环境按 `uv sync --locked` 安装，使用仓库的 `.venv`；不需要构建 Mertina 测试镜像。
测试不同分支时，可分别选择该目录下的不同文件，如 `v0.2-phase-1.sqlite3`，避免共用数据或 schema。
自动化测试使用临时文件，不直接写开发库。

## 环境连通性检查

```bash
docker compose -f dev/databases/compose.yaml exec -T postgres \
  psql -U mertina -d mertina_dev -v ON_ERROR_STOP=1 \
  -c 'SELECT version(), current_database(), current_user;'
docker compose -f dev/databases/compose.yaml exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql --protocol=TCP -h 127.0.0.1 -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT VERSION(), DATABASE(), CURRENT_USER(), @@character_set_database, @@default_storage_engine;"'
.venv/bin/python -m sqlite3 /Users/mac/Desktop/Metina/mertina_dev.sqlite3 'SELECT sqlite_version();'
```

应用接入检查：

```bash
uv run python -m mertina_agent.database --env-file .env
```

三后端统一契约测试位于 `tests/integration/database/`。显式提供
`MERTINA_TEST_POSTGRESQL_URL` 与 `MERTINA_TEST_MYSQL_URL` 后运行
`uv run pytest tests/integration/database`；未提供的网络后端将 skip。
配置项及 URL 格式见根目录 `.env.example`，阶段结果见
[验收记录](../../docs/testing/v0.2-phase-1/README.md)。

上述原生客户端命令仅验证数据库环境，不能替代应用接入测试。

2026-10-23 本机验证记录（Docker Compose v5.5.1，aarch64）：

| 后端 | 实际版本 | 本次验证 |
|---|---|---|
| PostgreSQL | 18.6 | 健康检查通过，连接到指定库和用户 |
| MySQL | 8.0.46 | 普通用户 TCP 连接通过，utf8mb4 / InnoDB |
| SQLite | 3.53.1（本地 Python 3.12.13） | 文件连接、quick_check、WAL、外键启用、Unicode、回滚与文件重开 |

SQLite 本地开发库已创建，未创建应用表；独立临时探针已自动清理。
原 SQLite Docker 卷 `mertina-agent-dev_sqlite-data` 已删除，Compose 已移除 SQLite 服务和卷定义。
PostgreSQL/MySQL 开发容器保持运行；没有修改原有其他项目容器。

## 停止与数据生命周期

```bash
docker compose -f dev/databases/compose.yaml stop
docker compose -f dev/databases/compose.yaml down
```

`stop` 和不带 `--volumes` 的 `down` 都保留数据，重建容器后仍可读取。
需要清空 PostgreSQL/MySQL 开发数据时，才手动执行：

```bash
docker compose -f dev/databases/compose.yaml down --volumes
```

最后一条会删除本项目两个数据库卷中的全部数据，不影响 SQLite 本地文件。
SQLite 数据由指定本地目录单独管理。不要使用全局 `docker volume prune` 清理本项目。

依据：[SQLite 无服务进程](https://www.sqlite.org/serverless.html)、
[Docker 命名卷](https://docs.docker.com/engine/storage/volumes/)、
[Python sqlite3 CLI](https://docs.python.org/3.12/library/sqlite3.html#command-line-interface)。
