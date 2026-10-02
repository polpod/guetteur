"""Tests du filtre de redaction global (logs.py).

Trois surfaces :

- `redact()` : fonction pure appliquée aux motifs connus et aux valeurs lues de
  l'environnement par `setup_logging()`.
- `JsonFormatter.format()` : la ligne JSON écrite sur stdout passe par `redact`.
- Les messages sortants Telegram (notifier et bot) héritent du même filtre —
  testés ici via le notifier historique.
"""

from __future__ import annotations

import io
import json
import logging
import os

import httpx
import pytest

from guetteur.logs import (
    JsonFormatter,
    _reset_registered_secrets_for_tests,
    redact,
    register_secret,
    setup_logging,
)
from guetteur.notify.base import Message
from guetteur.notify.telegram import TelegramNotifier


@pytest.fixture(autouse=True)
def _clean_secrets() -> None:
    _reset_registered_secrets_for_tests()


_FAKE_GOOGLE_KEY = "AIzaSy" + "A" * 33  # 4 + 35 = 39, format Google valide
_FAKE_TELEGRAM_TOKEN = "1234567890:" + "A" * 35  # 10 chiffres + ':' + 35 chars
_FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 20
_FAKE_WA_TOKEN = "EAAG" + "F" * 50


def test_redact_google_api_key_pattern() -> None:
    text = f"ping avec clé {_FAKE_GOOGLE_KEY} OK"
    assert "AIza" not in redact(text)
    assert "[REDACTED]" in redact(text)


def test_redact_telegram_bot_token_pattern() -> None:
    text = f"erreur bot : https://api.telegram.org/bot{_FAKE_TELEGRAM_TOKEN}/sendMessage"
    out = redact(text)
    assert _FAKE_TELEGRAM_TOKEN not in out
    assert "[REDACTED]" in out


def test_redact_anthropic_api_key_pattern() -> None:
    text = f"stacktrace: Authorization: Bearer {_FAKE_ANTHROPIC_KEY} refusée"
    out = redact(text)
    assert _FAKE_ANTHROPIC_KEY not in out
    assert "[REDACTED]" in out


def test_redact_url_key_param_keeps_prefix() -> None:
    url = (
        "Server error '500' for url "
        "'https://www.googleapis.com/youtube/v3/playlistItems"
        f"?part=snippet&playlistId=PLx&key={_FAKE_GOOGLE_KEY}&maxResults=50'"
    )
    out = redact(url)
    assert "AIza" not in out
    assert "key=[REDACTED]" in out
    # Les autres paramètres sont conservés.
    assert "playlistId=PLx" in out
    assert "part=snippet" in out


def test_redact_registered_secret_exact_value() -> None:
    register_secret("super-secret-token-value")
    out = redact("payload=super-secret-token-value trailing")
    assert "super-secret-token-value" not in out
    assert "[REDACTED]" in out


def test_registered_secret_ignores_short_values() -> None:
    # Un secret de moins de 8 caractères serait trop présent dans les logs.
    register_secret("short")
    text = "le mot short apparaît ici"
    assert redact(text) == text  # inchangé


def test_redact_idempotent() -> None:
    text = f"clé {_FAKE_GOOGLE_KEY} encore"
    once = redact(text)
    twice = redact(once)
    assert once == twice


def test_redact_empty_and_no_match() -> None:
    assert redact("") == ""
    assert redact("hello world") == "hello world"


def test_json_formatter_redacts_message_and_extras() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="échec sur https://www.googleapis.com/youtube/v3/playlistItems"
        f"?key={_FAKE_GOOGLE_KEY}&playlistId=PL",
        args=None,
        exc_info=None,
    )
    out = formatter.format(record)
    payload = json.loads(out)
    assert "AIza" not in out
    assert "key=[REDACTED]" in payload["event"]


def test_json_formatter_redacts_exception_text() -> None:
    """Le piège originel : httpx.HTTPStatusError formatte l'URL complète dans
    son message, qui termine dans `exc` via logger.exception. Doit être redacté."""
    try:
        request = httpx.Request(
            "GET",
            "https://www.googleapis.com/youtube/v3/playlistItems"
            f"?key={_FAKE_GOOGLE_KEY}&playlistId=PL",
        )
        response = httpx.Response(500, request=request)
        response.raise_for_status()
    except httpx.HTTPStatusError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="x",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=None,
        exc_info=exc_info,
    )
    out = JsonFormatter().format(record)
    payload = json.loads(out)
    assert "AIza" not in out
    assert "key=[REDACTED]" in payload["exc"]


def test_setup_logging_registers_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", _FAKE_GOOGLE_KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _FAKE_TELEGRAM_TOKEN)
    monkeypatch.setenv("WA_TOKEN", _FAKE_WA_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_ANTHROPIC_KEY)

    # Capture la sortie du handler installé par setup_logging.
    buffer = io.StringIO()
    monkeypatch.setattr("sys.stdout", buffer)
    setup_logging("INFO")
    logging.getLogger("guetteur.test").error(
        "secrets : %s %s %s %s",
        os.environ["YOUTUBE_API_KEY"],
        os.environ["TELEGRAM_BOT_TOKEN"],
        os.environ["WA_TOKEN"],
        os.environ["ANTHROPIC_API_KEY"],
    )
    out = buffer.getvalue()
    assert _FAKE_GOOGLE_KEY not in out
    assert _FAKE_TELEGRAM_TOKEN not in out
    assert _FAKE_WA_TOKEN not in out
    assert _FAKE_ANTHROPIC_KEY not in out
    assert out.count("[REDACTED]") >= 4


def test_telegram_notifier_redacts_outgoing_text() -> None:
    """Une alerte Telegram qui porte accidentellement un secret ne quitte pas le
    processus en clair : `TelegramNotifier._post` redacte `text` avant l'envoi."""
    bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier("TOKEN", "42", client)
    leaking = (
        "YouTube quota : plafond atteint. Dernière erreur : "
        f"key={_FAKE_GOOGLE_KEY} refusée."
    )
    notifier.send(Message(markdown_v2=leaking, plain=leaking, short=leaking))

    assert bodies, "aucun appel sendMessage ?"
    for body in bodies:
        assert _FAKE_GOOGLE_KEY not in body["text"]
        assert "key=[REDACTED]" in body["text"]
