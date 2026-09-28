"""Export d'une note Markdown par vidéo dans un vault Obsidian.

- Structure fixe : `Veille/Inbox/` (nouvelles vidéos), `Veille/<thème>/` (gardées),
  `Veille/_ecartes/` (écartées), `Veille/_taxonomie.md`, `Veille/_index.md`, plus les
  fiches projet `Projets/<slug>.md` et leur bloc-note d'idées `Projets/<slug>/IDEES.md`.
- Frontmatter YAML riche (voir `build_frontmatter`) avec `guetteur: true` — GUETTEUR
  ne touche JAMAIS à un fichier qui n'est pas marqué ainsi.
- Écriture atomique (tempfile + rename), idempotence par `video_id` via un index
  `Veille/.guetteur-index.json` qui retrouve la note même si elle a été déplacée
  d'Inbox vers un thème.
- git_sync : commit local + pull --rebase + push, avec verrou et fallback local si
  le remote est injoignable.

La sécurité est côté configuration (voir `_parse_obsidian`) : le chemin du vault
est refusé s'il est inclus dans `/opt/guetteur/data/nlm`, `/root` ou `/etc`."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guetteur.config import Config
from guetteur.models import DetailLevel, Summary, Video
from guetteur.store import Store
from guetteur.summarize.format import format_timestamp, timestamp_url

log = logging.getLogger(__name__)

INDEX_FILE = ".guetteur-index.json"
GUETTEUR_MARKER = "guetteur: true"
DEFAULT_STATUS = "inbox"
STATUSES = ("inbox", "garde", "ecarte")
ECARTES_DIR = "_ecartes"


# --- exceptions ------------------------------------------------------------------------


class ObsidianExportError(RuntimeError):
    """Erreur d'écriture, de git ou de validation lors de l'export."""


# --- helpers texte ---------------------------------------------------------------------


def slugify_title(text: str, max_len: int = 60) -> str:
    """Slug conservateur pour un nom de fichier Obsidian : ascii, minuscules,
    `-` comme séparateur. Ne coupe jamais un mot au milieu si évitable."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^\w\s-]", "", normalized).strip().lower()
    normalized = re.sub(r"[-\s]+", "-", normalized) or "sans-titre"
    if len(normalized) <= max_len:
        return normalized
    cut = normalized.rfind("-", 0, max_len)
    return normalized[: cut if cut > max_len // 2 else max_len]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- Taxonomy --------------------------------------------------------------------------


@dataclass(frozen=True)
class Taxonomy:
    """Thèmes autorisés avec leur liste de tags. Lue depuis `_taxonomie.md`.

    Format attendu (une section `## <thème>` par thème, tags en puces en dessous) :

        ## LLM
        - claude
        - anthropic
        - context

        ## Outils
        - uv
        - ruff
    """

    themes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def has_theme(self, theme: str) -> bool:
        return theme in self.themes

    def allowed_tags(self, theme: str) -> set[str]:
        return set(self.themes.get(theme, ()))

    def all_tags(self) -> set[str]:
        return {t for tags in self.themes.values() for t in tags}

    def split_tags(self, theme: str, tags: list[str]) -> tuple[list[str], list[str]]:
        """Sépare la liste `tags` en (dans la taxonomie du thème, hors taxonomie)."""
        allowed = self.allowed_tags(theme)
        kept: list[str] = []
        proposed: list[str] = []
        for t in tags:
            t_norm = t.strip().lower()
            if not t_norm:
                continue
            (kept if t_norm in allowed else proposed).append(t_norm)
        return kept, proposed


_SECTION_HEADER = re.compile(r"^##\s+(.+?)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.+?)\s*$")


def load_taxonomy(path: Path) -> Taxonomy:
    """Lit `_taxonomie.md`. Fichier absent → taxonomie vide (tag hors taxonomie = tout)."""
    if not path.exists():
        return Taxonomy(themes={})
    themes: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        header = _SECTION_HEADER.match(line)
        if header:
            current = header.group(1).strip()
            themes.setdefault(current, [])
            continue
        bullet = _BULLET.match(line)
        if bullet and current is not None:
            themes[current].append(bullet.group(1).strip().lower())
    return Taxonomy(themes={k: tuple(v) for k, v in themes.items()})


# --- fiches projet ---------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectSheet:
    slug: str
    nom: str
    statut: str = ""
    stack: tuple[str, ...] = ()
    objectifs: tuple[str, ...] = ()
    recherche: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    # Finitions Lot 6 §3 : le dépôt du projet (chemin local ou URL git) et les modules
    # clés au format « chemin/vers/fichier.py : rôle » ; injectés dans le prompt
    # d'applicabilité pour que Claude cite ces chemins réels dans le méga-prompt.
    depot: str = ""
    modules_cles: tuple[str, ...] = ()
    body_extra: str = ""  # tout ce qui suit le frontmatter, pour contexte Claude

    def as_context(self) -> str:
        """Rendu compact pour la passe applicabilité, encadré dans des balises."""
        parts = [f"# {self.nom}", f"slug: {self.slug}", f"statut: {self.statut}"]
        if self.stack:
            parts.append("Stack : " + ", ".join(self.stack))
        if self.depot:
            parts.append(f"Dépôt : {self.depot}")
        if self.objectifs:
            parts.append("Objectifs :\n- " + "\n- ".join(self.objectifs))
        if self.recherche:
            parts.append("Sujets recherchés :\n- " + "\n- ".join(self.recherche))
        if self.exclusions:
            parts.append("Exclusions :\n- " + "\n- ".join(self.exclusions))
        if self.modules_cles:
            parts.append(
                "Modules clés (seuls chemins autorisés dans les méga-prompts) :\n- "
                + "\n- ".join(self.modules_cles)
            )
        if self.body_extra.strip():
            parts.append("Notes libres :\n" + self.body_extra.strip())
        return "\n".join(parts)


def parse_project_sheet(slug: str, text: str) -> ProjectSheet:
    """Parse une fiche projet Markdown avec frontmatter YAML minimal."""
    front, body = _extract_frontmatter(text)
    stack = _seq(front.get("stack"))
    return ProjectSheet(
        slug=slug,
        nom=str(front.get("nom", slug)),
        statut=str(front.get("statut", "")),
        stack=stack,
        objectifs=_seq(front.get("objectifs")),
        recherche=_seq(front.get("recherche")),
        exclusions=_seq(front.get("exclusions")),
        depot=str(front.get("depot", "")),
        modules_cles=_seq(front.get("modules_cles")),
        body_extra=body,
    )


def _seq(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parseur YAML minimal (clef: valeur, listes en puces). Suffisant pour les
    fiches projet ; on ne dépend pas de PyYAML pour rester léger."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    front_raw, body = match.group(1), match.group(2)
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in front_raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") or line.startswith("- "):
            item = line.lstrip("- ").strip()
            if current_key is not None:
                bucket = data.get(current_key)
                if isinstance(bucket, list):
                    bucket.append(item)
                else:
                    data[current_key] = [item]
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            current_key = key
            if not value:
                data[key] = []
            elif value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                data[key] = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
            else:
                data[key] = value.strip('"').strip("'")
    return data, body


# --- frontmatter d'une note --------------------------------------------------------------


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    s = str(value)
    if any(c in s for c in ":#-\n[]{}\"'"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def build_frontmatter(
    video: Video,
    detail: DetailLevel,
    theme: str,
    tags: list[str],
    tags_proposes: list[str],
    projets: list[str],
    status: str,
    archive_notebooklm: str | None = None,
) -> str:
    published = (video.published or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")
    lines = [
        "---",
        "guetteur: true",
        f"video_id: {_yaml_scalar(video.video_id)}",
        f"url: {_yaml_scalar(video.url)}",
        f"chaine: {_yaml_scalar(video.channel)}",
        f"date_publication: {published}",
        'duree: ""',  # non fourni par le modèle Video actuel, laissé vide
        f"niveau: {_yaml_scalar(detail)}",
        f"tags: {_yaml_list(tags)}",
        f"tags_proposes: {_yaml_list(tags_proposes)}",
        f"theme: {_yaml_scalar(theme)}",
        f"projets: {_yaml_list(projets)}",
        f"statut: {_yaml_scalar(status)}",
        f"archive_notebooklm: {_yaml_scalar(archive_notebooklm or '')}",
        "---",
    ]
    return "\n".join(lines)


# --- rendu Markdown ---------------------------------------------------------------------


def _render_body_markdown(
    video: Video, summary: Summary, qa_items: list[tuple[int, str, str, str]]
) -> str:
    """Corps de la note. Le résumé détaillé est privilégié ; le mode standard est utilisé
    en fallback. Sections avec timestamps cliquables, puis « ## Questions »."""
    lines = [f"# {summary.title}", "", f"[Voir la vidéo]({video.url})", ""]
    if summary.detail == "detaille" and summary.sections:
        lines.append(f"**TL;DR** — {summary.tldr}")
        lines.append("")
        for sec in summary.sections:
            stamp = format_timestamp(sec.seconds)
            link = timestamp_url(video.video_id, sec.seconds)
            lines += [f"## {sec.title} — [{stamp}]({link})", ""]
            for bullet in sec.bullets:
                lines.append(f"- {bullet}")
            lines.append("")
        if summary.citations:
            lines += ["## Citations", ""]
            for cit in summary.citations:
                stamp = format_timestamp(cit.seconds)
                link = timestamp_url(video.video_id, cit.seconds)
                lines.append(f"- [{stamp}]({link}) — « {cit.text} »")
            lines.append("")
        if summary.actions:
            lines += ["## Actions", ""]
            for action in summary.actions:
                lines.append(f"- {action}")
            lines.append("")
        if summary.reserves:
            lines += ["## Réserves", ""]
            for reserve in summary.reserves:
                lines.append(f"- {reserve}")
            lines.append("")
    else:
        lines.append(f"**TL;DR** — {summary.tldr}")
        lines += ["", "## Points clés", ""]
        for kp in summary.key_points:
            stamp = format_timestamp(kp.seconds)
            link = timestamp_url(video.video_id, kp.seconds)
            lines.append(f"- [{stamp}]({link}) — {kp.text}")
        if summary.why_it_matters:
            lines += ["", f"**Pourquoi ça compte** — {summary.why_it_matters}"]
        lines.append("")
    # Section Questions : append idempotent par id de qa.
    lines += ["## Questions", ""]
    if qa_items:
        for qa_id, question, answer, at in qa_items:
            lines.append(f"### Q{qa_id} — {question}")
            lines.append(f"_{at}_")
            lines.append("")
            lines.append(answer)
            lines.append("")
    else:
        lines.append(
            "_Aucune question pour l'instant. Répondez au message Telegram pour en poser._"
        )
        lines.append("")
    return "\n".join(lines)


# --- écriture atomique + index ----------------------------------------------------------


@dataclass(frozen=True)
class NoteWriteResult:
    path: Path
    created: bool
    moved_from: Path | None = None


def _atomic_write(path: Path, content: str) -> None:
    import contextlib

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".guetteur-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        tmp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _read_index(vault_veille: Path) -> dict[str, str]:
    idx = vault_veille / INDEX_FILE
    if not idx.exists():
        return {}
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (ValueError, OSError):
        pass
    return {}


def _write_index(vault_veille: Path, index: dict[str, str]) -> None:
    _atomic_write(vault_veille / INDEX_FILE, json.dumps(index, ensure_ascii=False, indent=2))


# --- git ---------------------------------------------------------------------------------


_git_lock = threading.Lock()


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _git_sync(vault: Path, commit_message: str, remote: str) -> str:
    """Commit local ; si remote configuré, pull --rebase puis push. Retourne un
    court statut lisible : « local », « push_ok », « push_failed »."""
    with _git_lock:
        # git init si le vault n'est pas encore un dépôt.
        if not (vault / ".git").exists():
            init = _git(vault, "init", "-q")
            if init.returncode != 0:
                raise ObsidianExportError(f"git init : {init.stderr.strip()}")
            _git(vault, "config", "user.email", "guetteur@local")
            _git(vault, "config", "user.name", "GUETTEUR")
            if remote:
                _git(vault, "remote", "add", "origin", remote)
        # Add + commit (allow-empty si rien à committer).
        _git(vault, "add", "-A")
        commit = _git(vault, "commit", "-m", commit_message)
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout + commit.stderr:
            raise ObsidianExportError(f"git commit : {commit.stderr.strip()}")
        if not remote:
            return "local"
        # Détecter si le remote a déjà une HEAD ; sinon skip le pull (remote bare vide).
        ls = _git(vault, "ls-remote", "--heads", "origin", timeout=15.0)
        remote_has_head = ls.returncode == 0 and ls.stdout.strip() != ""
        if remote_has_head:
            pull = _git(vault, "pull", "--rebase", "origin", "HEAD", timeout=60.0)
            if pull.returncode != 0:
                log.warning(
                    "obsidian.git_pull_failed",
                    extra={"stderr": pull.stderr.strip()[:200]},
                )
                return "push_failed"
        push = _git(vault, "push", "origin", "HEAD", timeout=60.0)
        if push.returncode != 0:
            log.warning(
                "obsidian.git_push_failed",
                extra={"stderr": push.stderr.strip()[:200]},
            )
            return "push_failed"
        return "push_ok"


# --- Exporter --------------------------------------------------------------------------


class ObsidianExporter:
    """Facade unique du Lot 6 : `export_note`, `move_to_theme`, `discard`, `applicability_line`.

    Le store lui permet de retrouver la note d'une vidéo (via `obsidian_note`) et de
    persister status/thème (via `upsert_obsidian_note`). Les appels sont thread-safe
    grâce au verrou git ; les I/O disque sont atomiques."""

    def __init__(self, config: Config, store: Store) -> None:
        self._config = config
        self._store = store
        self._veille: Path = config.obsidian.path / config.obsidian.veille_dir
        self._projets: Path = config.obsidian.path / config.obsidian.projets_dir
        self._inbox: Path = self._veille / "Inbox"
        # Finitions Lot 6 §1 : quand ce drapeau est actif, `_commit` ne pousse pas de
        # commit intermédiaire. `export_video()` groupe note + idées en un seul commit.
        self._defer_commit: bool = False

    # --- vault init -------------------------------------------------------------------

    def ensure_vault_layout(self) -> None:
        """Crée les dossiers et les fichiers modèles s'ils manquent. Jamais destructif :
        un fichier sans marqueur `guetteur: true` est laissé tel quel."""
        for d in (self._inbox, self._veille / ECARTES_DIR, self._projets):
            d.mkdir(parents=True, exist_ok=True)
        tax_path = self._veille / "_taxonomie.md"
        if not tax_path.exists():
            _atomic_write(tax_path, _default_taxonomy_template())
        index_path = self._veille / "_index.md"
        if not index_path.exists():
            _atomic_write(index_path, _default_index_template())
        for slug, sheet_text in _default_project_sheets().items():
            sheet_path = self._projets / f"{slug}.md"
            if not sheet_path.exists():
                _atomic_write(sheet_path, sheet_text)

    # --- commit groupé (finitions Lot 6 §1) ------------------------------------------

    @contextmanager
    def batch_commit(self, message: str) -> Iterator[None]:
        """Regroupe les écritures qui suivent en un seul commit git. Toutes les
        méthodes `export_note`, `move_to_*`, `append_idea` appelées dans ce bloc ne
        commiteront pas individuellement ; un unique `_commit(message)` est fait en
        sortie. Sûr en cas d'exception : le commit final est skip dans ce cas."""
        previous = self._defer_commit
        self._defer_commit = True
        raised = False
        try:
            yield
        except Exception:
            raised = True
            raise
        finally:
            self._defer_commit = previous
            if not previous and not raised:
                self._commit(message)

    def export_video(
        self,
        video: Video,
        summary: Summary,
        detail: DetailLevel,
        theme: str,
        tags: list[str],
        tags_proposes: list[str],
        pertinences: list[tuple[str, int, str, str, str, str, str]] | None = None,
        archive_notebooklm: str | None = None,
    ) -> NoteWriteResult:
        """Écrit la note ET tous les blocs d'idées score ≥ idea_threshold en un
        SEUL commit git avec un message qui liste les projets ajoutés.

        `pertinences` : liste (slug, score, idée, integration, effort, risques, prompt)
        déjà scorée et en base — `Pipeline.maybe_export` la construit depuis
        `applicability_for`. Aucun appel Claude effectué ici."""
        pertinences = pertinences or []
        actionable: list[str] = []
        threshold = self._config.applicability.idea_threshold
        previous_defer = self._defer_commit
        self._defer_commit = True
        try:
            result = self.export_note(
                video=video,
                summary=summary,
                detail=detail,
                theme=theme,
                tags=tags,
                tags_proposes=tags_proposes,
                projets_scores=[(slug, score, idea) for slug, score, idea, *_ in pertinences],
                archive_notebooklm=archive_notebooklm,
            )
            for slug, score, idea, integration, effort, _risks, prompt in pertinences:
                if score < threshold or not prompt:
                    continue
                if self.append_idea(video, slug, score, idea, integration, effort, prompt):
                    actionable.append(slug)
        finally:
            self._defer_commit = previous_defer
        suffix = f" (+ idées : {', '.join(actionable)})" if actionable else ""
        self._commit(f"GUETTEUR : {video.title[:80]}{suffix}")
        return result

    # --- projets ---------------------------------------------------------------------

    def load_project_sheets(self) -> list[ProjectSheet]:
        sheets: list[ProjectSheet] = []
        if not self._projets.exists():
            return sheets
        for md in sorted(self._projets.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            slug = md.stem.lower()
            sheets.append(parse_project_sheet(slug, text))
        return sheets

    # --- note principale --------------------------------------------------------------

    def export_note(
        self,
        video: Video,
        summary: Summary,
        detail: DetailLevel,
        theme: str,
        tags: list[str],
        tags_proposes: list[str],
        projets_scores: list[tuple[str, int, str]] | None = None,
        archive_notebooklm: str | None = None,
    ) -> NoteWriteResult:
        """Écrit la note dans son emplacement courant (Inbox si nouvelle). Idempotent."""
        self.ensure_vault_layout()
        existing = self._store.obsidian_note(video.video_id)
        status = existing[1] if existing else DEFAULT_STATUS
        theme_effective = existing[2] if (existing and existing[2]) else theme
        path = self._current_path(video, status, theme_effective, existing)
        projets = [slug for slug, score, _idea in (projets_scores or []) if score >= 1]
        frontmatter = build_frontmatter(
            video=video,
            detail=detail,
            theme=theme_effective,
            tags=tags,
            tags_proposes=tags_proposes,
            projets=projets,
            status=status,
            archive_notebooklm=archive_notebooklm,
        )
        qa_items = self._store.all_qa(video.video_id)
        body = _render_body_markdown(video, summary, qa_items)
        if projets_scores:
            body += "\n" + self._render_applicability_section(projets_scores) + "\n"
        content = f"{frontmatter}\n\n{body}\n"
        created = not path.exists()
        _atomic_write(path, content)
        self._store.upsert_obsidian_note(
            video.video_id, str(path), status=status, theme=theme_effective
        )
        # Index vault-relatif pour permettre les liens wiki d'IDEES.md.
        index = _read_index(self._veille)
        index[video.video_id] = str(path.relative_to(self._veille))
        _write_index(self._veille, index)
        self._commit(f"GUETTEUR : {video.title[:80]}")
        return NoteWriteResult(path=path, created=created)

    def _current_path(
        self,
        video: Video,
        status: str,
        theme: str,
        existing: tuple[str, str, str] | None,
    ) -> Path:
        """Chemin de la note : conserve l'existant si présent, sinon calcule à partir
        du status/thème. Un déplacement (Inbox → thème) passe par `move_to_theme`."""
        if existing:
            p = Path(existing[0])
            if p.exists():
                return p
        # Finitions Lot 6 §2 : le préfixe est la date de publication de la vidéo ou
        # la date de traitement (aujourd'hui), au choix de l'utilisateur.
        if self._config.obsidian.filename_date == "traitement" or video.published is None:
            date = datetime.now(UTC).strftime("%Y-%m-%d")
        else:
            date = video.published.astimezone(UTC).strftime("%Y-%m-%d")
        slug = slugify_title(video.title)
        filename = f"{date} - {slug}.md"
        if status == "garde" and theme:
            return self._veille / theme / filename
        if status == "ecarte":
            return self._veille / ECARTES_DIR / filename
        return self._inbox / filename

    # --- déplacements ----------------------------------------------------------------

    def set_theme(self, video_id: str, theme: str) -> Path | None:
        """Applique un thème sans changer le statut. Utile pour /theme."""
        existing = self._store.obsidian_note(video_id)
        if existing is None:
            return None
        self._store.upsert_obsidian_note(video_id, existing[0], status=existing[1], theme=theme)
        return Path(existing[0])

    def move_to_theme(self, video_id: str, theme: str) -> Path | None:
        """Déplace la note dans `Veille/<theme>/` et enregistre statut = « garde »."""
        return self._move(video_id, target_status="garde", theme=theme)

    def move_to_discarded(self, video_id: str) -> Path | None:
        return self._move(video_id, target_status="ecarte", theme="")

    def _move(self, video_id: str, target_status: str, theme: str) -> Path | None:
        existing = self._store.obsidian_note(video_id)
        if existing is None:
            return None
        source = Path(existing[0])
        if not source.exists():
            return None
        # Rewrite frontmatter statut/theme dans le fichier avant déplacement.
        text = source.read_text(encoding="utf-8")
        new_text = _update_frontmatter_status(text, target_status, theme)
        # Déplacement effectif.
        target_dir = self._veille / (theme or ECARTES_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        _atomic_write(target, new_text)
        if target != source:
            try:
                source.unlink()
            except OSError as exc:
                raise ObsidianExportError(f"unlink {source} : {exc}") from exc
        self._store.upsert_obsidian_note(video_id, str(target), status=target_status, theme=theme)
        index = _read_index(self._veille)
        index[video_id] = str(target.relative_to(self._veille))
        _write_index(self._veille, index)
        self._commit(f"GUETTEUR : {target_status} — {source.stem}")
        return target

    # --- applicabilité ---------------------------------------------------------------

    def _render_applicability_section(self, scores: list[tuple[str, int, str]]) -> str:
        lines = ["## Applicabilité", ""]
        for slug, score, idea in scores:
            if score >= 1:
                marker = "★" * score
                lines.append(f"- **{slug}** ({marker}, {score}/3) — {idea}")
        return "\n".join(lines)

    def append_idea(
        self,
        video: Video,
        project_slug: str,
        score: int,
        idea: str,
        integration: str,
        effort: str,
        prompt: str,
    ) -> Path | None:
        """Append idempotent d'un bloc daté dans `Projets/<slug>/IDEES.md`. Retourne
        le chemin si écriture, None si déjà écrit."""
        if not self._store.mark_idea_written(video.video_id, project_slug):
            return None
        proj_dir = self._projets / project_slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        ideas_path = proj_dir / "IDEES.md"
        # Lien wiki vers la note (via l'index).
        note_link = self._wiki_link_for(video.video_id) or video.title
        block = _render_idea_block(
            date_iso=_now_iso(),
            note_link=note_link,
            score=score,
            idea=idea,
            integration=integration,
            effort=effort,
            prompt=prompt,
        )
        existing = ideas_path.read_text(encoding="utf-8") if ideas_path.exists() else ""
        if not existing.startswith("# Idées"):
            existing = "# Idées\n\n" + existing
        _atomic_write(ideas_path, existing + block + "\n")
        self._commit(f"GUETTEUR : idée pour {project_slug} — {video.title[:60]}")
        return ideas_path

    def _wiki_link_for(self, video_id: str) -> str | None:
        index = _read_index(self._veille)
        rel = index.get(video_id)
        if not rel:
            return None
        return f"[[{Path(rel).with_suffix('').as_posix()}]]"

    # --- git ------------------------------------------------------------------------

    def _commit(self, message: str) -> str:
        # Finitions Lot 6 §1 : quand `_defer_commit` est actif (export_video ou
        # batch_commit), les mutations intermédiaires ne poussent pas de commit ;
        # un unique commit est fait à la sortie du bloc.
        if self._defer_commit:
            return "deferred"
        if not self._config.obsidian.git_sync:
            return "disabled"
        try:
            return _git_sync(self._config.obsidian.path, message, self._config.obsidian.git_remote)
        except (ObsidianExportError, subprocess.TimeoutExpired) as exc:
            log.warning("obsidian.git_failed", extra={"error": str(exc)})
            return "error"


# --- helpers frontmatter -----------------------------------------------------------------


def _update_frontmatter_status(text: str, status: str, theme: str) -> str:
    """Remplace `statut: …` et `theme: …` dans le frontmatter YAML. Si le fichier
    n'a pas de frontmatter, on l'ajoute au minimum avec ces deux clefs (rare)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        header = f"---\n{GUETTEUR_MARKER}\nstatut: {status}\ntheme: {theme}\n---\n"
        return header + text
    front_raw, body = match.group(1), match.group(2)
    new_front_lines: list[str] = []
    seen_status = False
    seen_theme = False
    for line in front_raw.splitlines():
        if line.startswith("statut:"):
            new_front_lines.append(f"statut: {_yaml_scalar(status)}")
            seen_status = True
        elif line.startswith("theme:"):
            new_front_lines.append(f"theme: {_yaml_scalar(theme)}")
            seen_theme = True
        else:
            new_front_lines.append(line)
    if not seen_status:
        new_front_lines.append(f"statut: {_yaml_scalar(status)}")
    if not seen_theme:
        new_front_lines.append(f"theme: {_yaml_scalar(theme)}")
    return "---\n" + "\n".join(new_front_lines) + "\n---\n" + body


def _render_idea_block(
    date_iso: str,
    note_link: str,
    score: int,
    idea: str,
    integration: str,
    effort: str,
    prompt: str,
) -> str:
    lines = [
        "",
        f"## {date_iso} — {note_link}",
        f"**Score** : {score}/3  |  **Effort** : {effort}",
        "",
        f"**Idée** : {idea}",
        "",
        f"**Intégration** : {integration}",
        "",
        "**Méga-prompt Claude Code** :",
        "",
        "```",
        prompt.strip(),
        "```",
    ]
    return "\n".join(lines)


# --- gabarits initiaux -----------------------------------------------------------------


def _default_taxonomy_template() -> str:
    return (
        "# Taxonomie\n\n"
        "Une section `## <thème>` par thème avec la liste des tags autorisés en dessous.\n"
        "Les tags proposés par Claude hors de cette liste vont dans `tags_proposes`.\n\n"
        "## LLM\n- claude\n- anthropic\n- prompt\n- context\n\n"
        "## Outils\n- uv\n- ruff\n- python\n- git\n\n"
        "## Automatisation\n- workflow\n- ci\n- deploiement\n"
    )


def _default_index_template() -> str:
    return (
        "# Index de la veille\n\n"
        "Ajoutez le plugin Dataview pour lister automatiquement les notes par statut,\n"
        "thème ou projet. Exemple :\n\n"
        "```dataview\n"
        "table niveau, theme, statut\n"
        'FROM "Veille/Inbox"\n'
        "SORT date_publication DESC\n"
        "```\n"
    )


def _default_project_sheets() -> dict[str, str]:
    """Fiches par défaut : coder, eagle, vigie, console, guetteur."""
    return {
        "coder": _sheet(
            "CODER",
            "Agent multi-projets de développement local",
            stack=["Python", "TypeScript", "Claude Code"],
            objectifs=[
                "Automatiser les tâches répétitives de dev",
                "Passer d'un projet à l'autre sans perdre le contexte",
            ],
            recherche=[
                "Prompts pour Claude Code, MCP, hooks, sub-agents",
                "Workflows agentic, orchestration multi-agent",
            ],
            exclusions=["Rien qui touche à des données utilisateur"],
        ),
        "eagle": _sheet(
            "EAGLE",
            "Gestion de bibliothèque d'images / références visuelles",
            stack=["Electron", "TypeScript"],
            objectifs=["Tagger vite, retrouver vite"],
            recherche=["Tagging auto par IA, embeddings d'images"],
        ),
        "vigie": _sheet(
            "VIGIE",
            "Navigation surveillée (anti-doomscrolling)",
            stack=["Electron", "TypeScript"],
            objectifs=["Réduire le temps d'écran passif"],
            recherche=["Détection d'usage, limites douces, focus"],
        ),
        "console": _sheet(
            "CONSOLE",
            "Terminal personnalisé cross-platform",
            stack=["Electron", "xterm.js"],
            objectifs=["Terminal rapide et joli"],
            recherche=["Terminals, séquences ANSI, plugins de shell"],
        ),
        "guetteur": _sheet(
            "GUETTEUR",
            "Veille YouTube résumée par Claude (ce projet)",
            stack=["Python 3.12", "uv", "Claude Code", "SQLite"],
            objectifs=["Récupérer, résumer, envoyer, archiver, tagger"],
            recherche=["APIs Telegram, YouTube, NotebookLM, Anthropic ; scoring d'applicabilité"],
        ),
    }


def _sheet(
    nom: str,
    statut: str,
    stack: list[str] | None = None,
    objectifs: list[str] | None = None,
    recherche: list[str] | None = None,
    exclusions: list[str] | None = None,
) -> str:
    def _list_block(name: str, items: list[str] | None) -> str:
        if not items:
            return f"{name}: []"
        return f"{name}:\n" + "\n".join(f"  - {i}" for i in items)

    lines = [
        "---",
        "guetteur: true",
        f"nom: {nom}",
        f"statut: {statut}",
        _list_block("stack", stack),
        _list_block("objectifs", objectifs),
        _list_block("recherche", recherche),
        _list_block("exclusions", exclusions),
        "---",
        "",
        f"# {nom}",
        "",
        "Fiche projet éditée à la main. GUETTEUR lit `recherche` et `exclusions` pour",
        "scorer chaque vidéo. Les modifications libres restent dans le corps.",
    ]
    return "\n".join(lines) + "\n"
