from __future__ import annotations

import re

import pytest

from guetteur.models import KeyPoint, Summary, Video
from guetteur.notify.base import split_message
from guetteur.summarize.format import (
    escape_markdown_v2,
    format_timestamp,
    reading_time_minutes,
    render_markdown_v2,
    render_plain,
    timestamp_url,
)

VIDEO = Video("abc_DEF-123", "Titre", "Chaîne (officielle)", None, "https://youtu.be/abc_DEF-123")
SUMMARY = Summary(
    title="Titre *gras* [crochets] v1.2!",
    tldr="Phrase un. Phrase deux.",
    key_points=(KeyPoint(0, "Début."), KeyPoint(754, "Milieu (important)."), KeyPoint(3725, "Fin")),
    why_it_matters="Parce que 2+2=4.",
    reading_time_minutes=2,
)

# Caractère spécial MarkdownV2 non précédé d'un backslash (hors entités voulues).
UNESCAPED = re.compile(r"(?<!\\)[_\[\]()~`>#+\-=|{}.!]")


# --- découpage ---------------------------------------------------------------------------


def test_short_message_not_split() -> None:
    assert split_message("bonjour", 4096) == ["bonjour"]


def test_split_respects_limit_and_preserves_content() -> None:
    lines = [f"Ligne {i} " + "x" * 50 for i in range(300)]
    text = "\n".join(lines)
    parts = split_message(text, 4096)

    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text  # coupe uniquement sur des fins de ligne


def test_split_very_long_line_on_spaces() -> None:
    text = " ".join(["mot"] * 3000)  # ~12 000 caractères sur une seule ligne
    parts = split_message(text, 4096)
    assert all(len(p) <= 4096 for p in parts)
    assert " ".join(parts).split() == text.split()


def test_split_never_breaks_escape_sequence() -> None:
    # Pas d'espace : coupe brute. Le caractère à la position limite est un « \\. ».
    text = "a" * 9 + "\\." + "b" * 20
    parts = split_message(text, 10)
    assert all(len(p) <= 10 for p in parts)
    assert "".join(parts) == text
    for p in parts:
        trailing = len(p) - len(p.rstrip("\\"))
        assert trailing % 2 == 0, f"échappement coupé dans {p!r}"


def test_split_invalid_limit() -> None:
    with pytest.raises(ValueError):
        split_message("abc", 1)


# --- rendu ------------------------------------------------------------------------------


def test_escape_markdown_v2_all_specials() -> None:
    specials = "_*[]()~`>#+-=|{}.!"
    escaped = escape_markdown_v2(specials)
    assert escaped == "".join("\\" + c for c in specials)
    assert escape_markdown_v2("a\\b") == "a\\\\b"


def test_timestamps() -> None:
    assert timestamp_url("ID", 754) == "https://youtu.be/ID?t=754"
    assert timestamp_url("ID", -3) == "https://youtu.be/ID?t=0"
    assert format_timestamp(754) == "12:34"
    assert format_timestamp(3725) == "1:02:05"


def test_render_markdown_v2_is_escaped_and_has_clickable_timestamps() -> None:
    md = render_markdown_v2(SUMMARY, VIDEO, "Veille")
    assert "[12:34](https://youtu.be/abc_DEF-123?t=754)" in md
    assert "*Titre \\*gras\\* \\[crochets\\] v1\\.2\\!*" in md
    # Hors URL de liens, aucun caractère réservé n'est laissé non échappé.
    without_links = re.sub(r"\]\([^)]*\)", "]", md)
    without_links = without_links.replace("[", "").replace("]", "")
    # Retire les délimiteurs d'entités voulus : ligne en italique _..._
    without_links = re.sub(r"^_(.*)_$", r"\1", without_links, flags=re.MULTILINE)
    assert not UNESCAPED.search(without_links)


def test_render_plain_has_no_markdown_escapes() -> None:
    text = render_plain(SUMMARY, VIDEO, "Veille")
    assert "\\" not in text
    assert "https://youtu.be/abc_DEF-123?t=3725" in text
    assert "TL;DR : Phrase un. Phrase deux." in text
    assert "Lecture : 2 min" in text


def test_reading_time() -> None:
    assert reading_time_minutes("") == 1
    assert reading_time_minutes("mot " * 401) == 3
