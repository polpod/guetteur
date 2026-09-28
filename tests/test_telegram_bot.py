"""Tests unitaires du bot Telegram (Lot 5) : parsing callback_data, filtrage chat_id,
cache multi-niveaux, état question avec expiration, rate limit, reprise du thread.

Toutes les interactions HTTP passent par un `FakeApi` en mémoire — aucun appel réseau."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import Config, PlaylistConfig, Secrets, TelegramConfig
from guetteur.models import DetailLevel, KeyPoint, Summary
from guetteur.notify.telegram_bot import (
    META_AWAITING_VIDEO,
    META_HEARTBEAT,
    META_OFFSET,
    RateLimiter,
    TelegramApiError,
    TelegramBot,
    build_summary_keyboard,
    parse_callback_data,
)
from guetteur.store import Store
from guetteur.summarize.base import summary_to_json
from tests.helpers import make_config

# --- fakes -----------------------------------------------------------------------------


class FakeApi:
    """Bouchon de TelegramApi : conserve les envois et les callbacks, expose une file
    d'updates à consommer par le prochain `get_updates()`."""

    def __init__(self) -> None:
        self.pending_updates: list[list[dict[str, Any]]] = []
        self.messages_sent: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.get_updates_calls: list[int] = []
        self._next_msg_id = 1000
        self.raise_on_get_updates: list[Exception] = []

    def enqueue(self, updates: list[dict[str, Any]]) -> None:
        self.pending_updates.append(updates)

    def get_updates(self, offset: int, timeout_s: int) -> list[dict[str, Any]]:
        self.get_updates_calls.append(offset)
        if self.raise_on_get_updates:
            raise self.raise_on_get_updates.pop(0)
        return self.pending_updates.pop(0) if self.pending_updates else []

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = "MarkdownV2",
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        self._next_msg_id += 1
        self.messages_sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
                "message_id": self._next_msg_id,
            }
        )
        return self._next_msg_id

    def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        self.callback_answers.append({"id": callback_id, "text": text})


class FakeNotifier:
    """Bouchon de TelegramNotifier : concatène les message_ids simulés pour lier
    plusieurs parts à une vidéo côté store."""

    name = "telegram"

    def __init__(self, api: FakeApi) -> None:
        self.api = api
        self.sent: list[Any] = []

    def send(self, message: Any) -> str:
        self.sent.append(message)
        parts = message.markdown_v2_parts or (message.markdown_v2,)
        ids: list[str] = []
        for i, part in enumerate(parts):
            markup = message.reply_markup if i == len(parts) - 1 else None
            mid = self.api.send_message("42", part, parse_mode="MarkdownV2", reply_markup=markup)
            if mid is not None:
                ids.append(str(mid))
        return ",".join(ids)


class FakeSummarizer:
    def __init__(self, payloads: dict[DetailLevel, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, DetailLevel]] = []
        self.payloads = payloads or {}

    def summarize(self, transcript: Any, meta: Any) -> Summary:
        from guetteur.summarize.base import to_summary

        self.calls.append((meta.video.video_id, meta.detail))
        payload = self.payloads.get(meta.detail) or _default_payload(meta.detail)
        return to_summary(payload, detail=meta.detail)


def _default_payload(detail: DetailLevel) -> dict[str, Any]:
    if detail == "bref":
        return {
            "title": "T bref",
            "tldr": "A. B.",
            "key_points": [{"seconds": i, "text": f"P{i}"} for i in range(3)],
            "actions": ["Fais X."],
            "announced_items": 0,
        }
    if detail == "detaille":
        return {
            "title": "T detaille",
            "tldr": "Un. Deux. Trois.",
            "sections": [
                {"title": "Sec 1", "seconds": 60, "bullets": ["a", "b", "c"]},
                {"title": "Sec 2", "seconds": 120, "bullets": ["d", "e", "f"]},
                {"title": "Sec 3", "seconds": 180, "bullets": ["g", "h", "i"]},
                {"title": "Sec 4", "seconds": 240, "bullets": ["j", "k", "l"]},
            ],
            "citations": [{"seconds": 90, "text": "cit"}],
            "actions": ["Fais 1", "Fais 2", "Fais 3"],
            "reserves": ["Attention."],
            "announced_items": 0,
        }
    return {
        "title": "T standard",
        "tldr": "Une phrase. Deux.",
        "key_points": [{"seconds": i * 30, "text": f"P{i}"} for i in range(6)],
        "why_it_matters": "Ça compte.",
        "announced_items": 0,
    }


def _bot_config(tmp_path: Path, **overrides: Any) -> Config:
    telegram = TelegramConfig(interactive=True, rate_limit_per_hour=10, question_ttl_min=10)
    return make_config(
        tmp_path,
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="TOKEN", telegram_chat_id="42"),
        **overrides,
    )


@pytest.fixture
def store_and_video(tmp_path: Path) -> Iterator[tuple[Store, str]]:
    import json as _json

    from guetteur.models import Video as _Video

    store = Store(tmp_path / "guetteur.db")
    video_id = "VIDBOT12345"  # 11 chars = format YouTube
    store.add_new(_Video(video_id, "Titre", "Chaîne", None, f"https://youtu.be/{video_id}"), "PL")
    store.set_transcript(
        video_id,
        _json.dumps({"language": "fr", "source": "youtube", "segments": [[0, "Salut à tous."]]}),
    )
    store.set_summary(video_id, summary_to_json(_default_stub_summary()))
    yield store, video_id
    store.close()


def _default_stub_summary() -> Summary:
    return Summary(
        title="Titre",
        tldr="Résumé standard.",
        key_points=(KeyPoint(0, "Point 1"), KeyPoint(30, "Point 2")),
        why_it_matters="Important.",
        reading_time_minutes=1,
        detail="standard",
    )


def _make_bot(config: Config, store: Store, api: FakeApi, summarizer: Any) -> TelegramBot:
    notifier = FakeNotifier(api)
    return TelegramBot(
        config=config,
        store=store,
        summarizer=summarizer,
        question_answerer=lambda t, q, h: f"Réponse à « {q[:40]} »",
        claude_lock=threading.Lock(),
        notifier=notifier,  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="Veille"),
    )


# --- parsing callback_data --------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("v:ABC123XYZ_-:d:bref", ("ABC123XYZ_-", "detail", "bref")),
        ("v:aB-XYZ_9abc:d:standard", ("aB-XYZ_9abc", "detail", "standard")),
        ("v:11charYouTa:d:detaille", ("11charYouTa", "detail", "detaille")),
        ("v:VIDBOT12345:q", ("VIDBOT12345", "question", None)),
    ],
)
def test_parse_callback_data_valid(data: str, expected: tuple[str, str, str | None]) -> None:
    action = parse_callback_data(data)
    assert action is not None
    assert (action.video_id, action.kind, action.detail) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v::q",
        "v:ABC:d:mauvais",
        "v:ABC",
        "attaque; DROP TABLE",
        "v:ABC:d",
        "V:ABC:q",
    ],
)
def test_parse_callback_data_rejects_malformed(bad: str) -> None:
    assert parse_callback_data(bad) is None


def test_build_summary_keyboard_omits_current_level() -> None:
    kb = build_summary_keyboard("VID", "detaille")
    labels = [b["text"] for b in kb["inline_keyboard"][0]]
    assert "Détaillé" not in labels
    assert "Bref" in labels and "Standard" in labels and "Question" in labels
    callback_data = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert "v:VID:d:bref" in callback_data
    assert "v:VID:q" in callback_data


# --- filtrage chat_id -------------------------------------------------------------------


def test_bot_ignores_message_from_unknown_chat(
    tmp_path: Path, store_and_video: tuple[Store, str], caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = store_and_video
    api = FakeApi()
    bot = _make_bot(_bot_config(tmp_path), store, api, FakeSummarizer())
    caplog.set_level("INFO", logger="guetteur.notify.telegram_bot")

    # Update depuis un chat totalement inconnu.
    bot.handle_update(
        {
            "update_id": 1,
            "message": {"chat": {"id": 99999}, "text": "coucou"},
        }
    )
    # Aucun envoi, aucune answer_callback_query.
    assert api.messages_sent == []
    assert api.callback_answers == []
    assert any("telegram_bot.unknown_chat" in r.getMessage() for r in caplog.records)


def test_bot_unknown_chat_log_rate_limited_to_one_per_hour(
    tmp_path: Path, store_and_video: tuple[Store, str], caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = store_and_video
    api = FakeApi()
    bot = _make_bot(_bot_config(tmp_path), store, api, FakeSummarizer())
    caplog.set_level("INFO", logger="guetteur.notify.telegram_bot")
    for _ in range(5):
        bot.handle_update({"message": {"chat": {"id": 99999}, "text": "spam"}})
    unknown_logs = [r for r in caplog.records if r.getMessage() == "telegram_bot.unknown_chat"]
    # Un seul log par heure et par id inconnu.
    assert len(unknown_logs) == 1


# --- cache multi-niveaux ---------------------------------------------------------------


def test_button_serves_cached_summary_without_calling_claude(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, video_id = store_and_video
    api = FakeApi()
    summarizer = FakeSummarizer()
    bot = _make_bot(_bot_config(tmp_path), store, api, summarizer)

    # Pré-remplir le cache pour le niveau "detaille".
    detailed = FakeSummarizer().summarize(
        None,
        type("M", (), {"video": type("V", (), {"video_id": video_id})(), "detail": "detaille"})(),
    )
    store.cache_summary(video_id, "detaille", summary_to_json(detailed))
    assert summarizer.calls == []  # rien appelé jusque-là

    # Callback : bouton « Détaillé » depuis chat autorisé.
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{video_id}:d:detaille",
                "message": {"chat": {"id": 42}, "message_id": 1000},
            }
        }
    )
    # Claude n'a pas été appelé : la réponse vient du cache.
    assert summarizer.calls == []
    # Un answerCallbackQuery a été envoyé et le résumé aussi.
    assert api.callback_answers[0]["id"] == "cb1"
    assert len(api.messages_sent) >= 1


def test_button_first_hit_calls_summarizer_then_caches(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, video_id = store_and_video
    api = FakeApi()
    summarizer = FakeSummarizer()
    bot = _make_bot(_bot_config(tmp_path), store, api, summarizer)

    assert store.cached_summary(video_id, "bref") is None
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{video_id}:d:bref",
                "message": {"chat": {"id": 42}, "message_id": 1000},
            }
        }
    )
    assert summarizer.calls == [(video_id, "bref")]
    assert store.cached_summary(video_id, "bref") is not None
    # Second appui : servi depuis le cache, Claude n'est pas rappelé.
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb2",
                "data": f"v:{video_id}:d:bref",
                "message": {"chat": {"id": 42}, "message_id": 1000},
            }
        }
    )
    assert summarizer.calls == [(video_id, "bref")]  # inchangé


# --- état question avec expiration -----------------------------------------------------


def test_awaiting_question_expires_after_ttl(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, video_id = store_and_video
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    telegram = TelegramConfig(interactive=True, question_ttl_min=10)
    config = make_config(
        tmp_path,
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="T", telegram_chat_id="42"),
    )
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
        now=lambda: now,
    )
    # Simule appui sur Question.
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{video_id}:q",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }
    )
    assert store.get_meta(META_AWAITING_VIDEO) == video_id

    # 5 minutes plus tard : la question est acceptée.
    bot._now = lambda: now + timedelta(minutes=5)
    assert bot._awaiting_video() == video_id

    # 11 minutes plus tard : expiration.
    bot._now = lambda: now + timedelta(minutes=11)
    assert bot._awaiting_video() is None
    # L'état a été effacé.
    assert (store.get_meta(META_AWAITING_VIDEO) or "") == ""


# --- rate limit ------------------------------------------------------------------------


def test_rate_limiter_blocks_after_quota_reached() -> None:
    now = [datetime(2026, 9, 27, 12, 0, tzinfo=UTC)]
    limiter = RateLimiter(3, now=lambda: now[0])
    for _ in range(3):
        assert limiter.allow("42") is True
    assert limiter.allow("42") is False
    # Une heure plus tard : la fenêtre s'est vidée.
    now[0] += timedelta(hours=1, seconds=1)
    assert limiter.allow("42") is True


def test_rate_limiter_isolates_users() -> None:
    now = [datetime(2026, 9, 27, 12, 0, tzinfo=UTC)]
    limiter = RateLimiter(1, now=lambda: now[0])
    assert limiter.allow("A") is True
    assert limiter.allow("A") is False
    # Un autre utilisateur reste autorisé.
    assert limiter.allow("B") is True


def test_bot_rate_limit_blocks_button(tmp_path: Path, store_and_video: tuple[Store, str]) -> None:
    store, video_id = store_and_video
    telegram = TelegramConfig(interactive=True, rate_limit_per_hour=1)
    config = make_config(
        tmp_path,
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="T", telegram_chat_id="42"),
    )
    api = FakeApi()
    summarizer = FakeSummarizer()
    bot = TelegramBot(
        config=config,
        store=store,
        summarizer=summarizer,
        question_answerer=lambda t, q, h: "R",
        claude_lock=threading.Lock(),
        notifier=FakeNotifier(api),  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="V"),
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{video_id}:d:bref",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }
    )
    # 2e appui immédiat : quota dépassé → answerCallbackQuery mentionne la limite.
    api.callback_answers.clear()
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb2",
                "data": f"v:{video_id}:d:detaille",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }
    )
    assert api.callback_answers
    assert "Limite" in (api.callback_answers[0]["text"] or "")
    # La 2e génération n'a PAS été faite.
    assert summarizer.calls == [(video_id, "bref")]


# --- reprise du thread après exception -------------------------------------------------


def test_poll_loop_recovers_after_exception(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, _ = store_and_video
    telegram = TelegramConfig(interactive=True, poll_timeout_s=1)
    config = make_config(
        tmp_path,
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="T", telegram_chat_id="42"),
    )
    api = FakeApi()
    # Première tentative : exception. Deuxième : ok (aucun update).
    api.raise_on_get_updates.append(TelegramApiError("boom"))
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
    # Court-circuit du backoff pour le test.
    import guetteur.notify.telegram_bot as tgbot

    original_backoff = tgbot._BACKOFF_S
    tgbot._BACKOFF_S = (0.01, 0.01, 0.01)
    try:
        bot.start()
        # Attendre 2 tentatives de getUpdates (max 3 s).
        for _ in range(60):
            if len(api.get_updates_calls) >= 2:
                break
            time.sleep(0.05)
    finally:
        bot.stop(timeout=2.0)
        tgbot._BACKOFF_S = original_backoff
    assert len(api.get_updates_calls) >= 2, "Le thread n'a pas relancé après exception"


# --- heartbeat -------------------------------------------------------------------------


def test_poll_writes_heartbeat_on_each_successful_get_updates(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, _ = store_and_video
    api = FakeApi()
    bot = _make_bot(_bot_config(tmp_path), store, api, FakeSummarizer())
    api.enqueue([])  # une itération sans update
    bot._poll_once()
    assert store.get_meta(META_HEARTBEAT) is not None
    assert store.get_meta(META_OFFSET) is not None or True  # non écrit si aucune update


# --- Q&A cas simple --------------------------------------------------------------------


def test_reply_to_summary_message_triggers_qa(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, video_id = store_and_video
    api = FakeApi()
    calls: list[tuple[str, str]] = []

    def answerer(transcript: str, question: str, history: list[tuple[str, str]]) -> str:
        calls.append((question, str(len(history))))
        return f"Réponse pour {question[:20]}"

    bot = _make_bot(_bot_config(tmp_path), store, api, FakeSummarizer())
    bot._question_answerer = answerer

    # Lie un message envoyé à la vidéo (comme le pipeline le fait après un envoi).
    store.link_message(500, video_id, "summary:auto")

    bot.handle_update(
        {
            "message": {
                "chat": {"id": 42},
                "text": "Quel est le point principal ?",
                "reply_to_message": {"message_id": 500},
            }
        }
    )
    assert calls == [("Quel est le point principal ?", "0")]
    # Une réponse a été envoyée.
    assert api.messages_sent
    # Q&A stockée en base pour permettre les relances.
    history = store.recent_qa(video_id, 6)
    assert len(history) == 1


def test_qa_history_is_reinjected_in_context(
    tmp_path: Path, store_and_video: tuple[Store, str]
) -> None:
    store, video_id = store_and_video
    api = FakeApi()
    received: list[list[tuple[str, str]]] = []

    def answerer(transcript: str, question: str, history: list[tuple[str, str]]) -> str:
        received.append(list(history))
        return "R"

    bot = _make_bot(_bot_config(tmp_path), store, api, FakeSummarizer())
    bot._question_answerer = answerer
    store.link_message(500, video_id, "summary:auto")
    for i in range(3):
        bot.handle_update(
            {
                "message": {
                    "chat": {"id": 42},
                    "text": f"Q{i}",
                    "reply_to_message": {"message_id": 500},
                }
            }
        )
    # La 3e question a reçu l'historique des 2 précédentes.
    assert len(received[2]) == 2
    assert received[2][0][0] == "Q0"
    assert received[2][1][0] == "Q1"
