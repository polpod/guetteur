"""Tests unitaires de jobs/livre.py (Lot 7) : reprise après crash, priorité
veille (verrou partagé), un seul job à la fois, cache-first, pause/cancel."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import LivreConfig, ObsidianConfig
from guetteur.jobs.livre import (
    BookAssembler,
    JobAlreadyRunningError,
    LivrePlan,
    LivreRunner,
    cancel_livre,
    livre_status_text,
    pause_livre,
    persist_new_livre,
    resume_livre,
)
from guetteur.models import KeyPoint, Segment, Summary, Transcript, Video
from guetteur.sources.channel import ChannelFilters, ChannelInfo
from guetteur.store import Store
from guetteur.summarize.base import (
    Summarizer,
    SummarizerUnavailableError,
    SummaryMeta,
    summary_to_json,
)
from tests.helpers import make_config


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, video_id: str) -> Transcript:
        self.calls.append(video_id)
        return Transcript(video_id, "fr", "youtube", (Segment(0.0, f"transcript de {video_id}"),))


class RecordingSummarizer(Summarizer):
    """Summarizer factice qui compte les appels et peut lever à volonté."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls: list[str] = []
        self._fail_after = fail_after

    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary:
        self.calls.append(transcript.video_id)
        if self._fail_after is not None and len(self.calls) > self._fail_after:
            raise SummarizerUnavailableError("simulated usage limit")
        return Summary(
            title=f"Résumé {transcript.video_id}",
            tldr="tl;dr",
            key_points=(KeyPoint(0, "point"),),
            why_it_matters="",
            reading_time_minutes=1,
            detail=meta.detail,
        )

    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, object] | None,
        timeout_s: float,
    ) -> str:
        # Pas de BookAssembler dans ces tests : si on y passe, c'est une erreur.
        raise NotImplementedError("RecordingSummarizer : raw_call non utilisé ici")


def _fake_plan(tmp_path: Path, n_videos: int = 3) -> LivrePlan:
    videos: list[tuple[Video, int | None, int | None]] = []
    for i in range(n_videos):
        vid = f"vid{i:08d}xy"  # 11 chars
        v = Video(vid, f"Titre {i}", "", datetime(2026, 1, 1, tzinfo=UTC), f"https://youtu.be/{vid}")
        videos.append((v, 900, 1_000 + i * 10))
    return LivrePlan(
        channel=ChannelInfo(
            channel_id="UC" + "x" * 22,
            uploads_playlist_id="UU" + "x" * 22,
            title="Chaîne test",
        ),
        videos=videos,
        title="Livre test",
        detail="standard",
        filters=ChannelFilters(),
        estimated_minutes=12.0,
    )


def _config(tmp_path: Path, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"pause_between_videos_s": 0.0, "progress_every": 100}
    kwargs.update(overrides)
    return make_config(tmp_path, livre=LivreConfig(**kwargs))


def test_persist_refuses_second_job_while_one_is_running(tmp_path: Path) -> None:
    _config(tmp_path)  # instancie la config pour valider les valeurs
    store = Store(tmp_path / "db.sqlite")
    try:
        plan = _fake_plan(tmp_path, n_videos=2)
        first = persist_new_livre(store, plan, "https://youtube.com/@a")
        # Le premier est encore `pending` — un second doit être refusé, mais on
        # ne bloque que sur running/paused. Une fois `running` on refuse.
        store.set_livre_status(first, "running")
        with pytest.raises(JobAlreadyRunningError):
            persist_new_livre(store, plan, "https://youtube.com/@b")
        # `paused` bloque aussi (backoff en cours) :
        store.set_livre_status(first, "paused")
        with pytest.raises(JobAlreadyRunningError):
            persist_new_livre(store, plan, "https://youtube.com/@b")
        # `done` ou `cancelled` autorisent le suivant.
        store.set_livre_status(first, "done", finished=True)
        second = persist_new_livre(store, plan, "https://youtube.com/@c")
        assert second != first
    finally:
        store.close()


def test_runner_processes_all_and_uses_summary_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    try:
        plan = _fake_plan(tmp_path, n_videos=3)
        livre_id = persist_new_livre(store, plan, "https://youtube.com/@x")
        # On préseed le cache pour la 1re vidéo : le runner ne doit PAS l'appeler.
        vid0 = plan.videos[0][0].video_id
        cached = Summary(
            title="cached",
            tldr="t",
            key_points=(KeyPoint(0, "p"),),
            why_it_matters="",
            reading_time_minutes=1,
            detail="standard",
        )
        store.cache_summary(vid0, "standard", summary_to_json(cached))
        transcriber = FakeTranscriber()
        summarizer = RecordingSummarizer()
        runner = LivreRunner(
            config=config,
            store=store,
            transcriber=transcriber,
            summarizer=summarizer,
            sleep=lambda _s: None,
        )
        assert runner.run(livre_id) == "summarized"
        # 2 appels sur les 3 vidéos : la première est cachée.
        assert len(summarizer.calls) == 2
        # Les 3 sont bien en status `summarized`.
        prog = store.livre_progress(livre_id)
        assert prog.get("summarized") == 3
    finally:
        store.close()


def test_runner_resumes_after_crash_where_it_left_off(tmp_path: Path) -> None:
    """Simule un crash : summarizer lève après 2 vidéos, le runner passe le job
    en `paused`. Un second `run` (fresh summarizer) doit finir les 2 dernières."""
    config = _config(tmp_path, stop_on_usage_limit=True)
    store = Store(tmp_path / "db.sqlite")
    try:
        plan = _fake_plan(tmp_path, n_videos=4)
        livre_id = persist_new_livre(store, plan, "https://youtube.com/@x")
        transcriber = FakeTranscriber()
        breaking = RecordingSummarizer(fail_after=2)
        runner = LivreRunner(
            config=config,
            store=store,
            transcriber=transcriber,
            summarizer=breaking,
            sleep=lambda _s: None,
        )
        outcome = runner.run(livre_id)
        assert outcome == "paused"
        row = store.get_livre(livre_id)
        assert row is not None
        assert row["status"] == "paused"
        assert store.livre_progress(livre_id).get("summarized") == 2
        # Reprise : le backoff est encore actif, `resume` refuse en attendant.
        assert "reprise possible" in resume_livre(store, livre_id).lower()
        # On force le passé : la reprise peut aboutir.
        store.set_livre_status(
            livre_id, "paused", resume_after=datetime.now(UTC) - timedelta(hours=1)
        )
        assert "prêt à repartir" in resume_livre(store, livre_id)
        recovered = RecordingSummarizer()
        runner2 = LivreRunner(
            config=config,
            store=store,
            transcriber=transcriber,
            summarizer=recovered,
            sleep=lambda _s: None,
        )
        assert runner2.run(livre_id) == "summarized"
        # Il ne reste que 2 vidéos à faire (les 2 déjà summarized ne sont pas rejouées).
        assert len(recovered.calls) == 2
        assert store.livre_progress(livre_id).get("summarized") == 4
    finally:
        store.close()


def test_runner_respects_shared_claude_lock(tmp_path: Path) -> None:
    """Priorité veille : entre chaque vidéo le runner relâche le verrou et dort.
    Ce test vérifie que le verrou est SORTI de la section critique entre deux
    appels — un autre thread peut donc l'acquérir sans blocage."""
    config = _config(tmp_path, pause_between_videos_s=0.05)
    store = Store(tmp_path / "db.sqlite")
    try:
        plan = _fake_plan(tmp_path, n_videos=3)
        livre_id = persist_new_livre(store, plan, "https://youtube.com/@x")
        lock = threading.Lock()
        summarizer = RecordingSummarizer()
        runner = LivreRunner(
            config=config,
            store=store,
            transcriber=FakeTranscriber(),
            summarizer=summarizer,
            claude_lock=lock,
            sleep=lambda _s: None,
        )
        # On lance le runner dans un thread — pendant qu'il tourne on essaie
        # d'acquérir le lock plusieurs fois. Si le runner le tenait tout le temps,
        # notre acquisition échouerait avant timeout.
        acquired = threading.Event()

        def race() -> None:
            for _ in range(50):
                if lock.acquire(timeout=0.5):
                    lock.release()
                    acquired.set()
                    return

        racer = threading.Thread(target=race)
        racer.start()
        runner.run(livre_id)
        racer.join(timeout=5)
        assert acquired.is_set(), "la veille n'a jamais pu prendre le verrou"
    finally:
        store.close()


def test_cancel_and_pause_are_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    try:
        plan = _fake_plan(tmp_path, n_videos=2)
        livre_id = persist_new_livre(store, plan, "u")
        assert "annulé" in cancel_livre(store, livre_id)
        # Un pause sur un job cancelled ne change rien de dangereux.
        assert "pas en cours" in pause_livre(store, livre_id).lower()
    finally:
        store.close()


# --- rendu du plan (titre · vues · durée) ------------------------------------


def test_plan_render_shows_each_video_with_views_and_duration(tmp_path: Path) -> None:
    """Le rendu du plan doit afficher la ligne de tri (`triées par <order>`)
    puis une entrée par vidéo avec titre, vues formatées (`1.0k`, `1.2M`…) et
    durée au format `h/m/s`."""
    plan = _fake_plan(tmp_path, n_videos=3)
    # On en profite pour injecter une vue élevée et une durée différente pour
    # verifier le formatage.
    plan = LivrePlan(
        channel=plan.channel,
        videos=[
            (plan.videos[0][0], 125, 1_500_000),  # 2m05, 1.5M vues
            (plan.videos[1][0], 3661, 2_200),  # 1h01, 2.2k vues
            (plan.videos[2][0], None, None),  # durée inconnue, vues absentes
        ],
        title=plan.title,
        detail=plan.detail,
        filters=ChannelFilters(order="views"),
        estimated_minutes=plan.estimated_minutes,
    )
    out = plan.render()
    assert "triées par views" in out
    assert "1.5M vues" in out
    assert "2.2k vues" in out
    assert "— vues" in out
    assert "2m05" in out
    assert "1h01" in out
    # Chaque vidéo apparaît sur sa ligne — titre tronqué si trop long.
    for v, _d, _vc in plan.videos:
        assert v.title in out


# --- parseur /livre côté bot --------------------------------------------------


def test_parse_livre_cmd_args_defaults() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    url, order, max_videos = _parse_livre_cmd_args(["https://youtube.com/@chaine"])
    assert url == "https://youtube.com/@chaine"
    assert order == "date"
    assert max_videos is None


def test_parse_livre_cmd_args_order_and_max() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    url, order, max_videos = _parse_livre_cmd_args(
        ["https://youtube.com/@c", "--order", "views", "--max", "10"]
    )
    assert url == "https://youtube.com/@c"
    assert order == "views"
    assert max_videos == 10


def test_parse_livre_cmd_args_flags_before_url() -> None:
    """Ordre libre : les flags peuvent précéder l'URL."""
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    url, order, _ = _parse_livre_cmd_args(
        ["--order", "duration", "https://youtube.com/@c"]
    )
    assert url == "https://youtube.com/@c"
    assert order == "duration"


def test_parse_livre_cmd_args_rejects_unknown_order() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    with pytest.raises(ValueError, match="--order inconnu"):
        _parse_livre_cmd_args(["https://y/@c", "--order", "random"])


def test_parse_livre_cmd_args_rejects_bad_max() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    with pytest.raises(ValueError, match="--max"):
        _parse_livre_cmd_args(["https://y/@c", "--max", "zero"])
    with pytest.raises(ValueError, match="strictement positif"):
        _parse_livre_cmd_args(["https://y/@c", "--max", "0"])


def test_parse_livre_cmd_args_requires_url() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    with pytest.raises(ValueError, match="URL"):
        _parse_livre_cmd_args(["--order", "views"])


def test_parse_livre_cmd_args_rejects_unknown_option() -> None:
    from guetteur.notify.telegram_bot import _parse_livre_cmd_args

    with pytest.raises(ValueError, match="option inconnue"):
        _parse_livre_cmd_args(["https://y/@c", "--since", "2026-01-01"])


# --- BookAssembler : reprise par chapitre (Lot 7bis) ------------------------


def _assembler_setup(
    tmp_path: Path, n_videos: int = 4, n_chapters: int = 3
) -> tuple[Any, Store, int, list[str]]:
    """Prépare un livre dont toutes les vidéos sont déjà `summarized` et dont le
    cache de résumés est renseigné. Renvoie (config, store, livre_id, video_ids)."""
    vault = tmp_path / "vault"
    (vault / "Veille").mkdir(parents=True)
    obs = ObsidianConfig(enabled=True, path=vault, git_sync=False, git_remote="")
    config = make_config(
        tmp_path,
        obsidian=obs,
        livre=LivreConfig(
            pause_between_videos_s=0.0,
            progress_every=100,
            notebooklm=False,
            plan_timeout_s=600.0,
            chapter_timeout_s=900.0,
        ),
    )
    store = Store(tmp_path / "db.sqlite")
    plan = _fake_plan(tmp_path, n_videos=n_videos)
    livre_id = persist_new_livre(store, plan, "https://youtube.com/@x")
    vids = []
    for v, _d, _vc in plan.videos:
        vids.append(v.video_id)
        store.cache_summary(
            v.video_id,
            "standard",
            summary_to_json(
                Summary(
                    title=f"Résumé {v.video_id}",
                    tldr="TL;DR.",
                    key_points=(KeyPoint(0, "point A"),),
                    why_it_matters="",
                    reading_time_minutes=1,
                    detail="standard",
                )
            ),
        )
        store.set_livre_video_status(livre_id, v.video_id, "summarized")
    # Prépare un plan JSON persisté : évite l'appel plan en test de reprise.
    plan_json = json.dumps(
        {
            "titre": "Livre test",
            "introduction": "intro",
            "conclusion": "fin",
            "chapitres": [
                {
                    "titre": f"Chapitre {i + 1}",
                    "fil_conducteur": f"fil {i + 1}",
                    "video_ids": [vids[j] for j in range(i, len(vids), n_chapters)],
                }
                for i in range(n_chapters)
            ],
        },
        ensure_ascii=False,
    )
    store.set_livre_plan(livre_id, plan_json)
    return config, store, livre_id, vids


class _TrackingInvoker:
    """Invoker factice qui compte les appels et peut lever une fois sur un chapitre
    donné. Les plans utilisent le plan_json déjà posé en base (donc jamais rappelés)."""

    def __init__(self, fail_on_chapter: int | None = None) -> None:
        self.calls: list[tuple[str, bool, float]] = []
        self._fail = fail_on_chapter
        self._chapter_count = 0

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str:
        is_plan = json_schema is not None
        self.calls.append((user_prompt[:30], is_plan, timeout_s))
        if is_plan:
            return json.dumps(
                {
                    "titre": "Livre",
                    "introduction": "intro",
                    "chapitres": [
                        {"titre": "Chapitre 1", "fil_conducteur": "f", "video_ids": []}
                    ],
                    "conclusion": "fin",
                }
            )
        self._chapter_count += 1
        if self._fail is not None and self._chapter_count == self._fail:
            raise RuntimeError(f"crash chapitre {self._chapter_count}")
        return f"## Chapitre {self._chapter_count}\n\nContenu rédigé."


def test_assembler_skips_summaries_and_runs_plan_then_chapters(tmp_path: Path) -> None:
    """Les résumés sont déjà complets côté base : BookAssembler saute la boucle
    LivreRunner et appelle directement plan + chapitres. Le plan JSON étant déjà
    persisté, raw_call n'est appelé QUE pour les chapitres."""
    config, store, livre_id, _vids = _assembler_setup(tmp_path, n_videos=6, n_chapters=3)
    try:
        invoker = _TrackingInvoker()
        assembler = BookAssembler(config, store, summarizer=RecordingSummarizer())
        assembler.set_invoker(invoker)
        output = assembler.build(livre_id)
        assert output.livre_md.exists()
        # Le plan JSON était déjà persisté → aucun appel plan.
        assert all(not is_plan for _p, is_plan, _t in invoker.calls)
        # Un appel chapitre par chapitre non vide (3 ici) ; aucun chapitre « Divers »
        # inattendu puisque les 6 vidéos sont réparties sur 3 chapitres.
        chapter_calls = [c for c in invoker.calls if not c[1]]
        assert len(chapter_calls) == 3
        # Timeout chapitre bien propagé (900 s en défaut livre).
        assert all(timeout == 900.0 for _p, _is_plan, timeout in chapter_calls)
        # Statut phase = done après build.
        row = store.get_livre(livre_id)
        assert row is not None
        assert row["phase"] == "done"
        chapters = {c["rank"]: c for c in store.livre_chapters(livre_id)}
        assert len(chapters) == 3
        assert all(c["status"] == "done" and c["markdown"] for c in chapters.values())
    finally:
        store.close()


def test_assembler_resumes_at_next_chapter_after_crash(tmp_path: Path) -> None:
    """Chapitre 2 crashe : chapitres 1 persisté, 2 en failed. Reprise : seul le
    chapitre 2 et les suivants sont retentés — le 1 n'est jamais rappelé."""
    config, store, livre_id, _vids = _assembler_setup(tmp_path, n_videos=6, n_chapters=3)
    try:
        # Première passe : crash au chapitre 2.
        crashing = _TrackingInvoker(fail_on_chapter=2)
        assembler = BookAssembler(config, store, summarizer=RecordingSummarizer())
        assembler.set_invoker(crashing)
        with pytest.raises(RuntimeError, match="crash chapitre 2"):
            assembler.build(livre_id)
        chapters = {c["rank"]: c for c in store.livre_chapters(livre_id)}
        # Chapitre 1 persisté, 2 failed, 3 pending.
        assert chapters[0]["status"] == "done" and chapters[0]["markdown"]
        assert chapters[1]["status"] == "failed"
        assert chapters[1].get("last_error") and "crash chapitre 2" in str(
            chapters[1]["last_error"]
        )
        assert chapters[2]["status"] == "pending"
        # 2 appels chapitre (1 ok + 2 crash). Pas d'appel plan (plan_json déjà posé).
        chapter_calls = [c for c in crashing.calls if not c[1]]
        assert len(chapter_calls) == 2

        # Seconde passe : nouveau invoker (le binaire a été relancé).
        resumed = _TrackingInvoker()
        assembler = BookAssembler(config, store, summarizer=RecordingSummarizer())
        assembler.set_invoker(resumed)
        output = assembler.build(livre_id)
        assert output.livre_md.exists()
        # Reprise : seuls les chapitres 2 et 3 ont été rappelés. Le 1 reste en place.
        chapter_calls_resumed = [c for c in resumed.calls if not c[1]]
        assert len(chapter_calls_resumed) == 2
        chapters = {c["rank"]: c for c in store.livre_chapters(livre_id)}
        assert all(c["status"] == "done" for c in chapters.values())
        row = store.get_livre(livre_id)
        assert row is not None and row["phase"] == "done"
    finally:
        store.close()


def test_livre_status_text_includes_phase_and_chapter_progress(tmp_path: Path) -> None:
    _config, store, livre_id, _vids = _assembler_setup(tmp_path, n_videos=4, n_chapters=2)
    try:
        store.init_livre_chapters(livre_id, ["Chapitre 1", "Chapitre 2"])
        store.set_livre_chapter(livre_id, 0, "done", markdown="## c1")
        store.set_livre_chapter(livre_id, 1, "failed", last_error="boom")
        store.set_livre_phase(livre_id, "writing")
        txt = livre_status_text(store, livre_id)
        assert "phase : writing" in txt
        assert "Chapitres : 1/2 rédigés" in txt
        assert "1 en échec" in txt
    finally:
        store.close()


def test_assembler_rebuilds_plan_when_plan_json_absent(tmp_path: Path) -> None:
    """Pas de plan_json en base : la passe plan est appelée, le résultat
    persisté, puis les chapitres rédigés."""
    config, store, livre_id, _vids = _assembler_setup(tmp_path, n_videos=4, n_chapters=1)
    try:
        # On supprime le plan posé par _assembler_setup pour simuler un premier run.
        store.set_livre_plan(livre_id, "")
        invoker = _TrackingInvoker()
        assembler = BookAssembler(config, store, summarizer=RecordingSummarizer())
        assembler.set_invoker(invoker)
        assembler.build(livre_id)
        # Un appel plan au moins, timeout plan = 600 s par défaut.
        plan_calls = [c for c in invoker.calls if c[1]]
        assert len(plan_calls) == 1
        assert plan_calls[0][2] == 600.0
        # Plan désormais persisté en base.
        row = store.get_livre(livre_id)
        assert row is not None and row["plan_json"]
    finally:
        store.close()


def test_assembler_default_uses_summarizer_raw_call_in_prod(tmp_path: Path) -> None:
    """Sans invoker injecté, BookAssembler appelle `summarizer.raw_call(...)` du
    backend courant. Vérification : la méthode est bien utilisée et reçoit la
    signature 4-ary (system, user, schema, timeout)."""
    config, store, livre_id, _vids = _assembler_setup(tmp_path, n_videos=2, n_chapters=1)
    try:
        store.set_livre_plan(livre_id, "")  # force la passe plan.
        received: list[tuple[str, str, bool, float]] = []

        class RawSummarizer:
            def summarize(self, *_a: Any, **_k: Any) -> Summary:
                raise AssertionError("summarize ne doit pas être appelé ici")

            def raw_call(
                self,
                system_prompt: str,
                user_prompt: str,
                json_schema: dict[str, Any] | None,
                timeout_s: float,
            ) -> str:
                is_plan = json_schema is not None
                received.append((system_prompt[:20], user_prompt[:20], is_plan, timeout_s))
                if is_plan:
                    return json.dumps(
                        {
                            "titre": "Livre",
                            "introduction": "intro",
                            "chapitres": [
                                {
                                    "titre": "Chapitre 1",
                                    "fil_conducteur": "f",
                                    "video_ids": [],
                                }
                            ],
                            "conclusion": "",
                        }
                    )
                return "## Section\n\nContenu."

        assembler = BookAssembler(config, store, summarizer=RawSummarizer())
        output = assembler.build(livre_id)
        assert output.livre_md.exists()
        # Un appel plan (schema=PLAN_SCHEMA) + un appel chapitre.
        assert any(is_plan for *_, is_plan, _t in received)
        assert any(not is_plan for *_, is_plan, _t in received)
        # Les timeouts configurés passent bien.
        timeouts = sorted({timeout for *_, _is_plan, timeout in received})
        assert 600.0 in timeouts and 900.0 in timeouts
    finally:
        store.close()
