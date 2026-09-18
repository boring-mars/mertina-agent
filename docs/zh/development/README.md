# 开发规范

[English](../../en/development/README.md) | **中文**

本目录是 Mertina Agent（Hermes Agent 的云端版本）的开发规范。
所有参与项目的人，包括维护者，都遵守这些规范。

## 文档列表

| 文档 | 内容 |
|---|---|
| [贡献指南](contributing.md) | 从这里开始：环境搭建、完整的贡献流程 |
| [项目治理](governance.md) | 角色、权限、决策方式、如何成为协作者 |
| [分支管理](branching.md) | 分支模型、分支命名、`main` 的保护规则 |
| [提交规范](commit-convention.md) | Conventional Commits 格式、类型和示例 |
| [Pull Request](pull-requests.md) | 如何创建、更新和合并 PR |
| [代码审查](code-review.md) | 审查关注什么、作者和审查者如何配合 |
| [编码规范](coding-style.md) | Python 风格、类型标注、项目结构、依赖管理 |
| [测试](testing.md) | 测试目录结构、什么必须测试、如何运行测试 |
| [版本与发布](versioning-and-release.md) | 语义化版本、tag、变更日志、发布流程 |
| [Issue 与标签](issues-and-labels.md) | 如何报告 bug、提需求，以及 issue 如何分流处理 |
| [安全](security.md) | 密钥管理、漏洞报告、云服务的安全规则 |

## 核心规则速览

1. `main` 是唯一的长期分支，必须随时能构建、测试能通过。
2. 任何人（包括维护者）都不能直接 push 到 `main`，所有改动都通过 PR 合并。
3. 分支命名为 `<类型>/<简短描述>`，例如 `feat/s3-session-store`。
4. PR 标题遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `feat: add S3 session store`。
5. 每个 PR 至少需要一位非作者的批准，并且 CI 必须通过。
6. 只允许用 **squash merge** 合并 PR，合并后分支自动删除。
7. 版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，git tag（`vX.Y.Z`）是版本号的唯一来源。
8. 绝不提交密钥。安全漏洞请私下报告，不要发公开 issue。
9. 欢迎使用 AI 工具，但每一份贡献都必须有人理解并为之负责。
10. 所有人都遵守[行为准则](../../../CODE_OF_CONDUCT.md)。

## 现状说明

规范中提到的部分工具（CI、pre-commit 钩子、自动发版、PR 和 issue 模板）还在搭建中。
在这些工具就位之前，请手动遵守同样的规则。如果规范和工具的行为不一致，请开 issue，我们会修正其中一方。

## 修改规范

修改规范和修改代码走同样的流程：开一个 `docs:` 类型的 PR，**在同一个 PR 里同时更新中英文两个版本**。
涉及流程本身的修改（比如审查规则、发布规则）需要维护者批准。
