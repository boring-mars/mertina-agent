"""Validated process-level Gateway limits and loopback binding."""

import pytest
from pydantic import ValidationError

from mertina_agent.config import Settings, load_settings


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.1", "example.com", "localhost", "::1%lo0"]
)
def test_remote_or_resolved_bind_hosts_rejected(host):
    with pytest.raises(ValidationError):
        Settings(gateway_host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.12.1.3", "::1"])
def test_numeric_loopback_hosts_allowed(host):
    assert Settings(gateway_host=host).gateway_host == host


@pytest.mark.parametrize(
    "override",
    [
        {"gateway_max_runs": 1, "gateway_max_active_runs": 2},
        {"gateway_event_capacity": 10, "gateway_subscriber_capacity": 9},
        {"gateway_run_ttl_s": float("inf")},
        {"gateway_shutdown_timeout_s": 0},
        {"gateway_cancel_timeout_s": -1},
        {"gateway_heartbeat_s": float("nan")},
        {"gateway_send_timeout_s": 0},
        {"gateway_port": 65536},
        {"gateway_max_subscribers": 0},
    ],
)
def test_inconsistent_or_unbounded_limits_rejected(override):
    with pytest.raises(ValidationError):
        Settings(**override)


def test_gateway_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MERTINA_GATEWAY_PORT=8766\nMERTINA_GATEWAY_BOOTSTRAP=true\n", encoding="utf-8"
    )
    monkeypatch.setenv("MERTINA_GATEWAY_PORT", "8767")
    settings = load_settings(env_file=env_file)
    assert settings.gateway_port == 8767
    assert settings.gateway_bootstrap is True
