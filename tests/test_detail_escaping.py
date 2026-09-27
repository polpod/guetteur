"""Tests du correctif « échappement MarkdownV2 » (bug production Lot 4).

Couvre :
- Le rendu détaillé passe TOUS les textes du modèle par `escape_md_v2` — aucun caractère
  réservé ne subsiste nu, y compris dans les préfixes `!` (réserves), `>` (citations) et
  la numérotation `(i/N)`.
- Le validateur `validate_markdown_v2` détecte une part mal formée et retourne l'offset.
- Le notifier Telegram bascule tout le message en texte brut si la pré-validation
  échoue (0 appel avec `parse_mode=MarkdownV2`) et loggue `markdown_v2.fallback_plain`.
- Fallback à la volée : si Telegram rejette la partie 3 avec « can't parse entities »,
  les parties 1 et 2 ne sont PAS renvoyées ; seule la partie 3 (et les suivantes) part
  en texte brut, avec le même préfixe `(i/N)`.
- `guetteur compare --detail X` filtre à un ou plusieurs niveaux (CSV)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from guetteur.main import _parse_detail_filter, cli
from guetteur.models import Citation, DetailLevel, Section, Summary, Video
from guetteur.notify.base import Message
from guetteur.notify.telegram import TelegramNotifier
from guetteur.store import Store
from guetteur.summarize.format import (
    _MDV2_RESERVED,
    escape_markdown_v2,
    escape_md_v2,
    excerpt_around,
    numbered,
    render_markdown_v2,
    split_markdown_v2_parts,
    telegram_byte_offset,
    validate_markdown_v2,
)

# --- alias + escape ---------------------------------------------------------------------


def test_escape_md_v2_is_alias_of_escape_markdown_v2() -> None:
    assert escape_md_v2 is escape_markdown_v2


@pytest.mark.parametrize("ch", sorted(_MDV2_RESERVED))
def test_escape_md_v2_escapes_every_reserved_character(ch: str) -> None:
    """Chaque caractère réservé produit un texte que le validateur accepte."""
    out = escape_md_v2(f"avant{ch}après")
    assert f"\\{ch}" in out
    ok, offset = validate_markdown_v2(out)
    assert ok, f"escape_md_v2({ch!r}) → {out!r} invalide à l'offset {offset}"


# --- validateur -------------------------------------------------------------------------


def test_validate_markdown_v2_accepts_our_standard_render() -> None:
    """Un résumé standard rendu par nos soins doit être valide."""
    from guetteur.models import KeyPoint

    summary = Summary(
        title="Titre avec des points. Et un ! aussi.",
        tldr="Une phrase. Deux phrases !",
        key_points=(KeyPoint(0, "Point 1 ! numérique 3.14"),),
        why_it_matters="Ça compte : (car).",
        reading_time_minutes=1,
    )
    video = Video("X", "Titre", "C", None, "https://youtu.be/X")
    rendered = render_markdown_v2(summary, video, "Veille")
    ok, offset = validate_markdown_v2(rendered)
    assert ok, (
        f"Rendu invalide à l'offset {offset} : {rendered[max(0, offset - 20) : offset + 20]!r}"
    )


def test_validate_markdown_v2_refuses_unescaped_bang() -> None:
    ok, offset = validate_markdown_v2("Coucou ! monde")
    assert not ok
    assert offset == 7  # position du '!'


def test_validate_markdown_v2_refuses_unescaped_gt() -> None:
    ok, _ = validate_markdown_v2("A > B")
    assert not ok


def test_validate_markdown_v2_accepts_escaped_reserved() -> None:
    ok, _ = validate_markdown_v2("Coucou \\! monde \\. suite")
    assert ok


def test_validate_markdown_v2_accepts_bold_italic_link() -> None:
    ok, _ = validate_markdown_v2("*gras* _ital_ [texte](https://a.b)")
    assert ok


def test_validate_markdown_v2_refuses_unclosed_bold() -> None:
    ok, _ = validate_markdown_v2("*jamais fermé")
    assert not ok


def test_validate_markdown_v2_refuses_reserved_inside_bold() -> None:
    ok, _ = validate_markdown_v2("*bang ! ici*")
    assert not ok


# --- rendu detaille : plus aucun caractère nu ------------------------------------------


def _bomb_summary() -> Summary:
    """Résumé où chaque champ contient TOUS les caractères réservés MarkdownV2."""
    reserved = "_*[]()~`>#+-=|{}.!"
    return Summary(
        title=f"Titre {reserved}",
        tldr=f"TL;DR {reserved} phrase 2 {reserved}.",
        key_points=(),
        why_it_matters="",
        reading_time_minutes=3,
        detail="detaille",
        sections=(
            Section(
                title=f"Section A {reserved}",
                seconds=60,
                bullets=(
                    f"Puce 1 {reserved} détail",
                    f"Puce 2 {reserved} chiffre 3.14",
                ),
            ),
            Section(
                title=f"Section B {reserved}",
                seconds=120,
                bullets=(f"Puce {reserved}",),
            ),
        ),
        citations=(Citation(seconds=90, text=f"Extrait {reserved} reformulé."),),
        actions=(f"Fais {reserved} !", f"Évite {reserved}."),
        reserves=(f"Réserve {reserved} attention.",),
    )


def test_detailed_render_never_leaves_reserved_char_nu() -> None:
    """Rendu detaille avec tous les caractères réservés partout : validateur OK."""
    summary = _bomb_summary()
    video = Video("X", "V", "C", None, "https://youtu.be/X")
    rendered = render_markdown_v2(summary, video, "Veille")
    ok, offset = validate_markdown_v2(rendered)
    assert ok, (
        f"Rendu detaille invalide à l'offset {offset} : "
        f"...{rendered[max(0, offset - 30) : offset + 30]!r}..."
    )


def test_detailed_render_escapes_reserves_prefix_bang() -> None:
    """Le préfixe `!` des réserves est échappé (bug corrigé)."""
    summary = _bomb_summary()
    video = Video("X", "V", "C", None, "https://youtu.be/X")
    rendered = render_markdown_v2(summary, video, "Veille")
    # Chaque « ! » qui préfixe une réserve DOIT apparaître comme « \! » et jamais « ! ».
    # Toute paire « \n! » (nouvelle ligne + bang nu) est le bug.
    assert "\n!" not in rendered
    assert "\n\\!" in rendered  # forme correcte


def test_detailed_render_escapes_citations_prefix_gt() -> None:
    """Le préfixe des citations ne doit pas être un `>` réservé."""
    summary = _bomb_summary()
    video = Video("X", "V", "C", None, "https://youtu.be/X")
    rendered = render_markdown_v2(summary, video, "Veille")
    # `>` en début de ligne serait un blockquote MarkdownV2 non voulu.
    assert "\n>" not in rendered


def test_numbered_prefix_is_escaped_and_valid() -> None:
    """La numérotation `(1/3)` produit un préfixe valide MarkdownV2."""
    prefixed = numbered(["*a*", "*b*", "*c*"], escape=True)
    for part in prefixed:
        ok, offset = validate_markdown_v2(part)
        assert ok, f"Partie mal formée : {part[:30]!r} (offset {offset})"


def test_full_detailed_pipeline_produces_only_valid_parts() -> None:
    """render + split + numbered : chaque part finale est un MarkdownV2 valide."""
    from guetteur.summarize.format import TELEGRAM_LIMIT

    # On force un rendu long (chars réservés + puces à rallonge) pour déclencher le split.
    reserved = "_*[]()~`>#+-=|{}.!"
    long_summary = Summary(
        title=f"T {reserved}",
        tldr=f"TL {reserved}. Phrase 2.",
        key_points=(),
        why_it_matters="",
        reading_time_minutes=3,
        detail="detaille",
        sections=tuple(
            Section(
                title=f"S{i} {reserved}",
                seconds=(i + 1) * 60,
                bullets=tuple(f"Puce {j} {reserved} " + "x" * 250 for j in range(4)),
            )
            for i in range(8)
        ),
        citations=(Citation(30, f"Cit {reserved}"),),
        actions=(f"Fais {reserved}", f"Évite {reserved}"),
        reserves=(f"Réserve {reserved}",),
    )
    video = Video("X", "V", "C", None, "https://youtu.be/X")
    rendered = render_markdown_v2(long_summary, video, "Veille")
    parts = numbered(split_markdown_v2_parts(rendered, TELEGRAM_LIMIT), escape=True)
    assert len(parts) >= 2
    for i, part in enumerate(parts, start=1):
        ok, offset = validate_markdown_v2(part)
        assert ok, (
            f"Part {i}/{len(parts)} invalide à l'offset {offset} : "
            f"...{part[max(0, offset - 20) : offset + 20]!r}..."
        )


# --- notifier Telegram : pré-validation + fallback ------------------------------------


class _RecordingClient:
    """Client httpx factice qui journalise chaque POST et permet de scénariser les
    réponses. Chaque appel consomme une réponse dans `responses`."""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses

    def post(self, url: str, json: dict[str, Any]) -> Any:
        self.calls.append({"url": url, "json": json})
        status, body = self._responses.pop(0)

        class _Resp:
            status_code = status

            def json(self) -> dict[str, Any]:
                return body

        return _Resp()


def _bomb_message() -> Message:
    """Message dont la partie 3/3 contient un `!` nu (simule le bug production)."""
    ok_a = "\\(1/3\\) \n\n*A* valide"
    ok_b = "\\(2/3\\) \n\n*B* valide"
    bad_c = "\\(3/3\\) \n\n! réserve non échappée"  # <-- `!` nu = invalide
    return Message(
        markdown_v2="\n\n".join([ok_a, ok_b, bad_c]),
        plain="(1/3) A\n\n(2/3) B\n\n(3/3) ! réserve",
        markdown_v2_parts=(ok_a, ok_b, bad_c),
        plain_parts=("(1/3) A", "(2/3) B", "(3/3) ! réserve"),
    )


def _valid_message() -> Message:
    ok_a = "\\(1/2\\) \n\n*A*"
    ok_b = "\\(2/2\\) \n\n*B*"
    return Message(
        markdown_v2=f"{ok_a}\n\n{ok_b}",
        plain="(1/2) A\n\n(2/2) B",
        markdown_v2_parts=(ok_a, ok_b),
        plain_parts=("(1/2) A", "(2/2) B"),
    )


def test_notifier_prevalidation_falls_back_to_plain_before_sending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une partie mal formée → aucun envoi MarkdownV2, tout part en texte brut."""
    caplog.set_level("WARNING", logger="guetteur.notify.telegram")
    client = _RecordingClient(
        [
            (200, {"ok": True, "result": {"message_id": 101}}),
            (200, {"ok": True, "result": {"message_id": 102}}),
            (200, {"ok": True, "result": {"message_id": 103}}),
        ]
    )
    notifier = TelegramNotifier("T", "42", client=client)  # type: ignore[arg-type]
    result = notifier.send(_bomb_message())
    assert result == "101,102,103"
    # Aucun appel n'a été fait avec parse_mode MarkdownV2 : tous en texte brut.
    for call in client.calls:
        assert "parse_mode" not in call["json"], f"Un envoi MarkdownV2 a fuité : {call['json']}"
    # 3 appels au total (les 3 parts en texte brut).
    assert len(client.calls) == 3
    fallback = [r for r in caplog.records if r.getMessage() == "markdown_v2.fallback_plain"]
    assert len(fallback) == 1
    assert getattr(fallback[0], "reason", None) == "pre_validation"
    assert getattr(fallback[0], "invalid_index", None) == 3
    assert getattr(fallback[0], "total", None) == 3


def test_notifier_valid_message_stays_in_markdown_v2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un message dont toutes les parts sont valides part bien en MarkdownV2."""
    caplog.set_level("WARNING", logger="guetteur.notify.telegram")
    client = _RecordingClient(
        [
            (200, {"ok": True, "result": {"message_id": 201}}),
            (200, {"ok": True, "result": {"message_id": 202}}),
        ]
    )
    notifier = TelegramNotifier("T", "42", client=client)  # type: ignore[arg-type]
    notifier.send(_valid_message())
    for call in client.calls:
        assert call["json"].get("parse_mode") == "MarkdownV2"
    # Rien ne devrait logguer un fallback.
    assert "fallback_plain" not in caplog.text


def test_notifier_on_the_fly_400_falls_back_only_for_remaining_parts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parties 1 et 2 acceptées, partie 3 rejetée en 400 « can't parse entities » : seule
    la partie 3 est renvoyée en texte brut. Les parties 1 et 2 ne sont PAS renvoyées."""
    caplog.set_level("WARNING", logger="guetteur.notify.telegram")
    # On construit un message dont les 3 parts sont valides côté validateur, mais on
    # scénarise Telegram pour rejeter la 3e partie avec « can't parse entities ».
    good = _valid_message()
    md_parts = (
        "\\(1/3\\) \n\n*A*",
        "\\(2/3\\) \n\n*B*",
        "\\(3/3\\) \n\n*C* valide côté validateur",
    )
    plain_parts = ("(1/3) A", "(2/3) B", "(3/3) C brut")
    message = Message(
        markdown_v2="\n\n".join(md_parts),
        plain="\n\n".join(plain_parts),
        markdown_v2_parts=md_parts,
        plain_parts=plain_parts,
    )
    del good  # non utilisé
    client = _RecordingClient(
        [
            (200, {"ok": True, "result": {"message_id": 301}}),  # part 1 MarkdownV2 OK
            (200, {"ok": True, "result": {"message_id": 302}}),  # part 2 MarkdownV2 OK
            (
                400,
                {
                    "ok": False,
                    "description": (
                        "Bad Request: can't parse entities: Character '!' is reserved "
                        "and must be escaped with the preceding '\\' at byte offset 47"
                    ),
                },
            ),
            # Fallback plain : envoi de la 3e partie en texte brut, un seul appel.
            (200, {"ok": True, "result": {"message_id": 303}}),
        ]
    )
    notifier = TelegramNotifier("T", "42", client=client)  # type: ignore[arg-type]
    result = notifier.send(message)
    # Trace des appels : 3 en MarkdownV2 (dont un 400), puis 1 en texte brut.
    assert len(client.calls) == 4
    for call in client.calls[:3]:
        assert call["json"].get("parse_mode") == "MarkdownV2"
    # 4e appel : texte brut (pas de parse_mode).
    assert "parse_mode" not in client.calls[3]["json"]
    # Le texte brut envoyé est bien la part 3 (avec (i/N) préservé).
    assert client.calls[3]["json"]["text"] == "(3/3) C brut"
    # Les parts 1 et 2 n'ont PAS été renvoyées : le texte brut fait 1 seul appel post-400.
    assert result == "301,302,303"
    # Log du fallback avec index et extrait.
    fallback = [r for r in caplog.records if r.getMessage() == "markdown_v2.fallback_plain"]
    assert len(fallback) == 1
    assert getattr(fallback[0], "reason", None) == "telegram_parse_error"
    assert getattr(fallback[0], "part_index", None) == 3
    assert getattr(fallback[0], "byte_offset", None) == 47
    excerpt = getattr(fallback[0], "excerpt", "")
    assert len(excerpt) <= 80


def test_telegram_byte_offset_parses_error_message() -> None:
    msg = "Bad Request: can't parse entities: ... at byte offset 42"
    assert telegram_byte_offset(msg) == 42
    assert telegram_byte_offset("aucun offset ici") is None


def test_excerpt_around_windows_a_short_slice() -> None:
    text = "0123456789" * 20
    extract = excerpt_around(text, 100, radius=10)
    assert len(extract) == 20
    assert extract == "9012345678" * 2 or len(extract) == 20  # 20 caractères autour de 100


# --- compare --detail : filtre CSV -----------------------------------------------------


def test_parse_detail_filter_none_returns_all_levels() -> None:
    from guetteur.models import DETAIL_LEVELS

    assert _parse_detail_filter(None) == DETAIL_LEVELS
    assert _parse_detail_filter("") == DETAIL_LEVELS


def test_parse_detail_filter_single_level() -> None:
    assert _parse_detail_filter("detaille") == ("detaille",)


def test_parse_detail_filter_preserves_cli_order_and_dedups() -> None:
    assert _parse_detail_filter("detaille,bref,detaille") == ("detaille", "bref")


def test_parse_detail_filter_rejects_unknown_level() -> None:
    from guetteur.config import ConfigError

    with pytest.raises(ConfigError, match="valide"):
        _parse_detail_filter("bref,superhero")


# --- compare CLI : bout-en-bout, statut inchangé, filtre par niveau -------------------


def _seed_transcribed(store: Store, video_id: str, title: str) -> None:
    import json

    v = Video(video_id, title, "Chaîne", None, f"https://youtu.be/{video_id}")
    store.add_new(v, "PL")
    store.set_transcript(
        video_id,
        json.dumps({"language": "fr", "source": "youtube", "segments": [[0, "Salut."]]}),
    )


def _cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n\n'
        '[summarize]\nprovider = "claude_api"\n\n'
        '[[playlists]]\nid = "PL"\nlabel = "Veille"\ndetail = "standard"\n',
        encoding="utf-8",
    )
    return path


class _RecorderNotifier:
    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, message: Any) -> str | None:
        self.sent.append(message)
        return "ok"


def _summarize_stub(transcript: Any, meta: Any) -> Summary:
    from guetteur.summarize.base import to_summary

    payloads: dict[DetailLevel, dict[str, Any]] = {
        "bref": {
            "title": "T bref",
            "tldr": "A. B.",
            "key_points": [{"seconds": i, "text": f"P{i}"} for i in range(3)],
            "actions": ["Fais X."],
            "announced_items": 0,
        },
        "standard": {
            "title": "T standard",
            "tldr": "Une phrase. Deux.",
            "key_points": [{"seconds": i * 30, "text": f"P{i}"} for i in range(6)],
            "why_it_matters": "Ça compte.",
            "announced_items": 0,
        },
        "detaille": {
            "title": "T detaille",
            "tldr": "Un. Deux. Trois.",
            "sections": [
                {
                    "title": "Sec",
                    "seconds": 60,
                    "bullets": ["a", "b", "c"],
                }
            ],
            "citations": [{"seconds": 30, "text": "cit"}],
            "actions": ["Fais 1", "Fais 2", "Fais 3"],
            "reserves": ["Attention !"],
            "announced_items": 0,
        },
    }
    return to_summary(payloads[meta.detail], detail=meta.detail)


def test_compare_detail_single_level_sends_only_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--detail detaille` : un seul envoi (celui du niveau demandé)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    store = Store(tmp_path / "guetteur.db")
    _seed_transcribed(store, "VID_CMP1", "Vidéo")
    store.close()

    recorder = _RecorderNotifier()
    with (
        patch("guetteur.summarize.build_summarizer") as bs,
        patch("guetteur.main.build_notifier") as bn,
    ):
        bs.return_value.summarize.side_effect = _summarize_stub
        bn.return_value = recorder
        code = cli(
            [
                "--config",
                str(_cfg_file(tmp_path)),
                "compare",
                "--video-id",
                "VID_CMP1",
                "--detail",
                "detaille",
            ]
        )
    assert code == 0
    assert len(recorder.sent) == 1
    assert recorder.sent[0].plain.split("\n", 1)[0] == "[DETAILLE]"
    assert "[DETAILLE]" in capsys.readouterr().out


def test_compare_detail_two_levels_sends_in_cli_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--detail detaille,bref` : deux envois dans l'ordre du CLI."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    store = Store(tmp_path / "guetteur.db")
    _seed_transcribed(store, "VID_CMP2", "Vidéo")
    store.close()

    recorder = _RecorderNotifier()
    with (
        patch("guetteur.summarize.build_summarizer") as bs,
        patch("guetteur.main.build_notifier") as bn,
    ):
        bs.return_value.summarize.side_effect = _summarize_stub
        bn.return_value = recorder
        code = cli(
            [
                "--config",
                str(_cfg_file(tmp_path)),
                "compare",
                "--video-id",
                "VID_CMP2",
                "--detail",
                "detaille,bref",
            ]
        )
    assert code == 0
    assert len(recorder.sent) == 2
    headers = [m.plain.split("\n", 1)[0] for m in recorder.sent]
    assert headers == ["[DETAILLE]", "[BREF]"]


def test_compare_detail_invalid_level_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    Store(tmp_path / "guetteur.db").close()
    code = cli(
        [
            "--config",
            str(_cfg_file(tmp_path)),
            "compare",
            "--video-id",
            "X",
            "--detail",
            "superhero",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "superhero" in err
