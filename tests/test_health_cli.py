"""`guetteur health` (heartbeat, alerte limitée à une par heure), `status` et `reset`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guetteur.health import ALERT_EVERY, check_health, maybe_alert
from guetteur.main import cli
from guetteur.models import Video
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.store import Status, Store
from tests.helpers import make_config

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class Recorder(Notifier):
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Message] = []
        self.fail = fail

    def send(self, message: Message) -> str | None:
        if self.fail:
            raise NotifyError("Telegram HTTP 401 : Unauthorized")
        self.sent.append(message)
        return "1"


def _checks(store: Store, tmp_path: Path, now: datetime) -> dict[str, bool]:
    return {c.name: c.ok for c in check_health(make_config(tmp_path), store, now=now)}


def test_health_ko_without_heartbeat(store: Store, tmp_path: Path) -> None:
    assert _checks(store, tmp_path, NOW) == {"base": True, "heartbeat": False}


def test_health_heartbeat_threshold_is_three_intervals(store: Store, tmp_path: Path) -> None:
    store.beat(NOW)  # poll_interval_seconds = 300 → seuil 15 min
    assert _checks(store, tmp_path, NOW + timedelta(minutes=14))["heartbeat"] is True
    assert _checks(store, tmp_path, NOW + timedelta(minutes=16))["heartbeat"] is False


def test_alert_is_sent_at_most_once_per_hour(store: Store, tmp_path: Path) -> None:
    checks = check_health(make_config(tmp_path), store, now=NOW)  # KO : aucun cycle
    notifier = Recorder()

    assert maybe_alert(store, checks, lambda: notifier, now=NOW) == "sent"
    assert "healthcheck KO" in notifier.sent[0].plain
    assert "\\-" in notifier.sent[0].markdown_v2 or "\\." in notifier.sent[0].markdown_v2
    later = NOW + timedelta(minutes=45)
    assert maybe_alert(store, checks, lambda: notifier, now=later) == "throttled"
    after_hour = NOW + ALERT_EVERY + timedelta(seconds=1)
    assert maybe_alert(store, checks, lambda: notifier, now=after_hour) == "sent"
    assert len(notifier.sent) == 2


def test_no_alert_when_healthy(store: Store, tmp_path: Path) -> None:
    store.beat(NOW)
    checks = check_health(make_config(tmp_path), store, now=NOW)
    notifier = Recorder()
    assert maybe_alert(store, checks, lambda: notifier, now=NOW) == "ok"
    assert notifier.sent == []


def test_failed_alert_is_retried_at_next_timer_run(store: Store, tmp_path: Path) -> None:
    checks = check_health(make_config(tmp_path), store, now=NOW)
    assert maybe_alert(store, checks, lambda: Recorder(fail=True), now=NOW) == "error"
    # Rien n'a été envoyé : pas de verrou d'une heure, on retente au passage suivant.
    ok = Recorder()
    assert maybe_alert(store, checks, lambda: ok, now=NOW + timedelta(minutes=15)) == "sent"


# --- CLI -------------------------------------------------------------------------------


def _config_file(tmp_path: Path, interval: int = 300) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\npoll_interval_seconds = {interval}\n\n'
        '[[playlists]]\nid = "PL"\nlabel = "Veille IA"\n',
        encoding="utf-8",
    )
    return path


def _seed(tmp_path: Path) -> None:
    store = Store(tmp_path / "guetteur.db")
    store.add_new(Video("VID_FAILED", "Une vidéo dont l'envoi a échoué", "c", None, "u"), "PL")
    store.mark_failed("VID_FAILED", "telegram (définitive) : Telegram HTTP 400 : chat not found")
    store.initialize_playlist("PL", [Video("VID_OLD", "Ancienne", "c", None, "u")])
    store.beat()
    store.close()


def test_cli_status_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path)
    assert cli(["--config", str(_config_file(tmp_path)), "status"]) == 0
    out = capsys.readouterr().out
    for header in ("id", "titre", "playlist", "statut", "retries", "dernière erreur"):
        assert header in out
    assert "VID_FAILED" in out and "failed" in out and "chat not found" in out
    assert "Veille IA" in out  # libellé de la playlist
    assert "sent (ignorée)" in out  # vidéo ignorée au premier lancement
    assert "Total : failed=1, sent=1" in out

    assert cli(["--config", str(_config_file(tmp_path)), "status", "--status", "failed"]) == 0
    assert "VID_OLD" not in capsys.readouterr().out


def test_cli_reset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path)
    config = str(_config_file(tmp_path))
    assert cli(["--config", config, "reset", "--video-id", "VID_FAILED"]) == 0
    assert "remise en « new » (était : failed)" in capsys.readouterr().out
    store = Store(tmp_path / "guetteur.db")
    rec = store.get("VID_FAILED")
    store.close()
    assert rec is not None and rec.status is Status.NEW and rec.last_error is None
    assert cli(["--config", config, "reset", "--video-id", "INCONNUE"]) == 1


def test_cli_retry_requires_target(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli(["--config", str(_config_file(tmp_path)), "retry"])


def test_cli_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = str(_config_file(tmp_path))
    assert cli(["--config", config, "health"]) == 1  # base vide : aucun cycle
    assert "aucun cycle enregistré" in capsys.readouterr().out
    _seed(tmp_path)
    assert cli(["--config", config, "health"]) == 0
    out = capsys.readouterr().out
    assert "heartbeat" in out and "KO" not in out
