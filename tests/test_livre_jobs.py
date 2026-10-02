"""Tests unitaires de jobs/livre.py (Lot 7) : reprise après crash, priorité
veille (verrou partagé), un seul job à la fois, cache-first, pause/cancel."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import LivreConfig
from guetteur.jobs.livre import (
    JobAlreadyRunningError,
    LivrePlan,
    LivreRunner,
    cancel_livre,
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
