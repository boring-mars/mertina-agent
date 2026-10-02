# Ported from hermes-agent agent/chat_completion_nonstream.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Request-local worker lifecycle and interrupt polling."""

from mertina.agent import chat_completion_helpers as h


class _NonStreamRequest:
    """One non-streaming request on a worker thread, polled by the caller.

    State shared between the worker (``_call``) and the poll loop lives on the
    instance; ``_abort_request`` may run from the poll (stranger) thread.
    """

    def __init__(self, agent, api_kwargs: dict):
        self.agent = agent
        self.api_kwargs = api_kwargs
        self.result = {"response": None, "error": None}
        self.clients = h._RequestClientRegistry(agent)
        # Request-local cancel flag: agent._interrupt_requested is cleared at turn
        # boundaries but this daemon worker can outlive the turn, so it must know THIS
        # request was force-closed and not surface the transport error as a bug (#6600).
        self.cancelled = False
        self.thread = None

    def _make_client(self, reason: str):
        # Per-request clients are registered with the abort machinery so an interrupt can
        # force-close the worker's connection, never the shared client (#67142).
        client = self.agent._create_request_openai_client(reason=reason)
        return self.clients.set_client(client)

    def _call(self):
        try:
            self.result["response"] = h._dispatch_nonstreaming_api_request(
                self.agent, self.api_kwargs, make_client=self._make_client
            )
        except Exception as e:
            # Our own force-close caused this error: swallow it, the main
            # thread raises InterruptedError (#6600). Cancellation logs at debug.
            if self.cancelled:
                h.logger.debug(
                    "Non-streaming worker caught %s after request "
                    "cancellation — exiting without surfacing a network error.",
                    type(e).__name__,
                )
                return
            self.result["error"] = e
        finally:
            # Reuse reason only on a clean response; error or cancel-swallow
            # really closes so the next attempt builds a fresh pool.
            self.clients.close_once(
                "request_complete"
                if self.result["response"] is not None
                else "request_error_cleanup"
            )

    def _abort_request(self, reason: str) -> None:
        """Interrupt kill: abort the request client (#67142); the worker sees its own
        forced close via the cancel flag."""
        with h.contextlib.suppress(Exception):
            self.clients.close_once(reason)

    def _interrupt(self) -> None:
        # Mark cancelled BEFORE force-closing so the worker treats the transport
        # error as a cancel (#6600). Never close the shared client (releasing a
        # TLS FD mid-SSL-BIO corrupted an unrelated SQLite DB, #67142).
        self.cancelled = True
        h.logger.debug("Force-closing httpx client due to interrupt (not a network error).")
        self._abort_request("interrupt_abort")
        raise InterruptedError("Agent interrupted during API call")

    def run(self):
        agent = self.agent

        self.thread = t = h.threading.Thread(
            target=h._context_thread_target(self._call), daemon=True
        )
        t.start()
        while t.is_alive():
            t.join(timeout=0.3)
            if agent._interrupt_requested:
                self._interrupt()
        if self.result["error"] is not None:
            raise self.result["error"]
        return self.result["response"]
