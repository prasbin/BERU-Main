"""Tests for the LLM abstraction: mock provider and provider factory."""

from __future__ import annotations

import pytest

from backend.core.config import Settings
from backend.core.errors import ConfigurationError
from backend.engines.llm.base import LLMMessage
from backend.engines.llm.mock import MockProvider
from backend.engines.llm.openai_compatible import OpenAICompatibleProvider
from backend.engines.llm.registry import build_provider


async def test_mock_provider_echoes_last_user_message():
    provider = MockProvider()
    response = await provider.chat([LLMMessage(role="user", content="Hello world")])
    assert "Hello world" in response.content
    assert response.model
    assert response.usage is not None
    assert response.usage.total_tokens > 0


async def test_mock_provider_ignores_non_user_for_echo():
    provider = MockProvider()
    messages = [
        LLMMessage(role="system", content="be nice"),
        LLMMessage(role="user", content="ping"),
        LLMMessage(role="assistant", content="pong"),
        LLMMessage(role="user", content="final question"),
    ]
    response = await provider.chat(messages)
    assert "final question" in response.content


def test_build_provider_mock():
    provider = build_provider(Settings(llm_provider="mock"))
    assert provider.name == "mock"


def test_build_provider_unknown_raises():
    with pytest.raises(ConfigurationError):
        build_provider(Settings(llm_provider="does_not_exist"))


def test_build_provider_openai_compatible():
    provider = build_provider(
        Settings(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
            llm_model="local-model",
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "openai_compatible"


def test_build_provider_openai_compatible_requires_model():
    with pytest.raises(ConfigurationError):
        build_provider(
            Settings(
                llm_provider="openai_compatible",
                llm_base_url="http://localhost:1234/v1",
                llm_model="",
            )
        )
