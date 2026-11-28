"""Strict HTTP inputs and portable response serialization for the local Gateway."""

import re
from dataclasses import asdict
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from mertina_agent.state.models import CommitReceipt, SessionRecord

type Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
type NonNegative = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
type PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]


class RequestModel(BaseModel):
    """Reject coercions and undeclared fields at the HTTP boundary."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class CreateSession(RequestModel):
    """Create an empty session with an optional title."""

    title: str = Field(default="New conversation", max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Reject NUL and invalid Unicode before reaching a database driver."""
        validate_text(value)
        return value


class RenameSession(CreateSession):
    """Rename only if the durable metadata version still matches."""

    title: str = Field(max_length=200)
    expected_version: NonNegative


class StartRun(RequestModel):
    """Start one user turn in an existing conversation."""

    session_id: Identifier
    message: str = Field(min_length=1, max_length=65536)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Bound encoded input without altering whitespace in the actual prompt."""
        validate_text(value)
        if not value.strip() or len(value.encode("utf-8")) > 65536:
            message = "Message must contain text and fit within 65536 UTF-8 bytes"
            raise ValueError(message)
        return value


class SessionPage(RequestModel):
    """Stable keyset pagination by creation time ascending or update time descending."""

    order: Literal["created_asc", "updated_desc"] = "created_asc"
    limit: PageLimit = 50
    after_created_at_us: NonNegative | None = None
    after_updated_at_us: NonNegative | None = None
    after_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_cursor(self) -> Self:
        """Require both parts of the order's own keyset cursor together."""
        if self.order == "created_asc":
            timestamp, foreign = self.after_created_at_us, self.after_updated_at_us
        else:
            timestamp, foreign = self.after_updated_at_us, self.after_created_at_us
        if foreign is not None:
            message = "Session cursor field does not match the requested order"
            raise ValueError(message)
        if (timestamp is None) != (self.after_id is None):
            message = "Both session cursor fields are required together"
            raise ValueError(message)
        return self

    @property
    def after(self) -> tuple[int, str] | None:
        """Return the keyset cursor for the store, if one was supplied."""
        timestamp = (
            self.after_created_at_us if self.order == "created_asc" else self.after_updated_at_us
        )
        if timestamp is None or self.after_id is None:
            return None
        return timestamp, self.after_id

    def next_cursor(self, last: SessionRecord) -> dict[str, object]:
        """Build the continuation cursor using this order's field names."""
        if self.order == "created_asc":
            return {"after_created_at_us": last.created_at_us, "after_id": last.id}
        return {"after_updated_at_us": last.updated_at_us, "after_id": last.id}


class MessagePage(RequestModel):
    """Read a bounded message prefix across multiple HTTP requests."""

    after: NonNegative = 0
    limit: PageLimit = 50
    through: NonNegative | None = None

    @model_validator(mode="after")
    def validate_cursor(self) -> Self:
        """Disallow a cursor past its supplied high water mark."""
        if self.through is not None and self.after > self.through:
            message = "Message cursor exceeds its high water mark"
            raise ValueError(message)
        return self


def validate_text(value: str) -> None:
    """Require NUL-free Unicode encodable by all supported databases."""
    if "\x00" in value:
        message = "Text must not contain NUL"
        raise ValueError(message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        message = "Text contains invalid Unicode"
        raise ValueError(message) from exc


def parse_unsigned(value: str) -> int:
    """Parse a canonical, bounded integer query value or event cursor."""
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        message = "Expected an unsigned 64-bit signed-range integer"
        raise ValueError(message)
    return int(value)


def validate_identifier(value: str) -> str:
    """Validate a path identifier before a domain operation."""
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        message = "Invalid identifier"
        raise ValueError(message)
    return value


def receipt_response(operation_id: str, receipt: CommitReceipt | None) -> dict[str, object]:
    """Serialize a commit lookup; the fingerprint is internal and never exposed."""
    if receipt is None:
        return {"operation_id": operation_id, "committed": False}
    return {
        "operation_id": receipt.operation_id,
        "committed": True,
        "revision": receipt.revision,
        "first_seq": receipt.first_seq,
        "last_seq": receipt.last_seq,
        "status": receipt.status,
    }


def session_response(session: SessionRecord, *, active_run_id: str | None) -> dict[str, object]:
    """Serialize durable metadata alongside the process-local active run ID."""
    result: dict[str, object] = asdict(session)
    result["active_run_id"] = active_run_id
    return result
