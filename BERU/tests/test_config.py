"""Tests for configuration parsing and defaults."""

from __future__ import annotations

from backend.core.config import Settings, get_settings


def test_cors_wildcard_parses_to_list():
    assert Settings(cors_origins="*").cors_origin_list == ["*"]


def test_cors_csv_parses_and_trims():
    settings = Settings(cors_origins="http://a.com, http://b.com ,")
    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]


def test_default_provider_is_mock():
    # The autouse fixture forces LLM_PROVIDER=mock; confirms wiring end-to-end.
    assert get_settings().llm_provider == "mock"


def test_production_flag():
    assert Settings(BERU_ENV="production").is_production is True
    assert Settings(BERU_ENV="development").is_production is False
