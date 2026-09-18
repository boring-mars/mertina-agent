# 版本与发布

[English](../../en/development/versioning-and-release.md) | **中文**

## 语义化版本

版本号遵循[语义化版本 2.0](https://semver.org/lang/zh-CN/)：`主版本号.次版本号.修订号`（`MAJOR.MINOR.PATCH`）

| 部分 | 何时升级 | 示例 |
|---|---|---|
| `MAJOR` | 破坏性变更：用户升级时必须做出修改 | `1.4.2` → `2.0.0` |
| `MINOR` | 向后兼容的新功能 | `1.4.2` → `1.5.0` |
| `PATCH` | 向后兼容的 bug 修复 | `1.4.2` → `1.4.3` |

以下内容属于**公共接口**，对它们的不兼容修改就是破坏性变更：

- `mertina_agent` 导出的 Python API
- HTTP API
- CLI 命令和参数
- 配置项和环境变量
- 存储格式和数据库结构（升级时需要手动迁移，就属于破坏性变更）

### 1.0 之前

项目从 `0.x.y` 开始。主版本号为 0 时，API 被视为不稳定：

- 破坏性变更升级 **MINOR**：`0.3.1` → `0.4.0`
- 新功能和修复升级 **PATCH**：`0.3.1` → `0.3.2`
- 破坏性变更仍然要在 commit 中标记，并在发布说明中清楚列出

当公共接口足够稳定、我们愿意承诺保持兼容时，发布 `1.0.0`。

### 预发布版本

预发布版本使用 Python 打包工具能识别的 PEP 440 后缀：
`1.0.0a1`（alpha）、`1.0.0b1`（beta）、`1.0.0rc1`（候选版本）。
git tag 加上 `v` 前缀：`v1.0.0rc1`。

## 版本号记录在哪里

- **git tag 是唯一的版本来源。** 每次发布都在 `main` 上打一个附注标签 `vX.Y.Z`
- `pyproject.toml` 中的版本号由发布工具维护，不要在功能 PR 中手动修改
- tag 推送后永远不移动、不删除。如果某个版本有问题，就发布一个新版本

## 变更日志

- 仓库根目录的 `CHANGELOG.md` 采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 的风格
- 它根据已合并 PR 的 Conventional Commits 标题自动生成，这也是 PR 标题很重要的原因
- 出现在变更日志中的类型：`feat`、`fix`、`perf`、`revert`，以及所有标记为破坏性的改动。
  `docs`、`test`、`ci`、`chore` 等类型不出现
- 破坏性变更列在最前面，并附上迁移说明

## 发布流程

发布使用 [release-please](https://github.com/googleapis/release-please) 自动化完成：

```
PR 合并到 main
      │
      ▼
release-please 创建或更新一个 "Release PR"
（更新 pyproject.toml 中的版本号，更新 CHANGELOG.md）
      │
      ▼  维护者审查并合并 Release PR
      │
      ▼
自动创建 tag vX.Y.Z 和 GitHub Release
      │
      ▼
CI 用 `uv build` 构建并发布到 PyPI
（同时构建并推送容器镜像）
```

1. 随着 PR 不断合并，release-please 会持续更新 Release PR，并根据 commit 类型计算下一个版本号
2. 维护者决定发布时，检查 Release PR：版本号是否正确、变更日志是否通顺、
   破坏性变更是否有迁移说明。需要的话直接在 Release PR 里修改变更日志
3. 合并 Release PR 后，自动创建 tag 和 GitHub Release
4. tag 触发发布工作流：
   - `uv build` 生成 sdist 和 wheel
   - 通过 [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) 发布到 PyPI，
     仓库中不需要保存任何 API token
   - 构建并推送容器镜像，镜像使用版本号作为标签
5. 维护者确认包可以正常安装（`uvx mertina-agent --version`），镜像可以正常运行

只有维护者可以合并 Release PR。

## 发布节奏

没有固定的发布周期，有值得发布的内容时就发布，通常是：

- 重要修复（尤其是安全修复）之后尽快发布
- 功能开发阶段每隔几周发布一次

## 旧版本的补丁发布

通常只有最新版本会收到修复，用户应该升级到最新版本。
如果需要维护旧的大版本，参见[维护分支](branching.md#维护分支)。

## 废弃流程

移除或修改公共接口之前：

1. 在一个 minor 版本中标记为废弃：发出 `DeprecationWarning`，在文档中说明，并在变更日志中列出替代方案
2. 至少再保留一个 minor 版本，期间继续可用
3. 在下一个 major 版本中移除（1.0 之前则是下一个 minor 版本）
