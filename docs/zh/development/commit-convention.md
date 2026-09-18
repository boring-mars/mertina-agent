# 提交规范

[English](../../en/development/commit-convention.md) | **中文**

项目遵循 [Conventional Commits 1.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/)。
结构化的提交信息让历史更易读，也让工具可以自动生成变更日志、自动计算下一个版本号。

## 规范作用在哪里

因为每个 PR 都用 **squash merge** 合并，一个 PR 在 `main` 上只对应一个 commit，
而且 **PR 标题会成为这个 commit 的标题**。所以：

- **PR 标题必须遵守规范**，CI 会检查。
- 功能分支内部的 commit 信息不做检查。但仍然建议遵守规范，这对审查者有帮助，以后拆分工作也更方便。

## 格式

```
<类型>(<可选的范围>)<可选的 !>: <描述>

<可选的正文>

<可选的脚注>
```

示例：

```
feat(api): add endpoint for listing sessions

Sessions can now be listed with pagination. The default page size is 20.

Closes #37
```

## 类型

| 类型 | 含义 | 对版本号的影响（1.0 之后） |
|---|---|---|
| `feat` | 新功能 | minor |
| `fix` | 修复 bug | patch |
| `perf` | 性能优化 | patch |
| `docs` | 仅文档 | 无 |
| `refactor` | 既不修 bug 也不加功能的代码调整 | 无 |
| `test` | 新增或修复测试 | 无 |
| `build` | 构建系统、打包或依赖 | 无 |
| `ci` | CI 配置 | 无 |
| `style` | 仅格式调整，不影响代码含义 | 无 |
| `chore` | 不涉及源码和测试的其他改动 | 无 |
| `revert` | 回滚之前的提交 | 视情况而定 |

任何类型只要包含破坏性变更，都会导致主版本号升级（见[破坏性变更](#破坏性变更)）。

## 范围（scope）

范围是可选的，表示改动涉及的模块，使用小写，例如
`api`、`cli`、`storage`、`auth`、`deploy`、`docs`。每个 commit 只写一个范围。改动涉及很多模块时，不写范围。
范围列表会随代码库增长，优先复用已有的范围，不要随意新造。

## 描述

- 使用**英文**，用**祈使语气**：写 "add"，不写 "added" 或 "adds"
- 首字母小写，结尾不加句号
- 整个标题行控制在 72 个字符左右以内
- 说明改了*什么*，*为什么*改写在正文里

| ✅ 好 | ❌ 不好 |
|---|---|
| `fix(storage): retry S3 upload on throttling` | `fixed bug` |
| `feat(cli): add --region option` | `Feat: Added region option.` |
| `docs: explain local deployment with docker compose` | `update docs` |
| `refactor(agent): extract tool registry` | `refactor stuff and fix tests and bump deps` |

## 正文

- 与标题之间空一行，每行约 72 个字符换行
- 说明改动的动机，以及和之前行为的区别
- 可以用中文或英文，但推荐英文，方便所有贡献者阅读

## 脚注

- 关联 issue：`Closes #12`、`Fixes #12`、`Refs #12`
- 共同作者：`Co-authored-by: Name <email>`
- 破坏性变更：`BREAKING CHANGE: <说明>`

## 破坏性变更

如果现有用户必须做出修改才能继续正常使用，就是破坏性变更。例如删除或重命名了 API、
修改了配置项名称、存储格式不兼容、需要执行迁移等。

用以下任一种或两种方式标记：

```
feat(api)!: require API key for all endpoints

BREAKING CHANGE: anonymous access is no longer allowed. Set MERTINA_API_KEY
before upgrading.
```

务必在脚注中说明用户需要怎样迁移。

## 回滚

```
revert: feat(api): add endpoint for listing sessions

This reverts commit 1a2b3c4. The endpoint leaked sessions across tenants.
```
