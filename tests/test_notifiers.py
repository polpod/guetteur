from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from guetteur.config import WhatsAppConfig
from guetteur.notify.base import Message, NotifyError
from guetteur.notify.telegram import TELEGRAM_LIMIT, TelegramNotifier
from guetteur.notify.whatsapp_cloud import WhatsAppCloudNotifier


def _recorder(
    responses: list[httpx.Response],
) -> tuple[httpx.Client, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return responses.pop(0) if len(responses) > 1 else responses[0]

    return httpx.Client(transport=httpx.MockTransport(handler)), bodies


def test_telegram_splits_long_messages() -> None:
    client, bodies = _recorder([httpx.Response(200, json={"ok": True})])
    long_md = "\n".join(["ligne \\- " + "z" * 90] * 100)  # ~10 000 caractères
    TelegramNotifier("T", "42", client).send(Message(markdown_v2=long_md, plain="p"))

    assert len(bodies) == 3
    assert all(b["parse_mode"] == "MarkdownV2" and b["chat_id"] == "42" for b in bodies)
    assert all(len(b["text"]) <= TELEGRAM_LIMIT for b in bodies)


def test_telegram_error_raises() -> None:
    client, _ = _recorder([httpx.Response(400, json={"ok": False, "description": "bad"})])
    with pytest.raises(NotifyError, match="bad"):
        TelegramNotifier("T", "42", client).send(Message("x", "x"))


def test_telegram_requires_credentials() -> None:
    with pytest.raises(NotifyError):
        TelegramNotifier("", "42")


def test_whatsapp_sends_plain_text() -> None:
    client, bodies = _recorder([httpx.Response(200, json={"messages": [{"id": "wamid"}]})])
    WhatsAppCloudNotifier("T", "PHONE", "33600000000", client=client).send(
        Message(markdown_v2="*md*", plain="texte brut")
    )
    assert bodies == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "33600000000",
            "type": "text",
            "text": {"preview_url": True, "body": "texte brut"},
        }
    ]


def test_whatsapp_falls_back_to_template_when_window_closed() -> None:
    closed = httpx.Response(
        400, json={"error": {"code": 131047, "message": "Re-engagement message"}}
    )
    ok = httpx.Response(200, json={"messages": [{"id": "wamid"}]})
    client, bodies = _recorder([closed, ok])
    settings = WhatsAppConfig(
        template_name="nouveau_resume", template_language="fr", template_body_param=True
    )
    WhatsAppCloudNotifier("T", "PHONE", "336", settings=settings, client=client).send(
        Message(markdown_v2="x", plain="long\ntexte", short="Titre\n— https://youtu.be/x")
    )

    assert len(bodies) == 2
    tpl = bodies[1]["template"]
    assert bodies[1]["type"] == "template"
    assert tpl["name"] == "nouveau_resume"
    assert tpl["language"] == {"code": "fr"}
    assert tpl["components"][0]["parameters"][0]["text"] == "Titre — https://youtu.be/x"


def test_whatsapp_other_error_raises() -> None:
    client, _ = _recorder([httpx.Response(401, json={"error": {"code": 190, "message": "token"}})])
    with pytest.raises(NotifyError, match="190"):
        WhatsAppCloudNotifier("T", "P", "336", client=client).send(Message("x", "x"))
