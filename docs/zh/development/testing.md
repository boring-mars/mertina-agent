# 测试

[English](../../en/development/testing.md) | **中文**

测试让贡献者可以放心地修改代码。CI 会在每个 PR 上运行全部测试，测试不通过的 PR 无法合并。

## 运行测试

```bash
uv run pytest                          # 全部测试
uv run pytest tests/storage            # 某个目录
uv run pytest -k session               # 名称中包含 "session" 的测试
uv run pytest -x --lf                  # 遇到第一个失败就停止，只重跑上次失败的测试
uv run pytest --cov=mertina            # 生成覆盖率报告
```

## 目录结构

```
tests/
├── conftest.py          # 共享的 fixture
├── unit/                # 快速、隔离，不访问网络和外部服务
│   └── storage/
│       └── test_session_store.py
└── integration/         # 与真实服务交互（数据库、对象存储等）
    └── ...
```

- `tests/unit/` 的目录结构与 `mertina/` 对应
- 文件命名为 `test_<模块>.py`，函数命名为 `test_<行为>`，例如
  `test_load_session_raises_when_missing`

## 测试类型

| 类型 | 范围 | 外部服务 | 何时运行 |
|---|---|---|---|
| **单元测试** | 一个函数或类 | 不使用，用 fake 或 mock 替代 | 每个 PR，必须快（全部跑完只需几秒） |
| **集成测试** | 多个组件加真实的基础设施 | 真实服务，在本地启动（例如用 Docker Compose） | CI 中每个 PR 都运行，用 `@pytest.mark.integration` 标记 |
| **端到端测试** | 通过公开 API 测试部署后的服务 | 测试环境的部署 | 发版之前 |

本地可以用 `uv run pytest -m "not integration"` 跳过集成测试。

## 必须测试的内容

- **每个 bug 修复都要附带回归测试**，去掉修复后这个测试应该失败
- **每个新功能**都要测试主流程和重要的失败路径
- 公共 API 和配置解析要充分测试，因为它们出问题会直接影响用户
- 安全相关的逻辑（认证、授权、租户隔离、输入校验）必须测试，
  包括反向用例：应该被拒绝的请求确实被拒绝了

## 覆盖率

- CI 会统计覆盖率并显示在 PR 上
- 不设硬性的百分比指标，因为数字指标容易催生无意义的测试
- 没有特殊原因的话，PR 不应明显降低覆盖率，审查者可以要求补充测试

## 如何写好测试

- **测试行为，而不是实现。** 断言输出和可观察到的效果，而不是私有方法或调用次数
- **一个测试只测一个行为。** 看到失败测试的名字就应该知道哪里坏了
- **准备、执行、断言（Arrange、Act、Assert）。** 让这三部分清晰可见
- **结果确定。** 不依赖当前时间、随机数、测试执行顺序或真实网络。
  时钟和随机种子通过注入传入，并使用 fixture
- **快速。** 单元测试很慢，通常说明里面藏着 I/O
- **相互独立。** 每个测试自己准备和清理状态。文件操作使用 `tmp_path`
- 输入不同的相似测试，用 `pytest.mark.parametrize`，而不是复制粘贴
- 在系统边界处 mock（HTTP 客户端、云服务 SDK、LLM API），不要 mock 项目自身的代码

## LLM 与外部 API

项目会调用 LLM 服务和云服务。测试中：

- 单元测试和集成测试绝不调用真实的 LLM 或付费 API，使用 fake 或录制好的响应
- 测试中绝不使用真实的 API key。CI 中没有生产环境的凭证
- 如果使用录制的响应作为 fixture，提交前要删除其中的 key、token 和个人数据

## 不稳定的测试

时好时坏的测试就是 bug。发现后：

1. 开一个 issue，打上 `flaky-test` 标签
2. 如果它阻塞了其他人的 PR，用一个单独的 PR 给它加上 `@pytest.mark.skip(reason="flaky, see #123")`
3. 修好后去掉 skip。不要让被跳过的测试一直放在那里
