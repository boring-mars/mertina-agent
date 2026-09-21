"""process_bootstrap.OpenAI stands in for openai.OpenAI and imports it lazily."""

from typing import Any

from openai import OpenAI as SdkOpenAI

from mertina.agent import process_bootstrap


def test_calling_the_proxy_builds_an_sdk_client() -> None:
    client = process_bootstrap.OpenAI(api_key="test-key", base_url="http://localhost:9/v1")

    assert type(client) is SdkOpenAI
    client.close()


def test_isinstance_against_the_proxy_checks_the_sdk_class() -> None:
    # The proxy is an instance, not a class; typing it as Any lets isinstance take it.
    proxy: Any = process_bootstrap.OpenAI
    client = SdkOpenAI(api_key="test-key", base_url="http://localhost:9/v1")

    assert isinstance(client, proxy)
    assert not isinstance(object(), proxy)
    client.close()


def test_the_sdk_class_is_cached() -> None:
    assert process_bootstrap._load_openai_cls() is process_bootstrap._load_openai_cls() is SdkOpenAI


def test_repr_names_the_proxy() -> None:
    assert repr(process_bootstrap.OpenAI) == "<lazy openai.OpenAI proxy>"
