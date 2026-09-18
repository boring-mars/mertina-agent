# 分支管理

[English](../../en/development/branching.md) | **中文**

项目采用 [GitHub Flow](https://docs.github.com/zh/get-started/using-github/github-flow)：
一个长期分支，加上若干短期的功能分支。

## 分支模型

```
main ──●────────●────────●──────→
        \      ↗ \      ↗
         feat/a    fix/b          通过 PR squash 合并，之后分支自动删除
```

- **`main`** 是唯一的长期分支，必须随时能构建、测试能通过、可以发布。
- **其他分支都是短期的。** 从 `main` 切出，通过 PR 合并回去，合并后分支自动删除。
  争取几天内合并，而不是几周。长期不合并的分支会和 `main` 越差越远，最后很难合并。
- **没有** `develop`、`staging`、`hotfix` 这类分支。紧急修复和其他改动走同样的流程。

## 分支命名

格式：`<类型>/<简短描述>`

- `类型` 与 [commit 类型](commit-convention.md#类型) 保持一致
- `简短描述` 使用小写英文单词，单词之间用连字符连接（kebab-case），简短但要表达清楚
- 可以在描述前加上 issue 编号：`fix/42-session-timeout`
- 不要使用中文、空格、大写字母或个人名字

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat/` | 新功能 | `feat/s3-session-store` |
| `fix/` | 修复 bug | `fix/42-session-timeout` |
| `docs/` | 仅文档 | `docs/development-guidelines` |
| `refactor/` | 既不修 bug 也不加功能的代码调整 | `refactor/split-agent-runner` |
| `perf/` | 性能优化 | `perf/cache-tool-schemas` |
| `test/` | 新增或修复测试 | `test/api-integration` |
| `build/` | 构建系统、打包、依赖 | `build/switch-to-uv-build` |
| `ci/` | CI 配置 | `ci/add-python-matrix` |
| `chore/` | 其他维护性工作 | `chore/project-scaffold` |

机器人（Dependabot、Renovate、release-please）创建的分支有自己的命名方式，不受此规则约束。

## 分支放在哪里

- **外部贡献者**把分支推送到自己的 fork，从 fork 提 PR。
- **协作者**可以直接把分支推送到主仓库，方便其他人 checkout 下来一起协作。
  只推送你正在做的分支。

## `main` 的保护规则

以下规则由 GitHub 强制执行，对所有人生效，包括管理员：

- 必须通过 PR 合并，直接 push 会被拒绝
- 至少需要 1 个批准，推送新 commit 后之前的批准自动失效
- 所有审查讨论都必须标记为已解决
- 必须通过指定的 CI 检查（CI 搭好之后启用）
- 要求线性历史，不允许 merge commit
- 禁止 force push 和删除

仓库设置：只启用 **squash merge**，并且开启**合并后自动删除分支**。

## 保持分支最新

优先把你的分支 rebase 到 `main` 上，而不是把 `main` merge 进你的分支：

```bash
git fetch origin            # 或者：git fetch upstream
git rebase origin/main
git push --force-with-lease
```

使用 `--force-with-lease` 而不是 `--force`，这样不会覆盖别人推送到你分支上的提交。
如果有其他人也在同一个分支上提交，rebase 之前先和他们商量。

## 维护分支

目前只用 tag 标记版本，没有发布分支。

如果将来需要在 `main` 继续开发新版本的同时，给旧的大版本打补丁（例如 `main` 在开发 `2.0`，
但还要发布 `1.4.1`），就从对应的发布 tag 切出一个维护分支：

```bash
git switch -c release/1.x v1.4.0
```

维护分支的规则：

- 命名为 `release/<主版本号>.x`
- 和 `main` 一样受保护
- 只接收 bug 修复和安全修复，不加新功能
- 修复先合入 `main`，再 cherry-pick（`git cherry-pick -x <sha>`）到维护分支
- 补丁版本从维护分支打 tag 发布
- 该版本停止维护后，分支被归档（锁定）
