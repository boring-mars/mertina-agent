import pytest
from pydantic import ValidationError

from mertina_agent.gateway.api_models import (
    CreateSession,
    MessagePage,
    RenameSession,
    SessionPage,
    StartRun,
    parse_unsigned,
    session_response,
    validate_identifier,
)
from mertina_agent.state.models import SessionRecord

SESSION_ID = "a" * 32


@pytest.mark.parametrize("title", ["", "会话🦊", "x" * 200])
def test_title_accepts_exact_text(title):
    assert CreateSession.model_validate({"title": title}).title == title


@pytest.mark.parametrize("title", [None, 5, True, [], "x" * 201, "nul\x00", "\ud800"])
def test_title_rejects_invalid_text(title):
    with pytest.raises(ValidationError):
        CreateSession.model_validate({"title": title})


@pytest.mark.parametrize(
    "body",
    [
        {"title": "new"},
        {"title": "new", "expected_version": "1"},
        {"title": "new", "expected_version": True},
        {"title": "new", "expected_version": -1},
        {"title": "new", "expected_version": 2**63},
        {"title": "new", "expected_version": 0, "unknown": 1},
    ],
)
def test_rename_requires_strict_revision(body):
    with pytest.raises(ValidationError):
        RenameSession.model_validate(body)


def test_start_run_preserves_whitespace_and_exact_utf8_limit():
    message = " 开始\n" + "🦊" * 16380
    assert (
        StartRun.model_validate({"session_id": SESSION_ID, "message": message}).message == message
    )
    assert StartRun.model_validate({"session_id": SESSION_ID, "message": "x" * 65536})


@pytest.mark.parametrize("message", ["", "  \n", "\x00", "\udfff", "x" * 65537, "🦊" * 16385, 8])
def test_start_run_rejects_invalid_or_oversized_messages(message):
    with pytest.raises(ValidationError):
        StartRun.model_validate({"session_id": SESSION_ID, "message": message})


@pytest.mark.parametrize("identifier", ["A" * 32, "a" * 31, "g" * 32, "a" * 32 + "\n", ""])
def test_identifiers_are_exact_lowercase_hex(identifier):
    with pytest.raises(ValueError, match="Invalid identifier"):
        validate_identifier(identifier)
    with pytest.raises(ValidationError):
        StartRun.model_validate({"session_id": identifier, "message": "hello"})


@pytest.mark.parametrize(
    "value", ["-1", "+1", "01", "1.0", "1e2", " 1", "1\n", "\u0661", str(2**63)]
)
def test_numeric_cursors_reject_ambiguous_values(value):
    with pytest.raises(ValueError, match="Expected an unsigned"):
        parse_unsigned(value)


@pytest.mark.parametrize("value", ["0", "1", str(2**63 - 1)])
def test_numeric_cursors_accept_bounded_canonical_values(value):
    assert parse_unsigned(value) == int(value)


@pytest.mark.parametrize(
    "body",
    [
        {"after_id": SESSION_ID},
        {"after_created_at_us": 1},
        {"limit": 101},
        {"limit": 0},
        {"limit": True},
        {"limit": "2"},
        {"after_created_at_us": -1, "after_id": SESSION_ID},
    ],
)
def test_session_page_rejects_partial_or_invalid_cursors(body):
    with pytest.raises(ValidationError):
        SessionPage.model_validate(body)


def test_message_page_requires_cursor_before_high_water_mark():
    assert MessagePage.model_validate({"after": 2, "through": 2}).through == 2
    with pytest.raises(ValidationError):
        MessagePage.model_validate({"after": 3, "through": 2})


def test_session_response_keeps_metadata_and_activity_separate():
    session = SessionRecord(SESSION_ID, "title", 1, 2, 3, 4, 5)
    response = session_response(session, active_run_id="b" * 32)
    assert response == {
        "id": SESSION_ID,
        "title": "title",
        "created_at_us": 1,
        "updated_at_us": 2,
        "history_revision": 3,
        "metadata_version": 4,
        "message_count": 5,
        "active_run_id": "b" * 32,
    }
