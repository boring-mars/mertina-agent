"""检验 CLI 参照 Hermes 的历史传递、失败轮次和原有单次调用。"""

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from run_agent import _run_interactive_chat, main


class InteractiveCliTests(unittest.TestCase):
    """验证交互入口按暂存用户消息和采纳轮次结果的顺序工作。"""

    def test_reuses_full_history_until_exit(self):
        """下一轮能看到上一轮的用户、工具调用、工具结果和最终回复。"""
        first_messages = [
            {"role": "system", "content": "You are Mertina."},
            {"role": "user", "content": "create a file"},
            {"role": "assistant", "tool_calls": [{"id": "tool-1", "function": {"name": "write_file"}}]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "done"},
            {"role": "assistant", "content": "created"},
        ]
        previous_turn_messages = list(first_messages)
        agent = Mock()
        agent.run_conversation.side_effect = [
            {"completed": True, "messages": first_messages, "final_response": "created"},
            {
                "completed": True,
                "messages": first_messages + [{"role": "user", "content": "read it"}],
                "final_response": "read",
            },
        ]

        with (
            patch("builtins.input", side_effect=["create a file", "read it", "/quit"]),
            redirect_stdout(io.StringIO()),
        ):
            _run_interactive_chat(agent)

        self.assertEqual(
            agent.run_conversation.call_args_list,
            [
                call(user_message="create a file", conversation_history=[]),
                call(user_message="read it", conversation_history=previous_turn_messages),
            ],
        )

    def test_failed_turn_result_becomes_next_history(self):
        """失败轮次返回的消息也成为下一轮历史，与 Hermes 采纳结果一致。"""
        first_messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
        ]
        failed_messages = first_messages + [{"role": "user", "content": "failed"}]
        previous_turn_messages = list(failed_messages)
        agent = Mock()
        agent.run_conversation.side_effect = [
            {"completed": True, "messages": first_messages, "final_response": "ok"},
            {
                "completed": False,
                "messages": failed_messages,
                "error": "model error",
            },
            {
                "completed": True,
                "messages": failed_messages + [{"role": "user", "content": "retry"}],
                "final_response": "done",
            },
        ]

        with patch("builtins.input", side_effect=["first", "failed", "retry", "q"]), redirect_stdout(io.StringIO()):
            _run_interactive_chat(agent)

        self.assertEqual(
            agent.run_conversation.call_args_list[2],
            call(user_message="retry", conversation_history=previous_turn_messages),
        )

    def test_raised_turn_error_returns_to_prompt(self):
        """轮次调用抛异常时保留已暂存的用户消息，并继续接收输入。"""
        agent = Mock()
        agent.run_conversation.side_effect = [
            RuntimeError("temporary failure"),
            {"completed": True, "messages": [], "final_response": "ok"},
        ]

        with patch("builtins.input", side_effect=["first", "retry", "/exit"]), redirect_stdout(io.StringIO()):
            _run_interactive_chat(agent)

        self.assertEqual(
            agent.run_conversation.call_args_list[1],
            call(user_message="retry", conversation_history=[{"role": "user", "content": "first"}]),
        )

    def test_query_argument_keeps_single_turn_cli(self):
        """带位置参数时调用原有 chat 入口并在退出时关闭客户端。"""
        settings = SimpleNamespace(
            base_url="https://example.test/v1",
            model="test-model",
            api_key="dummy",
            max_iterations=10,
            max_retries=2,
            search_provider="none",
        )
        with (
            patch("sys.argv", ["run_agent.py", "hello"]),
            patch("agent.config.load_settings", return_value=settings),
            patch("run_agent.AIAgent") as agent_class,
            patch("run_agent._run_interactive_chat") as interactive,
            redirect_stdout(io.StringIO()) as output,
        ):
            agent_class.return_value.chat.return_value = "hello back"
            main()

        agent_class.return_value.chat.assert_called_once_with("hello")
        agent_class.return_value.close.assert_called_once_with()
        interactive.assert_not_called()
        self.assertEqual(output.getvalue().strip(), "hello back")


if __name__ == "__main__":
    unittest.main()
