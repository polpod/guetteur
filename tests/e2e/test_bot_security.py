"""E2E bot Telegram (Lot 5) — sécurité : les updates depuis un chat_id inconnu sont
ignorées, aucun envoi ne fuite. Le rate-limit du log INFO est vérifié en cas de spam."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from guetteur.config import PlaylistConfig, Secrets, TelegramConfig
from guetteur.notify.telegram_bot import TelegramBot
from guetteur.store import Store
from tests.helpers import make_config
from tests.test_telegram_bot import FakeApi, FakeNotifier, FakeSummarizer


def _bot(tmp_path: Path, chat_id: str = "42") -> tuple[TelegramBot, FakeApi, Store]:
    telegram = TelegramConfig(interactive=True)
    config = make_config(
        tmp_path,
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="T", telegram_chat_id=chat_id),
    )
    store = Store(tmp_path / "guetteur.db")
    api = FakeApi()
    bot = TelegramBot(
        config=config,
        store=store,
        summarizer=FakeSummarizer(),
        question_answerer=lambda t, q, h: "R",
        claude_lock=threading.Lock(),
        notifier=FakeNotifier(api),  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="V"),
    )
    return bot, api, store


def test_message_from_unknown_chat_is_silently_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot, api, store = _bot(tmp_path)
    caplog.set_level("INFO", logger="guetteur.notify.telegram_bot")
    try:
        bot.handle_update(
            {
                "update_id": 42,
                "message": {"chat": {"id": 99999}, "text": "coucou"},
            }
        )
    finally:
        store.close()
    # AUCUN envoi (sécurité : ne pas répondre à un inconnu).
    assert api.messages_sent == []
    assert api.callback_answers == []
    # Un log INFO trace l'incident avec l'id inconnu.
    unknown = [r for r in caplog.records if r.getMessage() == "telegram_bot.unknown_chat"]
    assert len(unknown) == 1
    assert getattr(unknown[0], "chat_id", None) == 99999


def test_callback_from_unknown_chat_is_silently_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot, api, store = _bot(tmp_path)
    caplog.set_level("INFO", logger="guetteur.notify.telegram_bot")
    try:
        bot.handle_update(
            {
                "callback_query": {
                    "id": "cb1",
                    "data": "v:ABC:d:bref",
                    "message": {"chat": {"id": 12345}, "message_id": 1},
                }
            }
        )
    finally:
        store.close()
    # Ni sendMessage, ni answerCallbackQuery : le bot est muet.
    assert api.messages_sent == []
    assert api.callback_answers == []
    assert any(r.getMessage() == "telegram_bot.unknown_chat" for r in caplog.records)


def test_repeat_unknown_chat_is_logged_only_once_per_hour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot, api, store = _bot(tmp_path)
    caplog.set_level("INFO", logger="guetteur.notify.telegram_bot")
    try:
        for _ in range(20):
            bot.handle_update({"message": {"chat": {"id": 42_424_242}, "text": "spam"}})
    finally:
        store.close()
    logs = [r for r in caplog.records if r.getMessage() == "telegram_bot.unknown_chat"]
    assert len(logs) == 1  # 1 seul log par heure et par id inconnu, malgré 20 tentatives
    assert api.messages_sent == []
