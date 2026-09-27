"""E2E : pipeline complet claude_code mocké + archive NotebookLM mockée.

Une vidéo passe par RSS → transcription → résumé → envoi Telegram → archivage :
au final la base marque archived_at, une source et une note existent dans le
notebook mocké, et la vidéo est bien « sent »."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from guetteur.archive.base import ArchiveError, ArchiveOutcome
from guetteur.models import Video
from guetteur.store import Status
from tests.e2e.world import NEW, World
from tests.helpers import make_config


class RecordingArchiver:
    """Doublure d'archiveur : accepte tout (ou échoue si `fail_on` correspond)."""

    enabled: bool = True

    def __init__(self, fail_on: str | None = None, retryable: bool = False) -> None:
        self.calls: list[tuple[Video, str]] = []
        self._fail_on = fail_on
        self._retryable = retryable

    def archive(self, video: Video, summary_markdown: str) -> ArchiveOutcome:
        self.calls.append((video, summary_markdown))
        if self._fail_on and self._fail_on in video.video_id:
            raise ArchiveError(f"panne simulée sur {video.video_id}", retryable=self._retryable)
        return ArchiveOutcome(notebook_id="nb_1", note_id=f"note_{len(self.calls)}")


def test_pipeline_archives_after_successful_send(tmp_path: Path) -> None:
    archiver = RecordingArchiver()
    world = World(make_config(tmp_path), "claude_code", archiver=archiver)
    world.pipeline.run_cycle()  # initialisation de la playlist
    world.feed.append(NEW)

    stats = world.pipeline.run_cycle()

    assert stats.sent == 1
    assert stats.archived == 1
    assert stats.failed == 0

    rec = world.store.get(NEW[0])
    assert rec is not None
    assert rec.status is Status.SENT
    assert rec.is_archived
    assert rec.archived_at is not None
    # L'archiveur a bien reçu la vidéo attendue + un résumé Markdown (# en tête).
    assert len(archiver.calls) == 1
    archived_video, body = archiver.calls[0]
    assert archived_video.video_id == NEW[0]
    assert body.startswith("# ")
    assert NEW[0] in body or "youtu" in body


def test_pipeline_archive_failure_never_blocks_send(tmp_path: Path, caplog: Any) -> None:
    """Un échec d'archivage laisse archived_at à NULL, sans dégrader le statut « sent »."""
    archiver = RecordingArchiver(fail_on=NEW[0], retryable=False)
    world = World(make_config(tmp_path), "claude_code", archiver=archiver)
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    caplog.set_level("WARNING", logger="guetteur.pipeline")

    stats = world.pipeline.run_cycle()

    assert stats.sent == 1  # Telegram a bien reçu le message
    assert stats.archived == 0
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT
    assert rec.archived_at is None
    assert "video.archive_failed" in caplog.text
    assert len(world.telegram) == 1


def test_pipeline_disabled_archiver_never_calls_archive(tmp_path: Path) -> None:
    """Archiveur par défaut (NoOp) : aucun appel, stats.archived reste à 0."""
    world = World(make_config(tmp_path), "claude_code")  # archiver=None → NoOpArchiver
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    stats = world.pipeline.run_cycle()
    assert stats.sent == 1 and stats.archived == 0
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.archived_at is None
