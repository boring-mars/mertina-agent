"""One-line, user-safe summaries of provider errors."""

import httpx
import openai
import pytest

from mertina.agent.api_error_summary import ApiErrorSummaryMixin

_REQUEST = httpx.Request("POST", "http://fake.test/v1/chat/completions")
_summarize = ApiErrorSummaryMixin._summarize_api_error


def _status_error(status: int, message: str = "boom", body: object = None) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQUEST)
    return openai.APIStatusError(message, response=response, body=body)


def test_an_offline_machine_gets_a_plain_hint() -> None:
    try:
        try:
            raise OSError("[Errno 11001] getaddrinfo failed")
        except OSError as dns:
            raise openai.APIConnectionError(request=_REQUEST) from dns
    except openai.APIConnectionError as error:
        summary = _summarize(error)

    assert summary.startswith("Mertina can't reach the model provider.")


def test_an_html_error_page_is_reduced_to_its_title() -> None:
    page = (
        "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
        "<body>Cloudflare Ray ID: <strong>8abc</strong></body></html>"
    )

    assert _summarize(_status_error(502, page)) == "HTTP 502 — 502 Bad Gateway — Ray 8abc"


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        ({"error": {"message": "model overloaded"}}, "HTTP 503: model overloaded"),
        ({"message": "flat message"}, "HTTP 503: flat message"),
        ({"error": {"message": {"detail": "nested"}}}, "HTTP 503: nested"),
        ({"error": {"message": ["a", {"code": "b"}]}}, "HTTP 503: a; b"),
    ],
)
def test_the_body_message_is_preferred(body: object, summary: str) -> None:
    assert _summarize(_status_error(503, body=body)) == summary


def test_otherwise_the_error_text_is_truncated() -> None:
    assert _summarize(_status_error(500, "x" * 800)) == "HTTP 500: " + "x" * 500
    assert _summarize(RuntimeError("plain")) == "plain"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("", "Unknown error"),
        (
            "<!DOCTYPE html><p>down</p>",
            "Service temporarily unavailable (HTML error page returned)",
        ),
        ("  spaced \n out  ", "spaced out"),
        ("y" * 200, "y" * 150 + "..."),
    ],
)
def test_clean_error_message(raw: str, clean: str) -> None:
    assert ApiErrorSummaryMixin()._clean_error_message(raw) == clean
