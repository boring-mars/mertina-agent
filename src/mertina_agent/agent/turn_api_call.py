"""The provider call of one loop iteration, interruptible while in flight.

Copied from Hermes agent/turn_api_call.py (``ApiCallVerdict``,
``perform_api_call``, ``handle_api_interrupt``) and agent/conversation_loop.py
(``INTERRUPT_WAITING_FOR_MODEL_PREFIX``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

Hermes runs the request on a worker thread and polls its interrupt flag,
raising ``InterruptedError`` into the retry loop. Here the request task races
the run's stop signal and the outcome is reported as a verdict. The Nous rate
guard, middleware, relay, MoA handshake and redirect crossing check are left out.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.interrupt import current_interrupt_signal
from mertina_agent.agent.model_client import ModelClientProtocol
from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, ToolDefinition

logger = logging.getLogger(__name__)

INTERRUPT_WAITING_FOR_MODEL_PREFIX = "Operation interrupted: waiting for model response ("
"""Stable prefix clients can match to treat the text as cancellation metadata."""


@dataclass
class ApiCallVerdict:
    """Verdict of :func:`perform_api_call`.

    Attributes:
        action: ``"fallthrough"`` means ``response`` is ready; ``"interrupted"``
            means the run was stopped while the request was in flight.
        response: The model response when ``action`` is ``"fallthrough"``.
    """

    action: Literal["fallthrough", "interrupted"]
    response: NormalizedResponse | None = None


async def perform_api_call(
    model_client: ModelClientProtocol,
    api_messages: Sequence[ChatMessage],
    tools: Sequence[ToolDefinition],
) -> ApiCallVerdict:
    """Issue one model request, abandoning it if the run is asked to stop.

    A response that arrives together with the stop request is still used; the
    next iteration then sees the stop before making another call.

    Raises:
        ModelInputError: If the history violates the text contract.
        ModelRequestError: For timeout, connection, HTTP or closed failures.
        ModelResponseError: If the provider response is malformed.
    """
    signal = current_interrupt_signal()
    if signal is None:
        return ApiCallVerdict("fallthrough", await model_client.complete(api_messages, tools=tools))

    request = asyncio.ensure_future(model_client.complete(api_messages, tools=tools))
    stop = asyncio.ensure_future(signal.wait())
    try:
        await asyncio.wait({request, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
        if not request.done():
            request.cancel()
            # The abandoned request's own error, if any, no longer matters.
            with suppress(asyncio.CancelledError, Exception):
                await request
    if request.cancelled():
        return ApiCallVerdict("interrupted")
    return ApiCallVerdict("fallthrough", request.result())


@dataclass
class ApiInterruptVerdict:
    """Verdict of :func:`handle_api_interrupt`: the turn ends as interrupted.

    Attributes:
        final_response: Cancellation text reported to the caller.
    """

    final_response: str


def handle_api_interrupt(*, api_start_time: float) -> ApiInterruptVerdict:
    """Record a stop that arrived while waiting for the model.

    Hermes also keeps text already streamed before the stop; that part arrives
    with the streaming transport.
    """
    api_elapsed_s = time.monotonic() - api_start_time
    logger.info("Interrupted during API call after %.1fs", api_elapsed_s)
    return ApiInterruptVerdict(
        final_response=f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}{api_elapsed_s:.1f}s elapsed)."
    )
