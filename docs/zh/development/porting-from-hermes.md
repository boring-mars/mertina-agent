# 从 Hermes 移植

[English](../../en/development/porting-from-hermes.md) | **中文**

Mertina Agent 是 Nous Research 的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
的精简重建版，上游以 MIT 许可发布。本文档长期记录我们从上游取了什么，以及取用时遵守的规则。
规则背后的推理见[v0.1 Agent Core 移植设计](../design/2026-09-20-agent-core-loop.md)。

## 上游

| | |
|---|---|
| 仓库 | https://github.com/NousResearch/hermes-agent |
| 许可 | MIT |
| 已移植至 | `fbc4ea8b96c784c9bb28dd27541c14514bba302d`（2026-09-20） |

「已移植至」指当前代码读取自哪个上游 commit。每次从更新的 commit 移植代码后，更新这一行。

我们不持续跟进上游。代码是在一个明确的 commit 上有意识地拷贝过来的，此后作为我们自己的代码维护。

## 规则

### 1. 路径和文件名与上游一致，统一置于 `mertina` 包下

所有可导入的代码都在唯一的顶层包 `mertina` 下面，所以移植文件的路径就是上游路径前加 `mertina/`：

| Hermes | Mertina |
|---|---|
| `agent/turn_tool_round.py` | `mertina/agent/turn_tool_round.py` |
| `agent/transports/base.py` | `mertina/agent/transports/base.py` |
| `tools/registry.py` | `mertina/tools/registry.py` |
| `run_agent.py` | `mertina/run_agent.py` |
| `hermes_state.py` | `mertina/state.py` |
| `hermes_cli/config.py` | `mertina/cli/config.py` |

`hermes_` 前缀是剥掉而不是替换：包名已经提供了命名空间，`mertina/mertina_state.py` 会结巴。

因为映射是机械的，不需要逐文件的映射表——一个已移植文件的来源就是它自己的路径。

我们自己写的、上游没有对应物的文件，按自身合适的方式组织，不编造对应关系。

### 2. 保留版权头

逐字拷贝的文件保留原始版权头，这是 MIT 许可的要求。读懂后重写的文件，注明它源自上游的哪个文件。

### 3. 保留抽象，不保留实现

上游的某个接口有多个实现时（模型 transport、搜索 provider），移植接口，只实现我们需要的那一个。

### 4. 不继承 accretion

Hermes 仓库根有 28 个 `hermes_state_*.py`，`agent/` 一个平铺目录下有 31 个 `turn_*.py`。
这是就地拆分文件的产物，那个共同前缀是一个从未被创建出来的目录。

我们不复制这些拆分痕迹。移植 `hermes_state.py` 得到一个 `mertina/state.py`；
将来确实需要拆分，就拆成 `state/` 包，并在下面记录这条偏离。

## 偏离记录

Mertina 的结构与上游不同的地方，记录下来以免被误认为是无意的漂移。

| 上游 | Mertina | 原因 |
|---|---|---|
| （暂无） | | |

## 与上游对照

在本仓库旁边放一份 Hermes 检出，已移植的文件可以直接和它的对应文件 diff：

```bash
diff <(sed 's/^from \(agent\|tools\)\./from mertina.\1./' ../hermes-agent/agent/turn_tool_round.py) \
     mertina/agent/turn_tool_round.py
```

`sed` 过滤把上游的 import 根改写成我们的，剩下的差异就只有语义差异了。

注意我们的副本是刻意更小的：当前里程碑范围外的特性是有意删除的，diff 很大是预期结果，不是要修复的问题。
