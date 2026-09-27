"""Rendu d'un résumé : MarkdownV2 échappé (Telegram) et texte brut (WhatsApp)."""

from __future__ import annotations

import math
import re

from guetteur.models import Summary, Video

# https://core.telegram.org/bots/api#markdownv2-style
_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_MDV2_URL_SPECIAL = re.compile(r"([)\\])")

WORDS_PER_MINUTE = 200


def escape_markdown_v2(text: str) -> str:
    return _MDV2_SPECIAL.sub(r"\\\1", text)


def escape_markdown_v2_url(url: str) -> str:
    return _MDV2_URL_SPECIAL.sub(r"\\\1", url)


def timestamp_url(video_id: str, seconds: int) -> str:
    return f"https://youtu.be/{video_id}?t={max(0, int(seconds))}"


def format_timestamp(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def reading_time_minutes(*texts: str) -> int:
    words = sum(len(t.split()) for t in texts)
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def render_markdown_v2(summary: Summary, video: Video, label: str = "") -> str:
    e = escape_markdown_v2
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [f"*{e(summary.title)}*"]
    if header_meta:
        lines.append(f"_{e(header_meta)}_")
    lines.append(f"[{e('▶️ Voir la vidéo')}]({escape_markdown_v2_url(video.url)})")
    lines += ["", f"*TL;DR* — {e(summary.tldr)}", "", "*Points clés*"]
    for kp in summary.key_points:
        link = escape_markdown_v2_url(timestamp_url(video.video_id, kp.seconds))
        lines.append(f"• [{e(format_timestamp(kp.seconds))}]({link}) {e(kp.text)}")
    lines += [
        "",
        f"*Pourquoi ça compte* — {e(summary.why_it_matters)}",
        "",
        e(f"⏱ Lecture : {summary.reading_time_minutes} min"),
    ]
    return "\n".join(lines)


def render_plain(summary: Summary, video: Video, label: str = "") -> str:
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [summary.title]
    if header_meta:
        lines.append(header_meta)
    lines += [video.url, "", f"TL;DR : {summary.tldr}", "", "Points clés :"]
    for kp in summary.key_points:
        lines.append(f"• {format_timestamp(kp.seconds)} — {kp.text}")
        lines.append(f"  {timestamp_url(video.video_id, kp.seconds)}")
    lines += [
        "",
        f"Pourquoi ça compte : {summary.why_it_matters}",
        "",
        f"⏱ Lecture : {summary.reading_time_minutes} min",
    ]
    return "\n".join(lines)


def render_markdown(summary: Summary, video: Video, label: str = "") -> str:
    """Résumé en Markdown brut (non échappé), utilisé pour l'archivage NotebookLM."""
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [f"# {summary.title}"]
    if header_meta:
        lines.append(f"_{header_meta}_")
    lines += ["", f"**TL;DR** — {summary.tldr}", "", "## Points clés", ""]
    for kp in summary.key_points:
        stamp = format_timestamp(kp.seconds)
        link = timestamp_url(video.video_id, kp.seconds)
        lines.append(f"- [{stamp}]({link}) — {kp.text}")
    lines += [
        "",
        f"**Pourquoi ça compte** — {summary.why_it_matters}",
        "",
        f"_⏱ Lecture : {summary.reading_time_minutes} min_",
    ]
    return "\n".join(lines)


def render_no_transcript(video: Video, markdown_v2: bool) -> str:
    text = f"⚠️ Pas de transcription disponible pour « {video.title} » — vidéo abandonnée."
    if markdown_v2:
        return f"{escape_markdown_v2(text)}\n{escape_markdown_v2(video.url)}"
    return f"{text}\n{video.url}"
