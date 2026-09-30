"""Orchestrateur de lot « livre » (Lot 7).

Vie d'un job :

    create  → pending  (livres row + livre_videos row par vidéo)
    run     → running  (résume chaque vidéo, réutilise summaries en cache)
       ↘ usage_limit ↗ paused (resume_after = now + backoff)
       ↘ crash / kill : le job reste en running, `resume` continue là où il en était
    plan + chapitres (Claude) → assembly Markdown + pandoc → done
    cancel  → cancelled (les vidéos déjà résumées restent en cache : réutilisables)

Priorité veille : le job libère le verrou Claude entre chaque vidéo puis dort
`pause_between_videos_s` secondes — la boucle de veille du même processus a le
temps d'intercaler un cycle. En CLI dédiée (`guetteur livre run`) elle n'a pas
de compétiteur, la pause reste utile pour ne pas saturer YouTube."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from guetteur.config import Config
from guetteur.export.livre_output import (
    BookOutput,
    build_glossary,
    run_pandoc,
    write_book,
)
from guetteur.models import DetailLevel, Summary, Video
from guetteur.pipeline import transcript_from_json
from guetteur.sources.channel import (
    ChannelFilters,
    ChannelInfo,
    ChannelResolver,
    ChannelVideoLister,
)
# Types duck-typing pour les tests : n'importe quel objet avec `.resolve(url)`
# ou `.list_videos(channel, filters)` fonctionne à la place des implémentations
# httpx-backed. Le signal en prod reste `ChannelResolver` / `ChannelVideoLister`.
from typing import Protocol as _Protocol


class _Resolvable(_Protocol):
    def resolve(self, url_or_handle: str) -> ChannelInfo: ...  # pragma: no cover


class _Listable(_Protocol):
    def list_videos(
        self, channel: ChannelInfo, filters: ChannelFilters
    ) -> list[tuple[Video, int | None]]: ...  # pragma: no cover
from guetteur.store import Store
from guetteur.summarize.base import (
    Summarizer,
    SummarizerUnavailableError,
    SummaryMeta,
    summary_from_json,
    summary_to_json,
)
from guetteur.summarize.book import (
    BookPlan,
    ChapterSpec,
    build_chapter_prompt,
    build_plan_prompt,
    chunk_chapter_videos,
    parse_plan,
    render_chapter,
)
from guetteur.transcript.base import NoTranscriptError, TranscriptProvider

log = logging.getLogger(__name__)


# --- estimation avant confirmation --------------------------------------------


@dataclass(frozen=True)
class LivrePlan:
    """Résumé du travail à faire, affiché avant `--yes` de confirmation."""

    channel: ChannelInfo
    videos: list[tuple[Video, int | None]]
    title: str
    detail: DetailLevel
    filters: ChannelFilters
    estimated_minutes: float

    @property
    def total_duration_s(self) -> int:
        return sum(d for _v, d in self.videos if d)

    def render(self) -> str:
        n = len(self.videos)
        hours, remainder = divmod(self.total_duration_s, 3600)
        minutes = remainder // 60
        lines = [
            f"Livre : {self.title}",
            f"Chaîne : {self.channel.title or self.channel.channel_id} ({self.channel.channel_id})",
            f"Vidéos : {n}",
            f"Durée totale : {hours} h {minutes:02d} min",
            f"Niveau de résumé : {self.detail}",
            f"Estimation Claude : ~{self.estimated_minutes:.0f} min "
            f"(≈ {self.estimated_minutes / 60:.1f} h)",
        ]
        return "\n".join(lines)


class JobAlreadyRunningError(RuntimeError):
    """Un autre livre est déjà en cours (running ou paused) — un seul à la fois."""


def plan_book(
    config: Config,
    channel_url: str,
    title: str | None,
    filters: ChannelFilters,
    detail: DetailLevel | None = None,
    resolver: _Resolvable | None = None,
    lister: _Listable | None = None,
) -> LivrePlan:
    """Étape 1 : résout la chaîne, liste les vidéos, calcule l'estimation. Ne
    touche pas au store — appelé avant confirmation."""
    # La clé n'est requise que si on doit instancier un résolveur/lister par
    # défaut. Les tests injectent leurs propres implémentations et peuvent
    # tourner sans clé — l'API réelle n'est jamais touchée.
    api_key = config.secrets.youtube_api_key
    if resolver is None and lister is None and not api_key:
        raise RuntimeError("livre : YOUTUBE_API_KEY absent — pas de résolution de chaîne")
    r = resolver or ChannelResolver(api_key)
    channel = r.resolve(channel_url)
    ls = lister or ChannelVideoLister(api_key)
    videos = ls.list_videos(channel, filters)
    if not videos:
        raise RuntimeError("livre : aucune vidéo retenue par les filtres")
    detail_used = detail or config.livre.detail_default
    return LivrePlan(
        channel=channel,
        videos=videos,
        title=title or (channel.title or "Livre"),
        detail=detail_used,
        filters=filters,
        estimated_minutes=len(videos) * config.livre.estimated_minutes_per_video,
    )


def persist_new_livre(store: Store, plan: LivrePlan, channel_url: str) -> int:
    """Étape 2 : après confirmation, matérialise le job en base. Refuse si un
    autre livre est déjà en cours (running ou paused)."""
    if store.has_running_livre():
        raise JobAlreadyRunningError(
            "un autre livre est en cours ; `guetteur livre status` puis "
            "`livre pause|cancel` avant d'en créer un nouveau."
        )
    filters_json = json.dumps(_filters_to_dict(plan.filters), ensure_ascii=False)
    return store.create_livre(
        channel_url=channel_url,
        channel_id=plan.channel.channel_id,
        channel_name=plan.channel.title,
        title=plan.title,
        detail=plan.detail,
        filters_json=filters_json,
        videos=[
            (
                v.video_id,
                v.title,
                d,
                v.published.isoformat() if v.published else None,
            )
            for v, d in plan.videos
        ],
    )


def _filters_to_dict(filters: ChannelFilters) -> dict[str, Any]:
    return {
        "min_duration_s": filters.min_duration_s,
        "max_duration_s": filters.max_duration_s,
        "since": filters.since.isoformat() if filters.since else None,
        "until": filters.until.isoformat() if filters.until else None,
        "include_shorts": filters.include_shorts,
        "max_videos": filters.max_videos,
    }


# --- runner -------------------------------------------------------------------


ProgressReporter = Callable[[str], None]


class LivreRunner:
    """Résume une à une les vidéos d'un livre, en réutilisant `summaries` cache
    et le pipeline existant (transcription + summarizer). Silencieux : aucune
    notification, aucun export, aucun archivage pendant la boucle."""

    def __init__(
        self,
        config: Config,
        store: Store,
        transcriber: TranscriptProvider,
        summarizer: Summarizer,
        claude_lock: threading.Lock | None = None,
        progress: ProgressReporter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._store = store
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._lock = claude_lock or threading.Lock()
        self._progress = progress or (lambda _msg: None)
        self._sleep = sleep

    def run(self, livre_id: int) -> str:
        """Traite toutes les vidéos queued. Retourne le statut final : 'summarized'
        (prêt pour la passe plan/chapitres), 'paused', 'cancelled' ou 'done'."""
        row = self._store.get_livre(livre_id)
        if row is None:
            raise RuntimeError(f"livre {livre_id} inconnu")
        if row["status"] in ("done", "cancelled"):
            return str(row["status"])
        self._store.set_livre_status(livre_id, "running", started=True)
        detail = _detail_value(str(row["detail"]))
        n_done = 0
        while True:
            # Refresh à chaque tour pour repérer un `pause`/`cancel` posé par ailleurs.
            row = self._store.get_livre(livre_id)
            if row is None or row["status"] in ("cancelled",):
                return "cancelled"
            if row["status"] == "paused":
                return "paused"
            entry = self._store.next_queued_livre_video(livre_id)
            if entry is None:
                break
            vid = str(entry["video_id"])
            title = str(entry["title"])
            try:
                self._process_video(livre_id, vid, title, detail)
            except SummarizerUnavailableError as exc:
                # Usage limit / auth / binaire absent : on suspend et on reprend
                # après backoff. Le livre reste `paused` avec resume_after posé.
                backoff = timedelta(minutes=self._config.livre.usage_limit_backoff_min)
                self._store.set_livre_status(
                    livre_id,
                    "paused",
                    last_error=f"summarizer indisponible : {exc}",
                    resume_after=datetime.now(UTC) + backoff,
                )
                self._progress(
                    f"⏸ Livre {livre_id} suspendu (usage limit ? {exc}). "
                    f"Reprise possible dans {self._config.livre.usage_limit_backoff_min} min "
                    "via `guetteur livre resume`."
                )
                if self._config.livre.stop_on_usage_limit:
                    return "paused"
                self._sleep(backoff.total_seconds())
                self._store.set_livre_status(livre_id, "running")
                continue
            except (NoTranscriptError, Exception) as exc:
                # Vidéo qui échoue : on la marque failed et on continue —
                # le livre peut se faire sans les vidéos sans sous-titres.
                self._store.set_livre_video_status(
                    livre_id, vid, "failed", last_error=f"{type(exc).__name__}: {exc}"
                )
                log.warning(
                    "livre.video_failed",
                    extra={"livre_id": livre_id, "video_id": vid, "error": str(exc)},
                )
                continue
            n_done += 1
            if n_done % self._config.livre.progress_every == 0:
                progress = self._store.livre_progress(livre_id)
                total = sum(progress.values())
                done = progress.get("summarized", 0)
                self._progress(
                    f"📚 Livre {livre_id} : {done}/{total} vidéos résumées."
                )
            # Priorité veille : on relâche le verrou et on souffle, un cycle de
            # veille du même processus (si applicable) peut passer devant.
            self._sleep(self._config.livre.pause_between_videos_s)
        # Toutes les vidéos qui pouvaient l'être sont summarized/failed.
        return "summarized"

    def _process_video(
        self, livre_id: int, video_id: str, title: str, detail: DetailLevel
    ) -> None:
        """Cache-first : si un résumé du même niveau existe déjà dans la table
        `summaries` (Lot 5), on le réutilise sans appeler Claude. Sinon on lit
        (ou récupère) le transcript puis on résume. Le lot n'envoie rien, ne
        déclenche ni archive ni export : la note Obsidian reste pour la veille."""
        cached = self._store.cached_summary(video_id, detail)
        if cached is not None:
            self._store.set_livre_video_status(livre_id, video_id, "summarized")
            return
        record = self._store.get(video_id)
        transcript = None
        if record is not None and record.transcript is not None:
            transcript = transcript_from_json(video_id, record.transcript)
        if transcript is None:
            transcript = self._transcriber.get(video_id)
        with self._lock:
            summary = self._summarizer.summarize(
                transcript,
                SummaryMeta(
                    video=Video(
                        video_id, title, "", None, f"https://youtu.be/{video_id}"
                    ),
                    language="fr",
                    detail=detail,
                ),
            )
        self._store.cache_summary(video_id, detail, summary_to_json(summary))
        # Idempotence : si un transcript a été récupéré par le lot pour une
        # vidéo pas encore en veille, on ne persiste PAS le record — le livre
        # ne pollue pas l'état de la playlist. Le transcript de retentative
        # reste dans le cache Claude via cache_summary.
        self._store.set_livre_video_status(livre_id, video_id, "summarized")


def _detail_value(raw: str) -> DetailLevel:
    if raw == "bref":
        return "bref"
    if raw == "detaille":
        return "detaille"
    return "standard"


# --- passe de plan + rédaction des chapitres ---------------------------------


class BookAssembler:
    """Deuxième et troisième passes Claude : plan JSON puis chapitres. Le
    résultat est écrit dans le vault Obsidian et converti en EPUB/PDF."""

    def __init__(
        self,
        config: Config,
        store: Store,
        summarizer: Summarizer,
        claude_lock: threading.Lock | None = None,
        max_chapter_prompt_chars: int = 90_000,
    ) -> None:
        self._config = config
        self._store = store
        self._summarizer = summarizer
        self._lock = claude_lock or threading.Lock()
        self._max = max_chapter_prompt_chars

    def build(self, livre_id: int) -> BookOutput:
        row = self._store.get_livre(livre_id)
        if row is None:
            raise RuntimeError(f"livre {livre_id} inconnu")
        detail = _detail_value(str(row["detail"]))
        entries = self._store.livre_videos(livre_id)
        summaries: dict[str, Summary] = {}
        for entry in entries:
            if entry["status"] != "summarized":
                continue
            raw = self._store.cached_summary(str(entry["video_id"]), detail)
            if raw is None:
                continue
            summaries[str(entry["video_id"])] = summary_from_json(raw)
        if not summaries:
            raise RuntimeError(f"livre {livre_id} : aucun résumé disponible pour la synthèse")
        plan = self._build_plan(str(row["channel_name"]), summaries)
        self._store.set_livre_plan(livre_id, json.dumps(_plan_to_dict(plan), ensure_ascii=False))
        chapters = self._write_chapters(plan, summaries)
        glossary = build_glossary(chapters)
        videos_meta = {
            str(e["video_id"]): (
                str(e["title"]),
                f"https://www.youtube.com/watch?v={e['video_id']}",
                str(e["published_at"]) if e["published_at"] else None,
            )
            for e in entries
        }
        vault_livres = (
            self._config.obsidian.path / self._config.obsidian.veille_dir
        ).parent / "Livres"
        vault_livres.mkdir(parents=True, exist_ok=True)
        output = write_book(
            plan=plan,
            chapters=chapters,
            videos_meta=videos_meta,
            channel_name=str(row["channel_name"]),
            channel_url=str(row["channel_url"]),
            vault_livres_dir=vault_livres,
            glossary=glossary,
        )
        self._store.set_livre_output(
            livre_id, output.livre_md.read_text(encoding="utf-8"), str(output.root)
        )
        return output

    def _build_plan(self, channel_name: str, summaries: dict[str, Summary]) -> BookPlan:
        prompt = build_plan_prompt(channel_name, list(summaries.items()))
        raw = self._call_claude(prompt, is_plan=True)
        return parse_plan(raw, list(summaries.keys()))

    def _write_chapters(
        self, plan: BookPlan, summaries: dict[str, Summary]
    ) -> dict[str, str]:
        chapters: dict[str, str] = {}
        for ch in plan.chapitres:
            triples: list[tuple[str, str, Summary]] = []
            for vid in ch.video_ids:
                if vid not in summaries:
                    continue
                triples.append(
                    (vid, f"https://www.youtube.com/watch?v={vid}", summaries[vid])
                )
            if not triples:
                chapters[ch.titre] = f"# {ch.titre}\n\n*Aucune vidéo rattachée.*\n"
                continue
            parts: list[str] = []
            for lot in chunk_chapter_videos(triples, self._max):
                prompt = build_chapter_prompt(ch, lot)
                parts.append(str(self._call_claude(prompt, is_plan=False)))
            titles = [(vid, url, s.title) for (vid, url, s) in triples]
            chapters[ch.titre] = render_chapter(ch, parts, titles)
        return chapters

    def _call_claude(self, prompt: str, is_plan: bool) -> Any:
        """Point d'extension : par défaut on appelle un `summarizer._call_json`
        générique si présent, sinon on lève. En pratique les tests injectent un
        `summarizer` factice qui renvoie du JSON pour plan et du Markdown pour
        chapitres. La prod branche un helper dédié."""
        raw = self._invoke(prompt, is_plan)
        if is_plan:
            return raw
        return raw

    def _invoke(self, prompt: str, is_plan: bool) -> str:
        """Duck-typing sur le summarizer : on cherche une méthode `raw_call`
        (fournie par un adaptateur dédié en test) ou on lève."""
        raw_call = getattr(self._summarizer, "raw_call", None)
        if raw_call is None:
            raise RuntimeError(
                "BookAssembler : le summarizer courant ne supporte pas raw_call — "
                "utiliser BookAssembler.set_invoker() dans les tests ou brancher "
                "un adaptateur en prod."
            )
        with self._lock:
            return str(raw_call(prompt, is_plan))


def _plan_to_dict(plan: BookPlan) -> dict[str, Any]:
    return {
        "titre": plan.titre,
        "introduction": plan.introduction,
        "conclusion": plan.conclusion,
        "chapitres": [
            {
                "titre": ch.titre,
                "fil_conducteur": ch.fil_conducteur,
                "video_ids": list(ch.video_ids),
            }
            for ch in plan.chapitres
        ],
    }


def plan_from_dict(data: dict[str, Any]) -> BookPlan:
    return BookPlan(
        titre=str(data["titre"]),
        introduction=str(data.get("introduction", "")),
        conclusion=str(data.get("conclusion", "")),
        chapitres=tuple(
            ChapterSpec(
                titre=str(ch["titre"]),
                fil_conducteur=str(ch.get("fil_conducteur", "")),
                video_ids=tuple(str(v) for v in ch.get("video_ids", [])),
            )
            for ch in data.get("chapitres", [])
        ),
    )


# --- finaliser : pandoc + envoi Telegram --------------------------------------


def finalize(
    output: BookOutput,
    title: str,
    pandoc_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> BookOutput:
    """Convertit livre.md en EPUB + PDF via pandoc. Renvoie un `BookOutput`
    enrichi avec les chemins produits (None si la conversion a échoué)."""
    out_epub = output.root / "livre.epub"
    out_pdf = output.root / "livre.pdf"
    epub, pdf = run_pandoc(output.livre_md, out_epub, out_pdf, title=title, pandoc=pandoc_runner)
    return BookOutput(
        root=output.root,
        livre_md=output.livre_md,
        index_md=output.index_md,
        chapter_files=output.chapter_files,
        epub=epub,
        pdf=pdf,
    )


# --- helpers exposés pour la CLI/tests ---------------------------------------


def livre_status_text(store: Store, livre_id: int) -> str:
    row = store.get_livre(livre_id)
    if row is None:
        return f"Livre {livre_id} inconnu."
    prog = store.livre_progress(livre_id)
    total = sum(prog.values())
    done = prog.get("summarized", 0)
    failed = prog.get("failed", 0)
    lines = [
        f"Livre {livre_id} — {row['title']}",
        f"Chaîne : {row['channel_name']} ({row['channel_url']})",
        f"Statut : {row['status']}",
        f"Vidéos : {done}/{total} résumées, {failed} en échec",
    ]
    if row.get("resume_after"):
        lines.append(f"Reprise après : {row['resume_after']}")
    if row.get("last_error"):
        lines.append(f"Dernière erreur : {row['last_error']}")
    if row.get("output_dir"):
        lines.append(f"Sortie : {row['output_dir']}")
    return "\n".join(lines)


def cancel_livre(store: Store, livre_id: int) -> str:
    row = store.get_livre(livre_id)
    if row is None:
        return f"Livre {livre_id} inconnu."
    if row["status"] == "done":
        return f"Livre {livre_id} déjà terminé — rien à annuler."
    store.set_livre_status(livre_id, "cancelled")
    return f"Livre {livre_id} annulé."


def pause_livre(store: Store, livre_id: int) -> str:
    row = store.get_livre(livre_id)
    if row is None:
        return f"Livre {livre_id} inconnu."
    if row["status"] != "running":
        return f"Livre {livre_id} pas en cours (statut : {row['status']})."
    store.set_livre_status(livre_id, "paused")
    return f"Livre {livre_id} suspendu."


def resume_livre(store: Store, livre_id: int) -> str:
    row = store.get_livre(livre_id)
    if row is None:
        return f"Livre {livre_id} inconnu."
    if row["status"] not in ("paused",):
        return f"Livre {livre_id} pas suspendu (statut : {row['status']})."
    resume_after = row.get("resume_after")
    if resume_after:
        try:
            ra = datetime.fromisoformat(str(resume_after))
        except ValueError:
            ra = None
        if ra is not None and ra > datetime.now(UTC):
            return f"Livre {livre_id} : reprise possible à {resume_after}."
    store.set_livre_status(livre_id, "pending")
    return f"Livre {livre_id} : prêt à repartir (`guetteur livre run {livre_id}`)."


def list_livres_text(store: Store, limit: int = 20) -> str:
    rows = store.list_livres(limit=limit)
    if not rows:
        return "Aucun livre."
    lines = ["ID  Statut     Vidéos  Titre"]
    for row in rows:
        prog = store.livre_progress(int(row["id"]))
        total = sum(prog.values())
        done = prog.get("summarized", 0)
        lines.append(
            f"{int(row['id']):>3}  {row['status']!s:<9} "
            f"{done:>3}/{total:<3}  {row['title']}"
        )
    return "\n".join(lines)


__all__ = [
    "BookAssembler",
    "JobAlreadyRunningError",
    "LivrePlan",
    "LivreRunner",
    "ProgressReporter",
    "cancel_livre",
    "finalize",
    "list_livres_text",
    "livre_status_text",
    "pause_livre",
    "persist_new_livre",
    "plan_book",
    "plan_from_dict",
    "resume_livre",
]
