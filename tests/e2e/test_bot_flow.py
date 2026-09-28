"""E2E bot Telegram (Lot 5) — flow complet avec un faux serveur Telegram en mémoire.

Scénario :
1. Le pipeline envoie un résumé standard (playlist detail = « standard ») → boutons
   sous le message.
2. L'utilisateur appuie sur « Détaillé » → answerCallbackQuery immédiat, génération,
   envoi du résumé détaillé (plusieurs parts numérotées) avec ses propres boutons.
3. L'utilisateur répond (reply) à une des parts avec une question → réponse Q&A avec
   timestamps cliquables.
4. Second appui sur « Détaillé » → servi depuis le cache, Claude n'est PAS rappelé."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from guetteur.config import PlaylistConfig, Secrets, TelegramConfig
from guetteur.notify.telegram_bot import TelegramBot
from tests.e2e.world import NEW, World
from tests.helpers import make_config
from tests.test_telegram_bot import FakeApi, FakeNotifier


def _mock_summary_payloads() -> dict[str, dict[str, Any]]:
    return {
        "standard": {
            "title": "Titre standard",
            "tldr": "Une phrase. Deux.",
            "key_points": [{"seconds": i * 30, "text": f"Point {i}"} for i in range(6)],
            "why_it_matters": "Ça compte.",
            "announced_items": 0,
        },
        "detaille": {
            "title": "Titre détaillé",
            "tldr": "Un. Deux. Trois.",
            "sections": [
                {
                    "title": f"Section {i}",
                    "seconds": (i + 1) * 60,
                    "bullets": [f"Puce {i}.a", f"Puce {i}.b", f"Puce {i}.c"],
                }
                for i in range(4)
            ],
            "citations": [{"seconds": 120, "text": "Passage marquant reformulé."}],
            "actions": ["Fais A.", "Fais B.", "Fais C."],
            "reserves": ["Attention à X."],
            "announced_items": 0,
        },
    }


def _stub_summarize(**payloads: dict[str, Any]) -> Any:
    """Faux client anthropic qui retourne un payload selon le detail détecté dans le
    system prompt (heuristique simple : « detaille » présent → payload détaillé)."""
    call_count = [0]

    def create(**kwargs: Any) -> SimpleNamespace:
        call_count[0] += 1
        system = str(kwargs.get("system", ""))
        if "détaillé" in system.lower() or "detaille" in system.lower():
            body = payloads["detaille"]
        else:
            body = payloads["standard"]
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(body))],
        )

    client = MagicMock()
    client.messages.create.side_effect = create
    return client


def test_bot_flow_summary_button_qa_and_cache(tmp_path: Path) -> None:
    payloads = _mock_summary_payloads()
    telegram = TelegramConfig(interactive=True, rate_limit_per_hour=10, question_ttl_min=10)
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="standard"),),
        telegram=telegram,
        secrets=Secrets(telegram_bot_token="T", telegram_chat_id="42"),
    )
    world = World(config, "claude_api")
    world.claude.messages.create = _stub_summarize(**payloads).messages.create

    # === Étape 1 : cycle pipeline → un résumé standard envoyé avec boutons.
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    stats = world.pipeline.run_cycle()
    assert stats.sent == 1
    # Une seule part standard.
    assert len(world.telegram) == 1
    first_sent = world.telegram[0]
    assert first_sent.get("reply_markup") is not None
    # Les boutons omettent le niveau affiché.
    labels = [b["text"] for b in first_sent["reply_markup"]["inline_keyboard"][0]]
    assert "Standard" not in labels
    assert "Bref" in labels and "Détaillé" in labels and "Question" in labels
    # Le résumé standard est cached.
    assert world.store.cached_summary(NEW[0], "standard") is not None

    # === Étape 2 : appui sur « Détaillé ». Faux serveur pour le bot.
    api = FakeApi()
    bot_notifier = FakeNotifier(api)
    from guetteur.summarize import build_summarizer

    summarizer = build_summarizer(config)
    from guetteur.summarize.qa import ClaudeApiQuestionAnswerer

    answerer = ClaudeApiQuestionAnswerer(world.claude, config.claude_model)

    # Force le fake anthropic à répondre du texte pour la Q&A.
    def qa_create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=("Réponse : voir https://youtu.be/NEW00000001?t=60 pour le passage clé."),
                )
            ],
        )

    # Patch messages.create pour distinguer summary vs Q&A : garder l'existing create.
    original_create = world.claude.messages.create
    call_counter = {"n": 0}

    def counted_create(**kwargs: Any) -> SimpleNamespace:
        # Si le prompt utilisateur contient la balise <question>, c'est une Q&A ;
        # sinon c'est une génération de résumé (Lot 7 §3 : encadrement des données).
        content = str(kwargs.get("messages", [{}])[0].get("content", ""))
        if "<question>" in content:
            return qa_create(**kwargs)
        call_counter["n"] += 1
        result: SimpleNamespace = original_create(**kwargs)
        return result

    world.claude.messages.create = counted_create

    bot = TelegramBot(
        config=config,
        store=world.store,
        summarizer=summarizer,
        question_answerer=answerer,
        claude_lock=threading.Lock(),
        notifier=bot_notifier,  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="Veille"),
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{NEW[0]}:d:detaille",
                "message": {"chat": {"id": 42}, "message_id": 1000},
            }
        }
    )
    # answerCallbackQuery immédiat, avec message d'avancement.
    assert api.callback_answers[0]["id"] == "cb1"
    # Le résumé détaillé a été envoyé (au moins une part).
    assert len(api.messages_sent) >= 1
    # Résumé cached au niveau « detaille » désormais.
    assert world.store.cached_summary(NEW[0], "detaille") is not None
    # Chaque message envoyé par le bot est lié à la vidéo.
    sent_by_bot = list(api.messages_sent)
    for entry in sent_by_bot:
        linked = world.store.video_for_message(entry["message_id"])
        assert linked == NEW[0]
    # Le dernier envoi porte les boutons (omission de « Détaillé »).
    last_markup = sent_by_bot[-1]["reply_markup"]
    assert last_markup is not None
    labels = [b["text"] for b in last_markup["inline_keyboard"][0]]
    assert "Détaillé" not in labels

    # === Étape 3 : reply à une des parts avec une question.
    reply_target = sent_by_bot[0]["message_id"]
    initial_qa_count = call_counter["n"]
    bot.handle_update(
        {
            "message": {
                "chat": {"id": 42},
                "text": "Qu'est-ce que l'auteur recommande ?",
                "reply_to_message": {"message_id": reply_target},
            }
        }
    )
    # Une réponse a été envoyée.
    assert len(api.messages_sent) > len(sent_by_bot)
    answer_msg = api.messages_sent[-1]["text"]
    assert "youtu.be" in answer_msg or "https" in answer_msg  # timestamp cliquable
    # La Q&A est stockée en base.
    assert len(world.store.recent_qa(NEW[0])) == 1
    # Aucun nouvel appel « summary » de Claude (seul le Q&A a tourné).
    assert call_counter["n"] == initial_qa_count

    # === Étape 4 : second appui sur « Détaillé » → cache, aucun call summary.
    before = call_counter["n"]
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb2",
                "data": f"v:{NEW[0]}:d:detaille",
                "message": {"chat": {"id": 42}, "message_id": 1000},
            }
        }
    )
    assert call_counter["n"] == before  # cache servi, Claude non rappelé
