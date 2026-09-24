"""[改动][溯源] ROADMAP.md:99-104：用假模型检验 v0.1 循环的关键协议。"""

import io
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.web_search_provider import BraveSearchProvider
from run_agent import AIAgent
from tools.web_tools import web_search_tool


def chunk(delta=None, finish_reason=None, usage=None):
    """构造 Chat Completions 流中的一个响应片段。"""
    return {"choices": [{"delta": delta or {}, "finish_reason": finish_reason}], "usage": usage}


class FakeCompletions:
    """按脚本返回模型流，并记录每次请求的消息。"""

    def __init__(self, responses):
        """保存各次 create 调用要返回的片段或异常。"""
        self.responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        """记录请求并返回下一段流。"""
        # [改动][溯源] ROADMAP.md:102-104：保存请求时的历史快照，避免后续追加污染断言。
        self.requests.append({**kwargs, "messages": [dict(message) for message in kwargs["messages"]]})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return iter(response)


class FakeProvider:
    """返回可预测的搜索结果，不访问网络。"""

    def __init__(self, barrier=None):
        """可选屏障用于验证两个搜索是否并发。"""
        self.queries = []
        self.limits = []
        self.barrier = barrier

    def search(self, query, limit=5):
        """保存查询并按 Hermes 的 success/data.web 格式返回一条来源。"""
        # [改动][溯源] agent/web_search_provider.py:WebSearchProvider.search；
        # Hermes 0469740:agent/web_search_provider.py:8-12 要求 provider 返回封套。
        if self.barrier:
            self.barrier.wait(timeout=3)
        self.queries.append(query)
        self.limits.append(limit)
        return {"success": True, "data": {"web": [
            {"title": query, "url": "https://example.com/" + query,
             "description": "source", "position": 1}
        ]}}


def fake_agent(responses, **kwargs):
    """用脚本模型及测试 provider 构造 Agent。"""
    completions = FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = AIAgent("https://example.test/v1", "fake-model", client=client, **kwargs)
    return agent, completions


class AgentLoopTests(unittest.TestCase):
    """覆盖回答、工具配对、并发、重试、预算与停止后续聊。"""

    def test_plain_stream_and_usage(self):
        """文本片段立即上报，用量和最终答复进入结果。"""
        deltas = []
        agent, model = fake_agent([
            [chunk({"content": "你"}), chunk({"content": "好"}, "stop",
                                       {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})]
        ], search_provider=FakeProvider())
        result = agent.run_conversation("hello", stream_callback=deltas.append)
        self.assertTrue(result["completed"])
        self.assertEqual(result["final_response"], "你好")
        self.assertEqual(deltas, ["你", "好"])
        self.assertEqual(result["usage"]["total_tokens"], 5)
        self.assertEqual(model.requests[0]["tools"][0]["function"]["name"], "web_search")

    def test_no_search_key_hides_tool(self):
        """没有搜索凭据时，模型请求及系统提示词都不宣称可用 web_search。"""
        # [改动][溯源] agent/web_search_provider.py:get_provider_env 将全空白视为无凭据。
        for key_value in ("", "   "):
            with self.subTest(key_value=key_value), patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": key_value}):
                agent, model = fake_agent([[chunk({"content": "hello"}, "stop")]])
                result = agent.run_conversation("hello")
                self.assertTrue(result["completed"])
                self.assertNotIn("tools", model.requests[0])
                self.assertNotIn("web_search", model.requests[0]["messages"][0]["content"])

    def test_tool_fragments_pair_with_result(self):
        """分块工具参数合并后执行搜索，模型收到匹配的工具结果。"""
        provider = FakeProvider()
        progress = []
        agent, model = fake_agent([
            [chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
                "name": "web_search", "arguments": '{"query":"Py'}}]}),
             chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'thon"}'}}]}, "tool_calls")],
            [chunk({"content": "Found it"}, "stop")],
        ], search_provider=provider, tool_progress_callback=progress.append)
        result = agent.run_conversation("find Python")
        self.assertTrue(result["completed"])
        self.assertEqual(provider.queries, ["Python"])
        self.assertEqual(model.requests[1]["messages"][-1]["tool_call_id"], "call_a")
        self.assertNotIn("name", model.requests[1]["messages"][-1])
        # [改动][溯源] Hermes tools/web_tools.py:268-273 的工具结果沿用 provider 封套。
        search_result = json.loads(result["messages"][-2]["content"])
        self.assertTrue(search_result["success"])
        self.assertEqual(search_result["data"]["web"][0]["title"], "Python")
        self.assertEqual([item["event"] for item in progress], ["tool.started", "tool.completed"])

    def test_independent_searches_run_in_parallel(self):
        """两次独立搜索同时启动，结果仍按原工具调用顺序配对。"""
        provider = FakeProvider(threading.Barrier(2))
        calls = [
            {"index": index, "id": f"call_{index}", "function": {
                "name": "web_search", "arguments": json.dumps({"query": query, "limit": 2})}}
            for index, query in enumerate(("one", "two"))
        ]
        agent, _ = fake_agent([
            [chunk({"tool_calls": calls}, "tool_calls")],
            [chunk({"content": "done"}, "stop")],
        ], search_provider=provider)
        result = agent.run_conversation("search both")
        self.assertTrue(result["completed"], result.get("error"))
        self.assertEqual([m["tool_call_id"] for m in result["messages"] if m["role"] == "tool"],
                         ["call_0", "call_1"])
        self.assertEqual(provider.limits, [2, 2])

    def test_retry_and_iteration_budget(self):
        """429 后重试同一迭代，模型请求数与预算分别统计。"""
        error = RuntimeError("rate limited")
        error.status_code = 429
        agent, _ = fake_agent([error, [chunk({"content": "ok"}, "stop")]],
                              max_iterations=1, max_retries=1)
        with patch("agent.conversation_loop._wait_retry"):
            result = agent.run_conversation("hello")
        self.assertTrue(result["completed"])
        self.assertEqual(result["api_calls"], 2)

    def test_stop_during_stream_allows_continuation(self):
        """流式停止后不留下半条 assistant 消息，下一轮可以沿历史继续。"""
        agent, model = fake_agent([
            [chunk({"content": "partial"}), chunk({"content": "ignored"}, "stop")],
            [chunk({"content": "continued"}, "stop")],
        ])
        first = agent.run_conversation("first", stream_callback=lambda _: agent.interrupt())
        self.assertTrue(first["interrupted"])
        self.assertEqual([m["role"] for m in first["messages"]], ["system", "user"])
        second = agent.run_conversation("next", conversation_history=first["messages"])
        self.assertTrue(second["completed"])
        self.assertEqual(second["final_response"], "continued")
        self.assertEqual(len([m for m in model.requests[1]["messages"] if m["role"] == "system"]), 1)

    def test_stop_during_tool_batch_closes_all_calls(self):
        """工具启动后停止时，每个 assistant 调用都有配对结果供下一轮使用。"""
        calls = [
            {"index": index, "id": f"stop_{index}", "function": {
                "name": "web_search", "arguments": json.dumps({"query": str(index)})}}
            for index in range(2)
        ]
        provider = FakeProvider()
        agent, _ = fake_agent([[chunk({"tool_calls": calls}, "tool_calls")]], search_provider=provider)

        def stop_on_start(event):
            """在第一个工具开始时模拟用户停止。"""
            if event["event"] == "tool.started":
                agent.interrupt()

        agent.tool_progress_callback = stop_on_start
        result = agent.run_conversation("search")
        self.assertTrue(result["interrupted"])
        self.assertEqual([m["tool_call_id"] for m in result["messages"] if m["role"] == "tool"],
                         ["stop_0", "stop_1"])

    def test_budget_exhaustion_keeps_tool_pairing(self):
        """工具执行后预算耗尽时返回未完成状态，同时保留合法结果配对。"""
        provider = FakeProvider()
        agent, _ = fake_agent([[chunk({"tool_calls": [{"index": 0, "id": "call_x", "function": {
            "name": "web_search", "arguments": '{"query":"x"}'}}]}, "tool_calls")]],
                              max_iterations=1, search_provider=provider)
        result = agent.run_conversation("search")
        self.assertEqual(result["turn_exit_reason"], "iteration_budget_exhausted")
        self.assertEqual(result["messages"][-1]["tool_call_id"], "call_x")

    def test_brave_provider_response_shape(self):
        """Brave provider 从 web.results 提取并返回 Hermes 搜索结果封套。"""
        # [改动][溯源] Hermes agent/web_search_provider.py:8-12 与当前 Brave 适配实现。
        payload = {"web": {"results": [{"title": "A", "url": "https://example.com", "description": "B"}]}}
        with patch("agent.web_search_provider.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as opened:
            results = BraveSearchProvider("test-token").search("query", 1)
        self.assertTrue(results["success"])
        self.assertEqual(results["data"]["web"][0]["url"], "https://example.com")
        self.assertEqual(results["data"]["web"][0]["position"], 1)
        self.assertIn("q=query", opened.call_args.args[0].full_url)

    def test_search_failure_keeps_hermes_envelope(self):
        """直接调用无凭据的搜索工具时，返回可被模型读取的失败封套。"""
        # [改动][溯源] Hermes agent/web_search_provider.py:12 约定 failure 形态；
        # tools/web_tools.py:web_search_tool 负责把 provider 异常转换为工具结果。
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""}):
            result = json.loads(web_search_tool("query", agent=SimpleNamespace(search_provider=None)))
        self.assertFalse(result["success"])
        self.assertIn("BRAVE_SEARCH_API_KEY", result["error"])


if __name__ == "__main__":
    unittest.main()
