"""Bout en bout avec le backend claude_api : RSS mocké (HTTP) -> transcription mockée ->
SDK anthropic mocké -> Telegram mocké (HTTP), puis vérification de l'état final en base."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.e2e import scenarios
from tests.e2e.world import WorldFactory, world_factory


@pytest.fixture
def make(tmp_path: Path) -> WorldFactory:
    return world_factory(tmp_path, "claude_api")


@pytest.mark.parametrize("scenario", scenarios.ALL, ids=lambda f: f.__name__)
def test_scenario(scenario: Callable[[WorldFactory], None], make: WorldFactory) -> None:
    scenario(make)
