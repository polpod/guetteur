"""Bout en bout avec le backend claude_code : mêmes scénarios que test_pipeline.py, mais le
résumé passe par le binaire `claude` (subprocess simulé), plus les pannes propres au binaire."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from guetteur.config import SummarizeConfig
from guetteur.store import Status
from tests.e2e import scenarios
from tests.e2e.world import NEW, World, WorldFactory, world_factory
from tests.helpers import FakeProcess, FakeSpawn, claude_code_ok, cli_envelope, make_config


@pytest.fixture
def make(tmp_path: Path) -> WorldFactory:
    return world_factory(tmp_path, "claude_code")


@pytest.mark.parametrize("scenario", scenarios.ALL, ids=lambda f: f.__name__)
def test_scenario(scenario: Callable[[WorldFactory], None], make: WorldFactory) -> None:
    scenario(make)


def _not_logged_in() -> FakeProcess:
    return FakeProcess(
        stdout=cli_envelope("Not logged in · Please run /login", is_error=True), returncode=1
    )


def test_not_logged_in_aborts_cycle_without_burning_retries(tmp_path: Path) -> None:
    spawn = FakeSpawn(_not_logged_in(), _not_logged_in(), claude_code_ok())
    world = World(make_config(tmp_path), "claude_code", spawn=spawn)
    world.pipeline.run_cycle()
    world.feed.append(NEW)

    for _ in range(2):  # plusieurs cycles sans session : la vidéo attend
        stats = world.pipeline.run_cycle()
        assert "claude auth login" in stats.aborted
        rec = world.store.get(NEW[0])
        assert rec is not None
        assert rec.status is Status.TRANSCRIBED and rec.retries == 0
        assert world.telegram == []

    stats = world.pipeline.run_cycle()  # l'utilisateur s'est connecté
    assert stats.sent == 1 and not stats.aborted
    assert world.transcriber.calls == [NEW[0]]  # transcription réutilisée
    assert len(world.telegram) == 1


def test_timeout_counts_as_retry(tmp_path: Path) -> None:
    config = make_config(tmp_path, summarize=SummarizeConfig(timeout_s=0.05))
    spawn = FakeSpawn(FakeProcess(hang=True), claude_code_ok())
    world = World(config, "claude_code", spawn=spawn)
    world.pipeline.run_cycle()
    world.feed.append(NEW)

    world.pipeline.run_cycle()
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.RETRY and rec.retries == 1
    assert spawn.processes[0].killed

    assert world.pipeline.run_cycle().sent == 1
    assert len(world.telegram) == 1
