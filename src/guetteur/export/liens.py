"""Export Obsidian d'un item LIEN (Lot 8).

Note Markdown dans `Veille/Inbox/` avec un frontmatter YAML court. Le chemin
est `Veille/Inbox/<kind>-<slug>.md`. Le commit git est différé puis déclenché
par `exporter.commit_now` (si git_sync est actif).

Le module expose une fonction unique `write_link_note(exporter, item, summary)`
qui s'appuie sur l'instance `ObsidianExporter` existante — on ne crée pas une
seconde classe pour un cas aussi simple.
"""

from __future__ import annotations

import logging
from pathlib import Path

from guetteur.export.obsidian import ObsidianExporter, _atomic_write, slugify_title
from guetteur.items import LinkItem
from guetteur.models import Summary

log = logging.getLogger(__name__)

_KIND_TAG = {
    "tweet": "x",
    "article": "web",
    "github": "github",
    "youtube_oneshot": "youtube",
}


def write_link_note(
    exporter: ObsidianExporter,
    item: LinkItem,
    summary: Summary,
    *,
    theme: str = "",
) -> Path:
    """Écrit la note dans Inbox et déclenche un commit git si git_sync=True."""
    inbox = exporter.inbox_path
    exporter.ensure_vault_layout()
    slug = slugify_title(f"{item.kind}-{summary.title or item.title or item.item_id}")
    path = inbox / f"{slug}.md"
    frontmatter = _frontmatter(item, summary, theme=theme)
    body = _body(item, summary)
    _atomic_write(path, f"{frontmatter}\n\n{body}\n")
    log.info("obsidian.link_note", extra={"path": str(path), "kind": item.kind})
    exporter.commit_now(f"GUETTEUR LIEN : {(summary.title or item.url)[:80]}")
    return path


def _frontmatter(item: LinkItem, summary: Summary, *, theme: str) -> str:
    tag = _KIND_TAG.get(item.kind, "web")
    lines = [
        "---",
        f"kind: {item.kind}",
        f"url: {_yaml_str(item.url)}",
        f"auteur: {_yaml_str(item.author)}",
        f"source: {item.source}",
    ]
    if item.published_at:
        lines.append(f"date: {item.published_at.date().isoformat()}")
    lines.append(f"titre: {_yaml_str(summary.title or item.title)}")
    if theme:
        lines.append(f"theme: {_yaml_str(theme)}")
    lines.append(f"tags: [{tag}]")
    lines.append("guetteur: true")
    lines.append("---")
    return "\n".join(lines)


def _body(item: LinkItem, summary: Summary) -> str:
    kind_label = {
        "tweet": "Tweet",
        "article": "Article",
        "github": "Dépôt GitHub",
        "youtube_oneshot": "Vidéo YouTube",
    }.get(item.kind, "Lien")
    lines = [
        f"# {summary.title or item.title or kind_label}",
        "",
        f"[{item.url}]({item.url})",
        "",
        f"**{kind_label}**",
        "",
        summary.tldr,
        "",
        "## Points clés",
    ]
    lines += [f"- {k.text}" for k in summary.key_points]
    if summary.actions:
        lines += ["", "## Actions", *[f"- {a}" for a in summary.actions]]
    if summary.why_it_matters:
        lines += ["", "## Pourquoi c'est intéressant", summary.why_it_matters]
    return "\n".join(lines)


def _yaml_str(value: str) -> str:
    if value is None:
        return '""'
    text = str(value)
    if any(c in text for c in ":#\n\"'") or text.strip() != text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
