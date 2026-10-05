"""Assemblage du livre en Markdown, conversion EPUB/PDF via pandoc, archivage
NotebookLM optionnel.

Le format retenu :

- `Livres/<slug-chaine>/livre.md` : le livre complet (frontmatter + intro +
  chapitres + conclusion + glossaire).
- `Livres/<slug-chaine>/chapitres/<NN>-<slug>.md` : un fichier par chapitre.
- `Livres/<slug-chaine>/_index.md` : page d'index avec bloc Dataview.
- `Livres/<slug-chaine>/livre.epub` et `livre.pdf` (produits par pandoc).

Pandoc est appelé via subprocess ; l'appelant en test unitaire remplace
`pandoc_run` par un mock. En prod on installe pandoc + DejaVu Sans dans le LXC
(install-lxc.sh, ajout Lot 7).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guetteur.summarize.book import BookPlan

log = logging.getLogger(__name__)


PandocRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_pandoc_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class BookOutput:
    root: Path
    livre_md: Path
    index_md: Path
    chapter_files: tuple[Path, ...]
    epub: Path | None = None
    pdf: Path | None = None


# --- rendu du livre complet ---------------------------------------------------


def _slug_channel(channel_name: str) -> str:
    """Slug simple pour le dossier `Livres/<slug>` — pas d'accents, minuscules."""
    from guetteur.export.obsidian import slugify_title

    return slugify_title(channel_name or "chaine")


def _yaml_scalar(value: Any) -> str:
    s = str(value)
    if any(c in s for c in ':#-\n[]{}"\''):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _frontmatter(
    channel_name: str,
    channel_url: str,
    n_videos: int,
    period: str,
    generated_at: str,
    book_title: str,
) -> str:
    """Frontmatter YAML du livre — repris identique dans livre.md et _index.md."""
    lines = [
        "---",
        "guetteur_livre: true",
        f"titre: {_yaml_scalar(book_title)}",
        f"chaine: {_yaml_scalar(channel_name)}",
        f"chaine_url: {_yaml_scalar(channel_url)}",
        f"nb_videos: {n_videos}",
        f"periode: {_yaml_scalar(period)}",
        f"genere_le: {_yaml_scalar(generated_at)}",
        "---",
    ]
    return "\n".join(lines)


def render_book_markdown(
    plan: BookPlan,
    chapters: dict[str, str],
    videos_meta: dict[str, tuple[str, str, str | None]],
    channel_name: str,
    channel_url: str,
    glossary: dict[str, str] | None = None,
) -> str:
    """`chapters` = {titre_de_chapitre: markdown_du_chapitre}. `videos_meta` =
    {video_id: (titre, url, published_iso)}. Renvoie le livre complet."""
    n_videos = sum(len(ch.video_ids) for ch in plan.chapitres)
    dates: list[str] = []
    for ch in plan.chapitres:
        for v in ch.video_ids:
            meta = videos_meta.get(v)
            if meta and meta[2]:
                dates.append(meta[2])
    period = ""
    if dates:
        period = f"{min(dates)[:10]} → {max(dates)[:10]}"
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    parts = [
        _frontmatter(
            channel_name=channel_name,
            channel_url=channel_url,
            n_videos=n_videos,
            period=period,
            generated_at=generated_at,
            book_title=plan.titre,
        ),
        "",
        f"# {plan.titre}",
        "",
        f"*Compilation de la chaîne [{channel_name}]({channel_url}) — {n_videos} vidéos.*",
        "",
        "## Introduction",
        "",
        plan.introduction or "*Introduction manquante.*",
        "",
    ]
    for ch in plan.chapitres:
        content = chapters.get(ch.titre) or ""
        parts.append(content.strip() or f"# {ch.titre}\n\n*Chapitre vide.*\n")
        parts.append("")
    if plan.conclusion:
        parts.append("# Conclusion")
        parts.append("")
        parts.append(plan.conclusion)
        parts.append("")
    if glossary:
        parts.append("# Glossaire")
        parts.append("")
        for term, definition in sorted(glossary.items()):
            parts.append(f"- **{term}** — {definition}")
        parts.append("")
    return "\n".join(parts)


def render_index(plan: BookPlan, channel_name: str) -> str:
    """Page `_index.md` avec un bloc Dataview qui recense les chapitres."""
    lines = [
        f"# {plan.titre}",
        "",
        f"Compilation de la chaîne **{channel_name}** — {len(plan.chapitres)} chapitres.",
        "",
        "## Chapitres",
        "",
    ]
    for i, ch in enumerate(plan.chapitres, 1):
        slug = ch.slug
        lines.append(f"{i:>2}. [[chapitres/{i:02d}-{slug}|{ch.titre}]]")
    lines.append("")
    lines.append("```dataview")
    lines.append("table titre, chaine, nb_videos, genere_le")
    lines.append('FROM "Livres"')
    lines.append("WHERE guetteur_livre")
    lines.append("```")
    return "\n".join(lines)


# --- écriture disque + assemblage --------------------------------------------


def write_book(
    plan: BookPlan,
    chapters: dict[str, str],
    videos_meta: dict[str, tuple[str, str, str | None]],
    channel_name: str,
    channel_url: str,
    vault_livres_dir: Path,
    glossary: dict[str, str] | None = None,
) -> BookOutput:
    """Écrit `Livres/<slug>/livre.md`, `chapitres/*.md` et `_index.md` sous
    `vault_livres_dir`, puis renvoie les chemins. Ne lance pas pandoc — c'est le
    rôle de `run_pandoc`."""
    slug = _slug_channel(channel_name)
    root = vault_livres_dir / slug
    chap_dir = root / "chapitres"
    chap_dir.mkdir(parents=True, exist_ok=True)
    book_md = render_book_markdown(
        plan, chapters, videos_meta, channel_name, channel_url, glossary
    )
    livre_path = root / "livre.md"
    livre_path.write_text(book_md, encoding="utf-8")
    index_path = root / "_index.md"
    index_path.write_text(render_index(plan, channel_name), encoding="utf-8")
    chapter_paths: list[Path] = []
    for i, ch in enumerate(plan.chapitres, 1):
        cpath = chap_dir / f"{i:02d}-{ch.slug}.md"
        cpath.write_text((chapters.get(ch.titre) or "").strip() + "\n", encoding="utf-8")
        chapter_paths.append(cpath)
    return BookOutput(
        root=root,
        livre_md=livre_path,
        index_md=index_path,
        chapter_files=tuple(chapter_paths),
    )


# --- pandoc EPUB + PDF -------------------------------------------------------


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


def run_pandoc(
    book_md: Path,
    out_epub: Path,
    title: str,
    author: str = "GUETTEUR",
    pandoc: PandocRunner | None = None,
) -> Path | None:
    """Convertit `book_md` en EPUB. Retourne le chemin produit ou `None` si
    pandoc échoue. Le PDF est désormais généré via `run_weasyprint` (plus de
    LaTeX)."""
    runner = pandoc or _default_pandoc_run
    cmd = [
        "pandoc",
        str(book_md),
        "--from=markdown+yaml_metadata_block",
        "--toc",
        "--toc-depth=2",
        "--metadata",
        f"title={title}",
        "--metadata",
        f"author={author}",
        "-o",
        str(out_epub),
    ]
    try:
        res = runner(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("livre.pandoc_epub_error", extra={"error": str(exc)})
        return None
    if res.returncode == 0 and out_epub.exists():
        return out_epub
    log.warning(
        "livre.pandoc_epub_failed",
        extra={"returncode": res.returncode, "stderr": (res.stderr or "")[:300]},
    )
    return None


def run_weasyprint(
    book_md: Path,
    out_pdf: Path,
    title: str,
    pandoc: PandocRunner | None = None,
) -> Path | None:
    """Convertit `book_md` en PDF via pandoc→HTML puis weasyprint→PDF. Renvoie
    `None` si weasyprint n'est pas installé (extra `[pdf]` optionnel) OU si une
    étape échoue. Ne lève jamais — l'EPUB reste le format principal."""
    try:
        from weasyprint import HTML, default_url_fetcher
    except ImportError:
        log.info("livre.weasyprint_not_installed")
        return None

    # Garde SSRF / LFI : la source HTML est générée depuis le Markdown d'un
    # chapitre rédigé par Claude, qui s'appuie sur des métadonnées YouTube
    # (titres de vidéos) non filtrables. Un `<img src="file:///etc/passwd">` ou
    # `<img src="http://169.254.169.254/…">` glissé dans la sortie modèle
    # ferait lire le fichier / l'IP par weasyprint lors du rendu. On autorise
    # seulement les URIs `data:` (images embarquées légitimes).
    def _safe_fetcher(url: str) -> dict[str, Any]:
        if url.startswith("data:"):
            return dict(default_url_fetcher(url))
        log.warning("livre.weasyprint_url_blocked", extra={"url": url[:120]})
        return {"string": b"", "mime_type": "text/plain"}
    runner = pandoc or _default_pandoc_run
    html_tmp = out_pdf.with_suffix(".html")
    cmd = [
        "pandoc",
        str(book_md),
        "--from=markdown+yaml_metadata_block",
        "--toc",
        "--toc-depth=2",
        "--standalone",
        "--metadata",
        f"title={title}",
        "-o",
        str(html_tmp),
    ]
    try:
        res = runner(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("livre.weasyprint_html_error", extra={"error": str(exc)})
        return None
    if res.returncode != 0 or not html_tmp.exists():
        log.warning(
            "livre.weasyprint_html_failed",
            extra={"returncode": res.returncode, "stderr": (res.stderr or "")[:300]},
        )
        return None
    try:
        HTML(filename=str(html_tmp), url_fetcher=_safe_fetcher).write_pdf(str(out_pdf))
    except Exception as exc:
        log.warning("livre.weasyprint_pdf_failed", extra={"error": str(exc)})
        return None
    finally:
        html_tmp.unlink(missing_ok=True)
    return out_pdf if out_pdf.exists() else None


# --- glossaire heuristique ---------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9_-]{2,}")


def build_glossary(chapters: dict[str, str], min_occurrences: int = 3) -> dict[str, str]:
    """Extrait des termes récurrents (Cap majuscule ou acronymes ≥ 3 lettres),
    présents dans au moins `min_occurrences` chapitres. Définition = phrase où
    le terme apparaît pour la première fois. Suffisant pour ébaucher un
    glossaire ; l'utilisateur peut l'éditer à la main dans `livre.md`."""
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for content in chapters.values():
        seen_in_this_chapter: set[str] = set()
        for tok in _TOKEN_RE.findall(content):
            if tok in seen_in_this_chapter:
                continue
            seen_in_this_chapter.add(tok)
            counts[tok] = counts.get(tok, 0) + 1
            if tok not in first_seen:
                # Isole la phrase qui contient la première occurrence.
                sentence = _sentence_containing(content, tok)
                first_seen[tok] = sentence
    return {
        term: first_seen[term]
        for term in sorted(counts)
        if counts[term] >= min_occurrences and term in first_seen
    }


def _sentence_containing(text: str, term: str) -> str:
    # Une phrase = jusqu'au prochain « . » « ! » « ? » ou fin de ligne.
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if term in sentence:
            return sentence.strip()
    return ""


__all__ = [
    "BookOutput",
    "PandocRunner",
    "build_glossary",
    "pandoc_available",
    "render_book_markdown",
    "render_index",
    "run_pandoc",
    "run_weasyprint",
    "write_book",
]
