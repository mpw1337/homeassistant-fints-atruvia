"""Shared pytest fixtures for fints_atruvia tests."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001 - requesting the fixture is the whole effect
    """Enable loading of the custom component for every test."""
    yield
