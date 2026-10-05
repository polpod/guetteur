"""E2E Lot 7 : chaîne mockée de 12 vidéos, job complet, Markdown assemblé
avec 3 chapitres, EPUB produit (pandoc mocké), envoi Telegram simulé."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from guetteur.config import (
    ApplicabilityConfig,
    LivreConfig,
    ObsidianConfig,
    PlaylistConfig,
)
from guetteur.jobs.livre import (
    BookAssembler,
    LivreRunner,
    finalize,
    persist_new_livre,
)
from guetteur.jobs.livre import (
    plan_book as job_plan_book,
)
from guetteur.models import KeyPoint, Segment, Summary, Transcript, Video
from guetteur.sources.channel import ChannelFilters, ChannelInfo
from guetteur.store import Store
from guetteur.summarize.base import Summarizer, SummaryMeta
from tests.helpers import make_config


class FakeChannelResolver:
    def resolve(self, url_or_handle: str) -> ChannelInfo:
        cid = "UC" + "z" * 22
        return ChannelInfo(
            channel_id=cid,
            uploads_playlist_id="UU" + "z" * 22,
            title="Chaîne Test",
            handle="chaine_test",
        )


class FakeChannelLister:
    def __init__(self, n: int = 12) -> None:
        self._n = n

    def list_videos(
        self, channel: ChannelInfo, filters: ChannelFilters
    ) -> list[tuple[Video, int | None, int | None]]:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        vids: list[tuple[Video, int | None, int | None]] = []
        for i in range(self._n):
            vid = f"vid{i:08d}xy"[:11]
            v = Video(
                video_id=vid,
                title=f"Vidéo {i} — sujet {'agents' if i < 4 else 'python' if i < 8 else 'infra'}",
                channel="Chaîne Test",
                published=base + timedelta(days=i),
                url=f"https://youtu.be/{vid}",
            )
            vids.append((v, 1200, 10_000 + i * 100))
        return vids


class FakeTranscriber:
    def get(self, video_id: str) -> Transcript:
        return Transcript(
            video_id, "fr", "youtube", (Segment(0.0, f"Contenu de {video_id}."),)
        )


class RawCallSummarizer(Summarizer):
    """Résume les vidéos ET répond aux passes plan / chapitre via `raw_call`
    (signature 4-ary alignée sur les backends claude_code / claude_api)."""

    def __init__(self, plan_json: str, chapter_md: str) -> None:
        self.summary_calls: list[str] = []
        self.raw_calls: list[tuple[str, str, bool, float]] = []
        self._plan_json = plan_json
        self._chapter_md = chapter_md

    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary:
        self.summary_calls.append(transcript.video_id)
        # Contenu enrichi : mots-clefs plausibles pour que le glossaire pêche « MCP ».
        return Summary(
            title=f"Résumé {transcript.video_id}",
            tldr=f"TL;DR de {transcript.video_id}. MCP est central ici.",
            key_points=(KeyPoint(0, "Point A MCP"), KeyPoint(60, "Point B agents")),
            why_it_matters="Contexte : MCP et agents Python.",
            reading_time_minutes=1,
            detail=meta.detail,
        )

    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str:
        is_plan = json_schema is not None
        self.raw_calls.append((system_prompt[:40], user_prompt[:40], is_plan, timeout_s))
        return self._plan_json if is_plan else self._chapter_md


def _fake_plan_json(video_ids: list[str]) -> str:
    """Plan JSON qui répartit 12 vidéos en 3 chapitres cohérents."""
    return json.dumps(
        {
            "titre": "Livre Test",
            "introduction": "Introduction courte du livre test.",
            "chapitres": [
                {
                    "titre": "Chapitre 1 — Agents",
                    "fil_conducteur": "L'ère des agents autonomes.",
                    "video_ids": video_ids[:4],
                },
                {
                    "titre": "Chapitre 2 — Python",
                    "fil_conducteur": "Outillage Python et MCP.",
                    "video_ids": video_ids[4:8],
                },
                {
                    "titre": "Chapitre 3 — Infra",
                    "fil_conducteur": "Infrastructure pour agents.",
                    "video_ids": video_ids[8:],
                },
            ],
            "conclusion": "Conclusion courte.",
        }
    )


def _fake_chapter_md() -> str:
    return (
        "## Sous-section 1\n\nContenu narratif qui mentionne MCP à plusieurs reprises.\n\n"
        "## Sous-section 2\n\nSuite du raisonnement — MCP encore ici.\n"
    )


def test_livre_flow_full_pipeline_to_epub(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    obs = ObsidianConfig(enabled=True, path=vault, git_sync=False, git_remote="")
    livre_cfg = LivreConfig(pause_between_videos_s=0.0, progress_every=100, notebooklm=False)
    config = make_config(
        tmp_path,
        obsidian=obs,
        applicability=ApplicabilityConfig(enabled=False),
        livre=livre_cfg,
        playlists=(PlaylistConfig(id="PLtest", label="Veille"),),
    )
    store = Store(tmp_path / "db.sqlite")
    try:
        # 1. Plan (résolution chaîne + listing filtré, sans réseau).
        plan = job_plan_book(
            config,
            "https://youtube.com/@chaine_test",
            title="Livre Test",
            filters=ChannelFilters(max_videos=12),
            detail="standard",
            resolver=FakeChannelResolver(),
            lister=FakeChannelLister(n=12),
        )
        assert len(plan.videos) == 12
        # 2. Persistance : le job apparaît en base.
        livre_id = persist_new_livre(store, plan, "https://youtube.com/@chaine_test")
        assert livre_id > 0
        # 3. Résumés de toutes les vidéos.
        vids = [v.video_id for v, _d, _vc in plan.videos]
        summarizer = RawCallSummarizer(
            plan_json=_fake_plan_json(vids), chapter_md=_fake_chapter_md()
        )
        runner = LivreRunner(
            config=config,
            store=store,
            transcriber=FakeTranscriber(),
            summarizer=summarizer,
            sleep=lambda _s: None,
        )
        assert runner.run(livre_id) == "summarized"
        assert len(summarizer.summary_calls) == 12
        assert store.livre_progress(livre_id).get("summarized") == 12
        # 4. Passe plan + chapitres.
        assembler = BookAssembler(config, store, summarizer)
        output = assembler.build(livre_id)
        assert output.livre_md.exists()
        text = output.livre_md.read_text(encoding="utf-8")
        assert "Livre Test" in text
        assert "Chapitre 1 — Agents" in text
        assert "MCP" in text
        # 3 chapitres → 3 fichiers dans chapitres/.
        assert len(output.chapter_files) == 3
        for path in output.chapter_files:
            assert path.exists()
        # 5. Sortie Pandoc mockée : EPUB OK. PDF None car weasyprint absent dans
        #    l'env de test (extra `[pdf]` non installé).
        def fake_pandoc(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"EPUB fictif")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        final = finalize(output, title="Livre Test", pandoc_runner=fake_pandoc)
        assert final.epub is not None and final.epub.exists()
        assert final.pdf is None
        store.set_livre_status(livre_id, "done", finished=True)
        row = store.get_livre(livre_id)
        assert row is not None and row["status"] == "done"
        # 6. Un « message Telegram final » simulé serait `send_document(final.epub)`.
        #    On vérifie ici juste que le fichier attendu est bien un EPUB non vide.
        assert final.epub.read_bytes().startswith(b"EPUB")
    finally:
        store.close()


def test_publish_book_commits_in_bare_remote_and_sends_telegram(tmp_path: Path) -> None:
    """Après la phase rendering, `publish_book` doit : (a) poser un commit
    « GUETTEUR : livre … » dans le vault et le pousser vers le remote bare,
    (b) envoyer `livre.epub` via sendDocument avec le chemin vault en caption."""
    import httpx

    from guetteur.export.livre_output import BookOutput
    from guetteur.jobs.livre import publish_book
    from guetteur.notify.telegram_bot import TelegramApi

    # Dépôt bare qui joue le rôle de GitHub, branche initiale `main`.
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(remote)], check=True
    )
    # Un commit initial pour que `git ls-remote` renvoie une HEAD (sinon
    # `_git_sync` passe à côté du pull).
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", "-q", str(seed)], check=True)
    for cfg in (
        ["user.email", "guetteur@test"],
        ["user.name", "Test"],
        ["commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(seed), "config", *cfg], check=True)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "-q", str(remote), "main"], check=True
    )

    # Vault clone du bare (comme en prod sur le LXC).
    vault = tmp_path / "vault"
    subprocess.run(["git", "clone", "-q", str(remote), str(vault)], check=True)
    for cfg in (
        ["user.email", "guetteur@test"],
        ["user.name", "GUETTEUR"],
        ["commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(vault), "config", *cfg], check=True)

    livres_root = vault / "Livres" / "chaine-test"
    (livres_root / "chapitres").mkdir(parents=True)
    livre_md = livres_root / "livre.md"
    livre_md.write_text("# Livre Test\n\nContenu.\n", encoding="utf-8")
    epub_path = livres_root / "livre.epub"
    epub_path.write_bytes(b"EPUB fictif")
    output = BookOutput(
        root=livres_root,
        livre_md=livre_md,
        index_md=livres_root / "_index.md",
        chapter_files=(),
        epub=epub_path,
        pdf=None,
    )

    config = make_config(
        tmp_path,
        obsidian=ObsidianConfig(
            enabled=True, path=vault, git_sync=True, git_remote=str(remote)
        ),
    )

    # Faux Telegram : capture sendDocument.
    doc_calls: list[tuple[str, bytes]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendDocument")
        doc_calls.append((request.url.path, request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(telegram_handler))
    bot = TelegramApi("TOKEN", client=client)

    status = publish_book(output, title="Livre Test", config=config, bot=bot, chat_id="42")

    assert status["git"] == "push_ok", status
    assert status["telegram"] == "sent", status
    # Le dépôt bare contient maintenant un commit « GUETTEUR : livre … ».
    log_proc = subprocess.run(
        ["git", "-C", str(remote), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "GUETTEUR : livre Livre Test" in log_proc.stdout
    # `livre.epub` et `livre.md` sont bien committés.
    files_proc = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Livres/chaine-test/livre.md" in files_proc.stdout
    assert "Livres/chaine-test/livre.epub" in files_proc.stdout
    # Un seul appel sendDocument (EPUB — pas de PDF ici).
    assert len(doc_calls) == 1
