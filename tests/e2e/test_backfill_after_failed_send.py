"""Bug constaté en réel (Lot 1) : l'envoi Telegram d'une vidéo échoue (404, puis 403, puis 400
« chat not found »), la vidéo finit « failed » ; un backfill suivant loggue
« video.already_sent » et ne renvoie rien. Cause : le backfill repassait la vidéo en « new »
alors qu'elle avait déjà un résumé, et la réclamation d'envoi n'acceptait pas ce statut ; la
vidéo restait bloquée en « new » pour toujours."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from guetteur.store import Status
from tests.e2e.world import NEW, World
from tests.helpers import make_config

TELEGRAM_ERRORS = [
    (404, "Not Found"),
    (403, "Forbidden: bot was blocked by the user"),
    (400, "Bad Request: chat not found"),
]


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_failed_send_is_not_reported_already_sent_and_backfill_force_resends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="guetteur")
    world = World(make_config(tmp_path), "claude_api")
    world.pipeline.run_cycle()
    world.feed.append(NEW)

    errors = list(TELEGRAM_ERRORS)

    def broken_chat(_request: httpx.Request) -> httpx.Response:
        code, description = errors.pop(0) if errors else TELEGRAM_ERRORS[-1]
        return httpx.Response(code, json={"ok": False, "description": description})

    world.replace_telegram(broken_chat)
    for _ in range(4):  # les 4 cycles de l'incident réel
        world.pipeline.run_cycle()

    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.FAILED
    # 404 = erreur de configuration : échec définitif dès la 1re tentative, pas 4 cycles.
    assert len(world.store.deliveries(NEW[0])) == 1
    assert "HTTP 404" in (rec.last_error or "")
    assert rec.summary is not None

    # Le chat est corrigé. Un backfill simple ne ment plus : « failed », pas « déjà envoyée ».
    world.replace_telegram(world._telegram_ok)
    caplog.clear()
    stats = world.pipeline.backfill("PLtest123", limit=5)
    events = _events(caplog)
    assert "video.already_sent" not in events and "backfill.already_sent" not in events
    assert "backfill.skipped_failed" in events
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.FAILED
    assert not any(NEW[0] in str(m) for m in world.telegram)
    backfilled_old = stats.sent  # les vidéos ignorées au 1er lancement, elles, partent

    # backfill --force reprend la vidéo « failed » et l'envoie, sans rappeler Claude.
    llm_before = world.llm_calls
    stats = world.pipeline.backfill("PLtest123", limit=5, force=True)
    assert stats.sent == 1
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT and rec.sent_at is not None
    assert world.llm_calls == llm_before
    assert len(world.telegram) == backfilled_old + 1

    # Un nouveau backfill --force ne renvoie jamais une vidéo réellement envoyée.
    caplog.clear()
    assert world.pipeline.backfill("PLtest123", limit=5, force=True).sent == 0
    assert "backfill.already_sent" in _events(caplog)


def test_video_stuck_in_new_with_summary_is_sent(tmp_path: Path) -> None:
    """Séquelle du bug dans une base existante : vidéo « new » AVEC résumé, que l'ancien code
    ne pouvait plus réclamer. Elle part désormais au cycle suivant."""
    world = World(make_config(tmp_path), "claude_api")
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    world.replace_telegram(lambda _r: httpx.Response(400, json={"ok": False}))
    world.pipeline.run_cycle()
    world.replace_telegram(world._telegram_ok)

    raw = sqlite3.connect(world.config.db_path)
    raw.execute("UPDATE videos SET status = 'new', retries = 0 WHERE video_id = ?", (NEW[0],))
    raw.commit()
    raw.close()

    assert world.pipeline.run_cycle().sent == 1
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT
    assert world.llm_calls == 1
