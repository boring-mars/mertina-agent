# 贡献指南

[English](../../en/development/contributing.md) | **中文**

感谢你对 Mertina Agent 的关注！本指南会带你从零开始，一直走到 PR 被合并。

## 可以怎样参与

- 通过 [issue](issues-and-labels.md) 报告 bug、提出功能建议
- 改进文档，包括这份规范
- 修复 bug 或实现新功能。可以从标记为 `good first issue` 的 issue 开始
- 审查别人的 PR。非维护者的审查意见同样欢迎

## 开始之前

- **先搜索。** 看看是否已经有相关的 issue 或 PR，避免重复劳动。
- **大改动先讨论。** 新功能、架构调整或者几百行以上的改动，请先开 issue（或 Discussion）
  讨论方案，达成一致后再写代码。这样可以避免辛苦写完的 PR 最后无法被接受。
- **认领 issue。** 在 issue 下留言说明你正在做，避免别人重复做。如果中途不做了，也请说一声，方便重新分配。
- 小修改（错别字、明显的 bug、小的文档改动）可以直接提 PR。

## 开发环境

### 前置要求

- [Git](https://git-scm.com/)
- [uv](https://docs.astral.sh/uv/)：负责管理 Python 版本、虚拟环境和依赖。
  不需要单独安装 Python，uv 会自动安装项目要求的版本。

### 获取代码

外部贡献者通过 fork 参与：

```bash
# 1. 在 GitHub 上 fork boring-mars/mertina-agent，然后 clone 你的 fork
git clone https://github.com/<你的用户名>/mertina-agent.git
cd mertina-agent

# 2. 把主仓库添加为 upstream
git remote add upstream https://github.com/boring-mars/mertina-agent.git
```

有写权限的协作者可以直接 clone 主仓库，并把分支推送到主仓库。

### 安装与检查

```bash
uv sync                    # 创建 .venv 并按 uv.lock 安装所有依赖
uv run pre-commit install  # 安装 git 钩子（每次提交前自动 lint 和格式化）
uv run pytest              # 运行测试
uv run ruff check .        # 代码检查
uv run ruff format .       # 格式化
uv run mypy src            # 类型检查
```

如果在最新的 `main` 上 `uv sync` 失败或测试不通过，请开 issue。这是项目的 bug，不是你的环境问题。

## 工作流程

```
main ──●──────────────●──────→
        \            ↗
         feat/xxx ──●──●        squash 合并后分支自动删除
```

1. **更新 `main`。**
   ```bash
   git switch main
   git pull upstream main     # 协作者用：git pull origin main
   ```
2. **创建分支。** 命名规则见[分支管理](branching.md)。
   ```bash
   git switch -c feat/short-description
   ```
3. **修改代码。** 一个分支只做一件事，同时补充或更新测试和文档。
4. **提交。** 分支内的 commit 信息格式不做强制要求，因为 PR 最终会被 squash。
   但清晰的提交信息对审查者很有帮助，参见[提交规范](commit-convention.md)。
5. **保持分支最新。** 如果 `main` 有了新提交，用 rebase 同步：
   ```bash
   git fetch upstream
   git rebase upstream/main
   git push --force-with-lease
   ```
   在自己的功能分支上 force push 没问题，但在 `main` 上永远不允许。
6. **推送并创建 PR。**
   ```bash
   git push -u origin feat/short-description
   ```
   按 PR 模板填写说明，参见 [Pull Request 规范](pull-requests.md)。
7. **回应审查意见。** 继续向同一个分支推送新的 commit，PR 会自动更新。
8. **合并。** 获得批准且 CI 通过后，由维护者（如果你有写权限，也可以是你自己）squash 合并。
9. **清理本地分支。**
   ```bash
   git switch main
   git pull --prune
   git branch -d feat/short-description
   ```

## 许可证与署名

- 本项目使用 [MIT 许可证](../../../LICENSE)。提交贡献即表示你同意以相同的条款授权你的贡献。
- 只提交你自己编写的代码，或者你有权以兼容许可证提交的代码。
- Mertina Agent 基于 Hermes Agent 改造而来。从 Hermes Agent 或其他项目复制、改编代码时，
  必须保留原有的版权和许可证声明，并在 PR 说明中注明代码来源。
- 引入与 MIT 不兼容的许可证（例如 GPL、AGPL）的依赖前，必须先讨论。

## 行为准则

保持尊重和建设性。评论代码，不评论人。默认对方是善意的，并且记住很多贡献者是利用业余时间、
甚至用非母语参与项目的志愿者。对骚扰他人的行为，维护者可以隐藏评论、锁定讨论或屏蔽用户。

## 获取帮助

- 对代码或流程有疑问，可以开 issue 或 Discussion。
- 如果你的 PR 一周都没有回应，欢迎在 PR 里提醒维护者。
