"""Vidéos qui annoncent une liste numérotée (« 9 pièges », « top 10 ») : un point clé par
élément, dans l'ordre, limite relevée de 8 à 12."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from guetteur.models import Segment, Transcript, Video
from guetteur.summarize.base import (
    LIST_MAX_KEY_POINTS,
    MAX_KEY_POINTS,
    SUMMARY_SCHEMA,
    SYSTEM_PROMPT,
    ChunkedSummarizer,
    SummaryMeta,
    detect_announced_list,
    summary_from_json,
    summary_to_json,
)
from guetteur.summarize.claude_api import ClaudeApiSummarizer
from guetteur.summarize.claude_code import ClaudeCodeSummarizer
from guetteur.summarize.format import render_markdown_v2, render_plain
from tests.helpers import FakeProcess, FakeSpawn, cli_envelope, summary_payload

PITFALLS = [
    "Pas de sauvegarde hors site",
    "Conteneurs privilégiés partout",
    "Mots de passe par défaut",
    "Ports exposés sur Internet",
    "Aucune supervision",
    "Mises à jour jamais appliquées",
    "Un seul disque sans RAID",
    "Documentation inexistante",
    "Onduleur absent",
]
VIDEO = Video(
    "PIEGES00009",
    "Les 9 pièges du homelab à éviter absolument",
    "Homelab FR",
    None,
    "https://www.youtube.com/watch?v=PIEGES00009",
)
META = SummaryMeta(video=VIDEO, language="fr")
ITEM_LINE = re.compile(r"\[(\d+)s\] Piège n°(\d+) : ([^.]+)\.")


def nine_item_transcript() -> Transcript:
    segments = [Segment(0.0, "Salut ! Aujourd'hui, les 9 pièges du homelab, dans l'ordre.")]
    for i, name in enumerate(PITFALLS, start=1):
        start = 30.0 + (i - 1) * 75
        segments.append(Segment(start, f"Piège n°{i} : {name}."))
        segments.append(Segment(start + 30, "On en parle souvent, et voici pourquoi c'est grave."))
    segments.append(Segment(720.0, "Voilà, vous connaissez maintenant les neuf pièges."))
    return Transcript("PIEGES00009", "fr", "youtube", tuple(segments))


def model_answer(document: str) -> dict[str, Any]:
    """Modèle factice : un point clé par élément trouvé dans la transcription reçue."""
    items = ITEM_LINE.findall(document)
    return {
        "title": "Les 9 pièges du homelab",
        "tldr": "Neuf erreurs classiques fragilisent un homelab. Chacune a une parade simple.",
        "key_points": [
            {"seconds": int(sec), "text": f"Piège {num} — {name} : à corriger en priorité."}
            for sec, num, name in items
        ],
        "why_it_matters": "Éviter ces pièges protège vos données et votre réseau.",
        "announced_items": len(items),
    }


class ApiBackend:
    def __init__(self) -> None:
        self.client = MagicMock()

        def create(**kwargs: Any) -> SimpleNamespace:
            data = model_answer(kwargs["messages"][0]["content"])
            text = SimpleNamespace(type="text", text=json.dumps(data))
            return SimpleNamespace(stop_reason="end_turn", content=[text])

        self.client.messages.create.side_effect = create
        self.summarizer = ClaudeApiSummarizer(self.client, "m")

    def instruction(self) -> str:
        return str(self.client.messages.create.call_args.kwargs["messages"][0]["content"])

    def system(self) -> str:
        return str(self.client.messages.create.call_args.kwargs["system"])


class EchoProcess(FakeProcess):
    """Faux binaire claude qui construit sa réponse à partir de la transcription (stdin)."""

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin = input
        self._done = True
        data = model_answer((input or b"").decode())
        return cli_envelope(json.dumps(data)), b""


class CodeBackend:
    def __init__(self) -> None:
        self.spawn = FakeSpawn(EchoProcess())
        self.summarizer = ClaudeCodeSummarizer(model="m", spawn=self.spawn)

    def instruction(self) -> str:
        args = self.spawn.calls[0][0]
        return args[args.index("-p") + 1]

    def system(self) -> str:
        args = self.spawn.calls[0][0]
        return args[args.index("--system-prompt") + 1]


@pytest.mark.parametrize(
    "backend_cls", [ApiBackend, CodeBackend], ids=["claude_api", "claude_code"]
)
def test_nine_announced_items_all_appear_in_order(backend_cls: type[Any]) -> None:
    backend = backend_cls()
    summary = backend.summarizer.summarize(nine_item_transcript(), META)

    # Les 9 éléments sont présents, un point clé chacun, dans l'ordre : plus de coupure à 8.
    assert len(summary.key_points) == 9
    for i, (point, name) in enumerate(zip(summary.key_points, PITFALLS, strict=True), start=1):
        assert point.text.startswith(f"Piège {i} — {name}")
    seconds = [p.seconds for p in summary.key_points]
    assert seconds == sorted(seconds) and seconds[0] == 30

    # Les 9 survivent jusqu'aux messages envoyés (Telegram et WhatsApp).
    plain = render_plain(summary, VIDEO)
    markdown = render_markdown_v2(summary, VIDEO)
    for name in PITFALLS:
        assert name in plain
        assert name.split()[0] in markdown
    assert plain.count("https://youtu.be/PIEGES00009?t=") == 9

    # La consigne annonce la liste détectée et le prompt système porte la règle.
    assert "Liste numérotée annoncée : 9 éléments" in backend.instruction()
    assert "un point clé par élément" in backend.system()
    assert "sans en sauter" in backend.instruction()


def test_system_prompt_and_schema_carry_list_rule() -> None:
    assert "« 9 pièges »" in SYSTEM_PROMPT and "« top 10 »" in SYSTEM_PROMPT
    assert "dans l'ordre" in SYSTEM_PROMPT and "n'en saute aucun" in SYSTEM_PROMPT
    assert "12" in SYSTEM_PROMPT
    assert "announced_items" in SUMMARY_SCHEMA["required"]


class _Canned(ChunkedSummarizer):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.instructions: list[str] = []

    def _complete(self, instruction: str, document: str) -> dict[str, Any]:
        self.instructions.append(instruction)
        return self.payload


def _plain_meta(title: str) -> SummaryMeta:
    return SummaryMeta(Video("X", title, "C", None, "https://youtu.be/X"), "fr")


def _short_transcript() -> Transcript:
    return Transcript("X", "fr", "youtube", (Segment(0, "Bonjour."),))


def test_without_list_the_limit_stays_eight() -> None:
    summarizer = _Canned(summary_payload(11) | {"announced_items": 0})
    summary = summarizer.summarize(_short_transcript(), _plain_meta("Proxmox en 10 minutes"))
    assert len(summary.key_points) == MAX_KEY_POINTS
    assert "Liste numérotée" not in summarizer.instructions[0]


def test_model_reported_list_raises_limit_even_if_title_is_silent() -> None:
    summarizer = _Canned(summary_payload(10) | {"announced_items": 10})
    summary = summarizer.summarize(_short_transcript(), _plain_meta("Mes outils préférés"))
    assert len(summary.key_points) == 10


def test_list_limit_is_twelve() -> None:
    summarizer = _Canned(summary_payload(15) | {"announced_items": 15})
    summary = summarizer.summarize(_short_transcript(), _plain_meta("Top 15 des commandes"))
    assert len(summary.key_points) == LIST_MAX_KEY_POINTS
    assert "regroupe les derniers" in summarizer.instructions[0]


def test_stored_list_summary_is_not_truncated_on_reload() -> None:
    summarizer = _Canned(summary_payload(9) | {"announced_items": 9})
    summary = summarizer.summarize(_short_transcript(), _plain_meta("Les 9 pièges"))
    assert len(summary_from_json(summary_to_json(summary)).key_points) == 9


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Les 9 pièges du homelab", 9),
        ("Top 10 des outils Linux", 10),
        ("TOP-5 apps", 5),
        ("5 étapes pour installer Proxmox", 5),
        ("Neuf erreurs à éviter", 9),
        ("7 costly mistakes to avoid", 7),
        ("Les 4 bonnes pratiques d\u2019un admin", 4),
        ("Proxmox en 10 minutes", None),
        ("3 ans plus tard", None),
        ("Gemma 4 12B explained", None),
        ("Le 1er conseil", None),
    ],
)
def test_detect_announced_list_in_title(title: str, expected: int | None) -> None:
    assert detect_announced_list(title) == expected


def test_detect_announced_list_in_transcript_intro() -> None:
    intro = "[0s] Bienvenue. [4s] Je vais vous présenter mes 6 astuces pour gagner du temps."
    assert detect_announced_list("Gagner du temps", intro) == 6
    late = "[0s] Bonjour." + " bla" * 3000 + " [900s] les 6 astuces"
    assert detect_announced_list("Gagner du temps", late) is None
