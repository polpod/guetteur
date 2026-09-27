"""E2E : `guetteur archive --pending` archive les vidéos « sent » sans archived_at.

Trois vidéos déjà envoyées sont mises dans la base, l'archiveur est configuré pour
échouer sur la 3e. La commande archive les 2 premières, marque leur archived_at,
laisse la 3e non archivée, et le code de retour vaut 1."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from guetteur.archive.base import ArchiveError, ArchiveOutcome
from guetteur.main import cli
from guetteur.models import Video
from guetteur.store import Store


class ScriptedArchiver:
    """Doublure : réussit sauf sur des video_id listés dans `fail_ids`."""

    enabled: bool = True

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_ids = fail_ids or set()

    def archive(self, video: Video, summary_markdown: str) -> ArchiveOutcome:
        self.calls.append(video.video_id)
        if video.video_id in self._fail_ids:
            raise ArchiveError(f"HTTP 500 sur {video.video_id}", retryable=True)
        return ArchiveOutcome(notebook_id="nb_1", note_id=f"note_{video.video_id}")


def _write_config(tmp_path: Path) -> Path:
    """config.toml minimal avec [archive] enabled = true."""
    path = tmp_path / "config.toml"
    home = tmp_path / "nlm"
    home.mkdir(mode=0o700)
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n\n'
        f'[archive]\nenabled = true\nhome = "{home.as_posix()}"\n'
        'account = "guetteur.veille@gmail.com"\n\n'
        '[[playlists]]\nid = "PL"\nlabel = "Veille IA"\n',
        encoding="utf-8",
    )
    return path


def _seed_sent(store: Store, video_id: str, title: str) -> None:
    """Vidéo dans l'état « réellement envoyée » : status=sent, sent_at horodaté, summary
    non NULL. Elle est donc éligible à pending_archive."""
    v = Video(
        video_id=video_id,
        title=title,
        channel="Chaîne",
        published=datetime(2026, 9, 20, tzinfo=UTC),
        url=f"https://youtu.be/{video_id}",
    )
    store.add_new(v, "PL")
    store.set_transcript(video_id, '{"language": "fr", "source": "youtube", "segments": []}')
    store.set_summary(
        video_id,
        '{"title": "T", "tldr": "TL", "key_points": [{"seconds":0,"text":"P"}],'
        ' "why_it_matters": "W"}',
    )
    assert store.claim_for_sending(video_id)
    assert store.mark_sent(video_id)


def test_archive_pending_reports_partial_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trois « sent », l'archiveur échoue sur la 3e → 2 archivées, retour 1."""
    store = Store(tmp_path / "guetteur.db")
    for i in range(1, 4):
        _seed_sent(store, f"VID_{i}", f"Vidéo {i}")
    assert len(store.pending_archive()) == 3
    store.close()

    fake = ScriptedArchiver(fail_ids={"VID_3"})
    # On force build_archiver à retourner notre doublure quel que soit config.archive.
    with patch("guetteur.main.build_archiver", return_value=fake):
        code = cli(["--config", str(_write_config(tmp_path)), "archive", "--pending"])

    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert code == 1  # au moins une en échec
    assert "VID_1" in out and "VID_2" in out
    assert "1 en échec" in out and "2 archivée" in out
    # La 3e est mentionnée sur stderr avec sa raison (redactée si nécessaire).
    assert "VID_3" in err

    # Vérification base : les 2 premières sont archived_at, la 3e non.
    store = Store(tmp_path / "guetteur.db")
    try:
        assert store.get("VID_1").archived_at is not None  # type: ignore[union-attr]
        assert store.get("VID_2").archived_at is not None  # type: ignore[union-attr]
        assert store.get("VID_3").archived_at is None  # type: ignore[union-attr]
        assert set(fake.calls) == {"VID_1", "VID_2", "VID_3"}
    finally:
        store.close()


def test_archive_pending_when_nothing_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Aucune vidéo en attente d'archive : retour 0 et message clair."""
    Store(tmp_path / "guetteur.db").close()  # base vide

    with patch("guetteur.main.build_archiver", return_value=ScriptedArchiver()):
        code = cli(["--config", str(_write_config(tmp_path)), "archive", "--pending"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Aucune vidéo" in out


def test_archive_video_id_specific(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--video-id VID_1` archive uniquement VID_1 alors que VID_2 est aussi en attente."""
    store = Store(tmp_path / "guetteur.db")
    _seed_sent(store, "VID_1", "Vidéo 1")
    _seed_sent(store, "VID_2", "Vidéo 2")
    store.close()

    fake = ScriptedArchiver()
    with patch("guetteur.main.build_archiver", return_value=fake):
        code = cli(["--config", str(_write_config(tmp_path)), "archive", "--video-id", "VID_1"])
    assert code == 0
    assert fake.calls == ["VID_1"]
    store = Store(tmp_path / "guetteur.db")
    try:
        assert store.get("VID_1").archived_at is not None  # type: ignore[union-attr]
        assert store.get("VID_2").archived_at is None  # type: ignore[union-attr]
    finally:
        store.close()
    assert "VID_1" in capsys.readouterr().out


def test_archive_disabled_returns_error(tmp_path: Path) -> None:
    """`archive.enabled = false` → retour 1 et message clair sur stderr."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n\n'
        '[[playlists]]\nid = "PL"\nlabel = "Veille"\n',
        encoding="utf-8",
    )
    code = cli(["--config", str(path), "archive", "--pending"])
    assert code == 1
