"""
tests/bot_config/conftest.py

Pytest fixtures for bot_config tests.
All bot_config imports are lazy (inside fixture bodies) to avoid
module-level import errors during pytest collection phase.
"""

import pytest
from .helpers import make_row


@pytest.fixture
def valid_row() -> dict:
    return make_row()


@pytest.fixture
def valid_config(valid_row):
    from bot_config.models import BotConfig
    return BotConfig.from_row(valid_row)
