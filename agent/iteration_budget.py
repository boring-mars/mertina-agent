# 版本 V0.1 变更说明
# [改动][溯源] Hermes 0469740ab33fd02a4f55a6ea11d81df04ea646a5:agent/iteration_budget.py；
# ROADMAP.md:73-74。保留原版线程安全计数，只开放当前对话轮次使用的 consume/used/remaining。
# 已注释的功能：预算预警比例和 execute_code/subagent 的 refund；原实现见下方归档段。
# 源代码改动点：去掉当前阶段没有调用方的预警与返还入口，保留原版锁和预算语义。
# 新增代码：中文职责与溯源说明；旧 Mertina dataclass 实现亦在归档段保留。

# === Hermes 原版逐行归档：暂不启用的代码以注释保留 ===
# [溯源] 0469740ab33fd02a4f55a6ea11d81df04ea646a5:agent/iteration_budget.py
# """Per-agent iteration budget — thread-safe consume/refund counter.

# Each ``AIAgent`` (parent or subagent) holds its own :class:`IterationBudget`: the parent's
# cap is ``max_iterations`` (default 500), each subagent's ``delegation.max_iterations``
# (default 50), so total iterations across parent + subagents can exceed the parent's cap.
# """

# from __future__ import annotations

# import math
# import threading


# def normalize_budget_warning_ratio(value) -> float | None:
#     """A finite ratio strictly between zero and one, or None (feature off)."""
#     if value is None or isinstance(value, bool):
#         return None
#     try:
#         ratio = float(value)
#     except (TypeError, ValueError):
#         return None
#     return ratio if math.isfinite(ratio) and 0 < ratio < 1 else None


# class IterationBudget:
#     """Thread-safe iteration counter; ``execute_code`` (programmatic tool calling)
#     iterations are refunded via :meth:`refund` so they don't eat into the budget."""

#     def __init__(self, max_total: int):
#         self.max_total = max_total
#         self._used = 0
#         self._lock = threading.Lock()

#     def consume(self) -> bool:
#         """Try to consume one iteration.  Returns True if allowed."""
#         with self._lock:
#             if self._used >= self.max_total:
#                 return False
#             self._used += 1
#             return True

#     def refund(self) -> None:
#         """Give back one iteration (e.g. for execute_code turns)."""
#         with self._lock:
#             if self._used > 0:
#                 self._used -= 1

#     @property
#     def used(self) -> int:
#         with self._lock:
#             return self._used

#     @property
#     def remaining(self) -> int:
#         with self._lock:
#             return max(0, self.max_total - self._used)


# __all__ = ["IterationBudget"]

# === Mertina v0.1 当前有效实现 ===

"""单轮 Agent 迭代预算：并发工具共享的线程安全计数器。"""

import threading


class IterationBudget:
    """管理一轮最多可发起的模型请求次数，重试不额外消耗额度。"""

    def __init__(self, max_total: int) -> None:
        """设置请求上限，并创建保护计数器的锁。"""
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """在预算未耗尽时占用一次请求额度，返回是否占用成功。"""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    # [改动][溯源] Hermes 原版 refund 用于 execute_code 返还预算；ROADMAP.md:66-85
    # 当前只有模型请求消耗预算，没有返还场景，原函数在归档段保留。

    @property
    def used(self) -> int:
        """返回已经占用的模型请求额度。"""
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        """返回尚可发起的模型请求次数。"""
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]


# === Mertina 改动前实现：接口收缩时按原样注释保留 ===
# """[改动][溯源] ROADMAP.md:73-74：记录单轮对话可发起的模型请求次数。"""
#
# from dataclasses import dataclass
#
#
# @dataclass
# class IterationBudget:
#     """每次新模型请求消耗一次预算；同一次请求的重试不重复计数。"""
#
#     limit: int
#     used: int = 0
#
#     @property
#     def remaining(self) -> int:
#         """返回本轮还能发起的模型请求数。"""
#         return max(0, self.limit - self.used)
#
#     def consume(self) -> bool:
#         """预算充足时占用一次请求额度，并告知调用方是否成功。"""
#         if self.remaining == 0:
#             return False
#         self.used += 1
#         return True
