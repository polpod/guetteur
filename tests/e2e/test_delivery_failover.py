"""Canal principal (WhatsApp) en panne 5xx sur ses 3 tentatives : le canal de secours
(Telegram) reçoit le résumé, la vidéo est « sent » et deliveries trace les 4 tentatives."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from guetteur.config import NotifyConfig, PlaylistConfig, Secrets
from guetteur.store import Status
from tests.e2e.world import NEW, World
from tests.helpers import make_config


def _world(tmp_path: Path) -> World:
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", notify="whatsapp"),),
        notify=NotifyConfig(fallback="telegram"),
        secrets=Secrets(
            telegram_bot_token="T",
            telegram_chat_id="42",
            wa_token="W",
            wa_phone_id="P",
            wa_to="336",
        ),
    )
    return World(config, "claude_api")


def test_primary_5xx_three_times_then_fallback_delivers(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.pipeline.run_cycle()
    world.feed.append(NEW)

    whatsapp_calls: list[dict[str, object]] = []

    def whatsapp_down(request: httpx.Request) -> httpx.Response:
        whatsapp_calls.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"code": 2, "message": "Service unavailable"}})

    world.replace_whatsapp(whatsapp_down)
    stats = world.pipeline.run_cycle()

    assert stats.sent == 1 and stats.failed == 0
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT and rec.sent_at is not None
    assert len(whatsapp_calls) == 3  # 3 tentatives sur le principal
    assert world.sleeps == [2.0, 8.0]  # attentes croissantes entre elles
    assert len(world.telegram) == 1  # le secours a reçu le résumé
    assert world.telegram[0]["parse_mode"] == "MarkdownV2"

    deliveries = world.store.deliveries(NEW[0])
    assert len(deliveries) == 4
    assert [(d.channel, d.attempt, d.ok, d.is_fallback) for d in deliveries] == [
        ("whatsapp", 1, False, False),
        ("whatsapp", 2, False, False),
        ("whatsapp", 3, False, False),
        ("telegram", 1, True, True),
    ]
    assert all("503" in (d.error or "") for d in deliveries[:3])
    assert deliveries[3].provider_message_id == "1001"

    world.pipeline.run_cycle()  # idempotence : rien n'est renvoyé
    assert len(world.telegram) == 1 and len(whatsapp_calls) == 3


def test_both_channels_down_is_retried_next_cycle(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    down = httpx.Response(502, json={"ok": False, "error": {"code": 1, "message": "down"}})
    world.replace_whatsapp(lambda _r: down)
    world.replace_telegram(lambda _r: down)

    assert world.pipeline.run_cycle().sent == 0
    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SUMMARIZED and rec.retries == 1
    assert "whatsapp (passagère)" in (rec.last_error or "")
    assert "telegram (passagère)" in (rec.last_error or "")
    assert len(world.store.deliveries(NEW[0])) == 6

    world.replace_whatsapp(world._whatsapp_ok)  # le principal revient
    assert world.pipeline.run_cycle().sent == 1
    assert len(world.whatsapp) == 1 and world.llm_calls == 1
