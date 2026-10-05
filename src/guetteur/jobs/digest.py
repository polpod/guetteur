"""Digest hebdomadaire (Lot 8 §4).

Regroupe les items LIEN envoyés et les vidéos « gardées » (note Obsidian hors
Inbox) sur la fenêtre `--since` (défaut 7 jours). Rendu :

- Message Telegram court : par thème, une ligne par entrée, puis 3 idées projet
  les plus fortes.
- Note Obsidian `Veille/Digests/<AAAA-WW>.md` avec le même contenu en
  Markdown.

Pas d'appel Claude : on utilise les données déjà en base (thèmes et scores
d'applicabilité).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from guetteur.export.obsidian import ObsidianExporter, _atomic_write
from guetteur.items import LinkItem
from guetteur.notify.base import Message, Notifier
from guetteur.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DigestEntry:
    kind: str  # "lien" | "video"
    theme: str  # "" = sans thème
    title: str
    url: str


@dataclass(frozen=True)
class ProjectIdea:
    project_slug: str
    score: int
    video_title: str
    idea: str


@dataclass(frozen=True)
class Digest:
    since: datetime
    until: datetime
    entries: list[DigestEntry]
    top_ideas: list[ProjectIdea]

    @property
    def iso_week(self) -> str:
        year, week, _ = self.until.isocalendar()
        return f"{year:04d}-W{week:02d}"


def build_digest(store: Store, since: datetime, until: datetime | None = None) -> Digest:
    """Construit un digest à partir du store. `until` défaut = utcnow()."""
    until = until or datetime.now(UTC)
    entries: list[DigestEntry] = []

    for item in store.items_sent_since(since):
        entries.append(
            DigestEntry(
                kind="lien",
                theme=item.theme,
                title=_short_title_item(item),
                url=item.url,
            )
        )

    for video in store.videos_kept_since(since):
        theme = ""
        note = store.obsidian_note(video.video_id)
        if note is not None:
            theme = note[2] or ""
        entries.append(
            DigestEntry(
                kind="video",
                theme=theme,
                title=video.title,
                url=video.url,
            )
        )

    top_ideas = _top_project_ideas(store, since)
    return Digest(since=since, until=until, entries=entries, top_ideas=top_ideas)


def _short_title_item(item: LinkItem) -> str:
    if item.title:
        return item.title
    if item.summary:
        import json

        try:
            data = json.loads(item.summary)
            t = str(data.get("title", "")).strip()
            if t:
                return t
        except (ValueError, json.JSONDecodeError):
            pass
    return item.url


def _top_project_ideas(store: Store, since: datetime) -> list[ProjectIdea]:
    """3 meilleures idées projet (par score, puis par titre vidéo) sur la fenêtre."""
    # Les vidéos « gardées » ont des entrées dans `applicability`. On lit celles-ci
    # pour les videos sent depuis `since`.
    results: list[ProjectIdea] = []
    seen: set[tuple[str, str]] = set()
    for video in store.videos_kept_since(since):
        for slug, score, idea, *_rest in store.applicability_for(video.video_id):
            key = (video.video_id, slug)
            if key in seen or not idea:
                continue
            seen.add(key)
            results.append(
                ProjectIdea(project_slug=slug, score=score, video_title=video.title, idea=idea)
            )
    results.sort(key=lambda x: (-x.score, x.video_title))
    return results[:3]


def render_digest_markdown(digest: Digest) -> str:
    """Markdown pour la note Obsidian Veille/Digests/<AAAA-WW>.md."""
    lines = [
        f"# Digest {digest.iso_week}",
        "",
        f"Du {digest.since.date().isoformat()} au {digest.until.date().isoformat()}.",
        "",
    ]
    if not digest.entries:
        lines += ["_Rien gardé cette semaine._"]
    else:
        grouped: dict[str, list[DigestEntry]] = defaultdict(list)
        for e in digest.entries:
            grouped[e.theme or "Sans thème"].append(e)
        for theme in sorted(grouped):
            lines.append(f"## {theme}")
            for e in grouped[theme]:
                tag = "📎" if e.kind == "lien" else "🎞️"
                lines.append(f"- {tag} [{e.title}]({e.url})")
            lines.append("")
    if digest.top_ideas:
        lines.append("## Idées projet retenues")
        for idea in digest.top_ideas:
            lines.append(
                f"- **{idea.project_slug.upper()}** ({idea.score}) — {idea.idea} "
                f"_({idea.video_title})_"
            )
    return "\n".join(lines).rstrip() + "\n"


def render_digest_telegram(digest: Digest) -> Message:
    """Message Telegram court — markdown_v2 pour les liens cliquables."""
    from guetteur.summarize.format import escape_md_v2

    header = f"*Digest {escape_md_v2(digest.iso_week)}*"
    md_lines = [header, ""]
    plain_lines = [f"Digest {digest.iso_week}", ""]
    if not digest.entries:
        md_lines.append(escape_md_v2("Rien gardé cette semaine."))
        plain_lines.append("Rien gardé cette semaine.")
    else:
        grouped: dict[str, list[DigestEntry]] = defaultdict(list)
        for e in digest.entries:
            grouped[e.theme or "Sans thème"].append(e)
        for theme in sorted(grouped):
            md_lines.append(f"*{escape_md_v2(theme)}*")
            plain_lines.append(theme)
            for e in grouped[theme]:
                tag = "📎" if e.kind == "lien" else "🎞️"
                md_lines.append(
                    f"{tag} [{escape_md_v2(e.title)}]({escape_md_v2(e.url)})"
                )
                plain_lines.append(f"{tag} {e.title} — {e.url}")
            md_lines.append("")
            plain_lines.append("")
    if digest.top_ideas:
        md_lines.append(f"*{escape_md_v2('Idées projet')}*")
        plain_lines.append("Idées projet")
        for idea in digest.top_ideas:
            md_lines.append(
                f"• *{escape_md_v2(idea.project_slug.upper())}* "
                f"\\({idea.score}\\) {escape_md_v2(idea.idea)}"
            )
            plain_lines.append(
                f"• {idea.project_slug.upper()} ({idea.score}) {idea.idea}"
            )
    md = "\n".join(md_lines).rstrip()
    plain = "\n".join(plain_lines).rstrip()
    return Message(markdown_v2=md, plain=plain, short=f"Digest {digest.iso_week}")


def write_digest_note(exporter: ObsidianExporter, digest: Digest) -> Path:
    """Écrit la note Obsidian Veille/Digests/<AAAA-WW>.md et commite."""
    exporter.ensure_vault_layout()
    digests_dir = exporter.veille_path / "Digests"
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{digest.iso_week}.md"
    _atomic_write(path, render_digest_markdown(digest))
    exporter.commit_now(f"GUETTEUR DIGEST : {digest.iso_week}")
    return path


def send_digest(
    digest: Digest,
    telegram: Notifier,
    exporter: ObsidianExporter | None = None,
) -> Path | None:
    """Envoie le digest sur Telegram et écrit la note Obsidian si configuré."""
    telegram.send(render_digest_telegram(digest))
    if exporter is not None:
        return write_digest_note(exporter, digest)
    return None


def parse_since(spec: str, now: datetime | None = None) -> datetime:
    """Parse `--since` : « 7d », « 24h », « 2w », ou ISO date (2026-10-01)."""
    now = now or datetime.now(UTC)
    s = spec.strip().lower()
    if s.endswith("d") and s[:-1].isdigit():
        return now - timedelta(days=int(s[:-1]))
    if s.endswith("h") and s[:-1].isdigit():
        return now - timedelta(hours=int(s[:-1]))
    if s.endswith("w") and s[:-1].isdigit():
        return now - timedelta(weeks=int(s[:-1]))
    try:
        parsed = datetime.fromisoformat(spec)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError as exc:
        raise ValueError(
            f"--since invalide : {spec!r} (attendu : 7d, 24h, 2w ou ISO date)"
        ) from exc
