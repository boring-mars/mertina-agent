import asyncio

import pytest

from mertina_agent.config import Settings
from mertina_agent.exceptions import ConfigurationError
from mertina_agent.gateway.run import GatewayRunner


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway_host": "0.0.0.0"},
        {"gateway_host": "example.com"},
        {"gateway_event_capacity": 257, "gateway_subscriber_capacity": 256},
        {"gateway_cancel_timeout_s": float("inf")},
        {"gateway_body_timeout_s": 0},
    ],
)
def test_unchecked_settings_copies_are_revalidated_before_any_startup(overrides):
    unchecked = Settings(_env_file=None).model_copy(update=overrides)
    with pytest.raises(ConfigurationError):
        GatewayRunner(unchecked)


def test_unstarted_runner_has_no_address_or_registry_and_can_close_idempotently():
    async def scenario():
        runner = GatewayRunner(Settings(_env_file=None))
        with pytest.raises(ConfigurationError):
            _ = runner.registry
        with pytest.raises(ConfigurationError):
            _ = runner.address
        await runner.aclose()
        await runner.aclose()
        with pytest.raises(ConfigurationError, match="only be started once"):
            await runner.start()

    asyncio.run(scenario())
