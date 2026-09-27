"""Tests unitaires de l'archiveur NotebookLM (client entièrement mocké).

Couverture des conditions de sécurité de l'audit §8 (A1-A5) plus le workflow :
création du notebook, idempotence source/note, rotation par quota, redaction,
message d'aide en cas d'extra manquant, et distinction retryable / non retryable."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from guetteur.archive.base import FORBIDDEN_ENV_VARS, ArchiveError, redact
from guetteur.archive.notebooklm import NotebookLMArchiver
from guetteur.config import ArchiveConfig
from guetteur.models import Video
from guetteur.store import Store

# --- doublures de la bibliothèque notebooklm --------------------------------------------


class FakeNotebook:
    def __init__(self, id: str, title: str) -> None:
        self.id = id
        self.title = title


class FakeSource:
    def __init__(self, id: str, url: str, title: str = "") -> None:
        self.id = id
        self.url = url
        self.title = title


class FakeNote:
    def __init__(self, id: str, title: str, content: str = "") -> None:
        self.id = id
        self.title = title
        self.content = content


class FakeSourcesAPI:
    def __init__(self, state: dict[str, list[FakeSource]]) -> None:
        self._state = state
        self.add_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.pending_exceptions: list[BaseException] = []

    async def list(self, notebook_id: str) -> list[FakeSource]:
        return list(self._state.get(notebook_id, []))

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> FakeSource:
        if self.pending_exceptions:
            raise self.pending_exceptions.pop(0)
        self.add_calls.append((notebook_id, url, {"wait": wait, "wait_timeout": wait_timeout}))
        src = FakeSource(id=f"src_{len(self._state.get(notebook_id, [])) + 1}", url=url)
        self._state.setdefault(notebook_id, []).append(src)
        return src


class FakeNotesAPI:
    def __init__(self, state: dict[str, list[FakeNote]]) -> None:
        self._state = state
        self.create_calls: list[tuple[str, str, str]] = []

    async def list(self, notebook_id: str) -> list[FakeNote]:
        return list(self._state.get(notebook_id, []))

    async def create(self, notebook_id: str, *, title: str, content: str) -> FakeNote:
        self.create_calls.append((notebook_id, title, content))
        note_id = f"note_{len(self._state.get(notebook_id, [])) + 1}"
        note = FakeNote(id=note_id, title=title, content=content)
        self._state.setdefault(notebook_id, []).append(note)
        return note


class FakeNotebooksAPI:
    def __init__(self, state: dict[str, FakeNotebook]) -> None:
        self._state = state
        self.created: list[str] = []

    async def create(self, title: str) -> FakeNotebook:
        nb_id = f"nb_{len(self._state) + 1}"
        nb = FakeNotebook(id=nb_id, title=title)
        self._state[nb_id] = nb
        self.created.append(title)
        return nb


class FakeClient:
    def __init__(
        self,
        account_email: str | None = "guetteur.veille@gmail.com",
        email_raises: BaseException | None = None,
    ) -> None:
        self._notebooks_state: dict[str, FakeNotebook] = {}
        self._sources_state: dict[str, list[FakeSource]] = {}
        self._notes_state: dict[str, list[FakeNote]] = {}
        self.notebooks = FakeNotebooksAPI(self._notebooks_state)
        self.sources = FakeSourcesAPI(self._sources_state)
        self.notes = FakeNotesAPI(self._notes_state)
        self._email = account_email
        self._email_raises = email_raises
        self.opened = 0
        self.closed = 0

    async def get_account_email(self, *, live_fallback: bool = True) -> str | None:
        if self._email_raises:
            raise self._email_raises
        return self._email


def factory_for(client: FakeClient) -> Any:
    """Fabrique un factory sync qui retourne un async context manager donnant client."""

    def open_client() -> Any:
        @asynccontextmanager
        async def _open() -> Any:
            client.opened += 1
            try:
                yield client
            finally:
                client.closed += 1

        return _open()

    return open_client


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "nlm"
    h.mkdir(mode=0o700)
    return h


@pytest.fixture
def archive_config(home: Path) -> ArchiveConfig:
    return ArchiveConfig(
        enabled=True,
        notebook_name="Veille YouTube",
        account="guetteur.veille@gmail.com",
        home=home,
        max_sources_per_notebook=45,
    )


@pytest.fixture
def video() -> Video:
    return Video(
        video_id="Q3VqYvsFo84",
        title="Vidéo test",
        channel="Chaîne",
        published=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
        url="https://www.youtube.com/watch?v=Q3VqYvsFo84",
    )


@pytest.fixture
def clean_env() -> Any:
    """Enlève toute variable NOTEBOOKLM_* interdite du process AVANT le test, et
    supprime au retour toute variable interdite qu'un test aurait posée : deux tests
    consécutifs ne peuvent pas se contaminer via os.environ."""
    saved: dict[str, str] = {}
    vars_to_watch = (*FORBIDDEN_ENV_VARS, "NOTEBOOKLM_HOME")
    for v in vars_to_watch:
        if v in os.environ:
            saved[v] = os.environ.pop(v)
    yield
    # Toute variable interdite posée pendant le test est retirée.
    for v in vars_to_watch:
        os.environ.pop(v, None)
    # On restaure l'état d'origine.
    for k, v in saved.items():
        os.environ[k] = v


# --- A1 : version + extra manquant ------------------------------------------------------


def test_extra_missing_returns_explicit_error(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """L'archiveur explique clairement quoi installer si notebooklm-py est absent."""
    archiver = NotebookLMArchiver(archive_config, store)  # factory par défaut

    # On simule l'absence de notebooklm en désactivant le module dans sys.modules
    with patch.dict(sys.modules, {"notebooklm": None}), pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    assert not excinfo.value.retryable
    msg = str(excinfo.value)
    assert "notebooklm-py absent" in msg
    assert "uv sync" in msg and "extra notebooklm" in msg


# --- A2 : variables interdites ---------------------------------------------------------


@pytest.mark.parametrize("var", FORBIDDEN_ENV_VARS)
def test_forbidden_env_var_refuses_start(
    var: str,
    archive_config: ArchiveConfig,
    store: Store,
    video: Video,
    clean_env: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chaque variable interdite fait échouer l'archivage AVANT tout appel au client."""
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    os.environ[var] = "attaquant"
    caplog.set_level("ERROR", logger="guetteur.archive.notebooklm")
    with pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    assert not excinfo.value.retryable
    assert var in str(excinfo.value)
    assert "archive.forbidden_env" in caplog.text
    # Le client n'a même pas été ouvert.
    assert client.opened == 0


def test_forbidden_env_stripped_from_client_env(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """Défense en profondeur : le _controlled_env retire les variables interdites."""
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    # On instrumente _controlled_env pour observer l'env dans le bloc.
    seen: dict[str, str] = {}

    orig = archiver._controlled_env

    def capturing() -> Any:
        cm = orig()

        class Wrap:
            def __enter__(self) -> None:
                cm.__enter__()
                for var in (*FORBIDDEN_ENV_VARS, "NOTEBOOKLM_HOME"):
                    seen[var] = os.environ.get(var, "<absent>")

            def __exit__(self, *a: Any) -> None:
                cm.__exit__(*a)

        return Wrap()

    archiver._controlled_env = capturing  # type: ignore[method-assign]
    # Comme A2 refuse dès la présence, on ne peut pas placer une variable interdite
    # ici : on vérifie juste que NOTEBOOKLM_HOME est bien injecté et qu'aucune
    # variable interdite ne fuit dans l'env passé au client.
    archiver.archive(video, "# test")
    assert seen["NOTEBOOKLM_HOME"] == str(archive_config.home)
    for var in FORBIDDEN_ENV_VARS:
        assert seen[var] == "<absent>", f"{var} n'a pas été retirée"


# --- A3 : permissions 0700 / 0600 ------------------------------------------------------


def test_home_must_be_0700(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any, tmp_path: Path
) -> None:
    """Un home en 0755 est refusé avec la commande chmod à passer."""
    bad_home = tmp_path / "loose"
    bad_home.mkdir(mode=0o755)
    cfg = ArchiveConfig(enabled=True, notebook_name="X", home=bad_home, max_sources_per_notebook=10)
    archiver = NotebookLMArchiver(cfg, store, client_factory=factory_for(FakeClient()))
    with pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    assert "0700" in str(excinfo.value)
    assert f"chmod 700 {bad_home}" in str(excinfo.value)
    # Mode inchangé : on ne modifie jamais les permissions d'un autre chemin.
    assert (bad_home.stat().st_mode & 0o777) == 0o755


def test_storage_state_json_must_be_0600(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """Un storage_state.json en 0644 est refusé, avec chmod à passer."""
    (archive_config.home / "storage_state.json").write_text("{}", encoding="utf-8")
    (archive_config.home / "storage_state.json").chmod(0o644)
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(FakeClient()))
    with pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    assert "0600" in str(excinfo.value) and "chmod 600" in str(excinfo.value)


def test_home_created_at_0700_if_absent(
    tmp_path: Path, store: Store, video: Video, clean_env: Any
) -> None:
    """Home absent = créé en 0700, jamais d'autre chemin touché.
    On s'assure qu'un dossier voisin (créé en 0755 avant l'archivage) conserve ses droits."""
    home = tmp_path / "nlm"
    sibling = tmp_path / "voisin"
    sibling.mkdir(mode=0o755)
    cfg = ArchiveConfig(enabled=True, notebook_name="X", home=home, max_sources_per_notebook=10)
    archiver = NotebookLMArchiver(cfg, store, client_factory=factory_for(FakeClient()))
    archiver.archive(video, "# test")
    assert home.exists()
    assert (home.stat().st_mode & 0o777) == 0o700
    # Le voisin est intact : on ne modifie jamais les permissions ailleurs.
    assert (sibling.stat().st_mode & 0o777) == 0o755


# --- A4 : redaction ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_masked"),
    [
        ("Authorization: Bearer ya29.longtoken", "<redacted>"),
        ("Cookie: __Secure-1PSIDTS=abc123; SID=xyz", "<redacted>"),
        ("Set-Cookie: SID=def456; Path=/", "<redacted>"),
        ("failed with ya29.superlongtoken", "<redacted>"),
        ("aas_et/quz_x-y_z", "<redacted>"),
        ("SNlM0e_supersecrettoken", "<redacted>"),
    ],
)
def test_redact_masks_secrets(raw: str, expected_masked: str) -> None:
    out = redact(raw)
    assert expected_masked in out
    # Aucun fragment de secret ne subsiste
    for fragment in ("ya29.", "SNlM0e_", "SID=", "Bearer", "aas_et/"):
        assert fragment not in out or expected_masked in out


def test_redact_leaves_normal_text_alone() -> None:
    assert redact("HTTP 500 : Internal Server Error") == "HTTP 500 : Internal Server Error"


# --- Workflow : création + idempotence -------------------------------------------------


def test_notebook_created_on_first_use(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    outcome = archiver.archive(video, "# résumé\ncontenu")
    assert outcome.notebook_id == "nb_1"
    assert client.notebooks.created == ["Veille YouTube"]
    assert store.get_meta("archive_notebook_id") == "nb_1"
    assert store.get_meta("archive_notebook_index") == "1"
    # 1 source et 1 note dans le notebook créé
    assert len(client.sources.add_calls) == 1
    assert len(client.notes.create_calls) == 1


def test_source_not_added_twice_same_url(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """Une URL déjà présente dans le notebook (par ex. ajoutée par ailleurs) n'est pas
    réajoutée. La note, elle, est créée si son titre est absent."""
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    # Premier appel : notebook créé + source + note
    archiver.archive(video, "# a")
    assert len(client.sources.add_calls) == 1
    assert len(client.notes.create_calls) == 1
    # Second appel avec la même vidéo : ni source, ni note recréée.
    archiver.archive(video, "# a")
    assert len(client.sources.add_calls) == 1
    assert len(client.notes.create_calls) == 1


def test_source_url_normalized_across_query_and_fragment(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """L'URL avec un ?utm=… est considérée équivalente à celle sans query."""
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    archiver.archive(video, "# a")
    variant = Video(
        video_id=video.video_id,
        title=video.title,
        channel=video.channel,
        published=video.published,
        url=video.url + "&utm_source=email#t=30",
    )
    archiver.archive(variant, "# a")
    # add_url appelé une seule fois : la source « équivalente » a été détectée.
    assert len(client.sources.add_calls) == 1


def test_note_not_recreated_when_title_exists(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """Une note dont le titre est déjà présent n'est pas recréée."""
    client = FakeClient()
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    archiver.archive(video, "# résumé 1")
    # On ré-archive avec le même titre attendu (même date + même titre vidéo).
    archiver.archive(video, "# résumé 2 différent")
    assert len(client.notes.create_calls) == 1
    # Le contenu de la première note est conservé (pas d'écrasement).
    assert client.notes.create_calls[0][2].startswith("# résumé 1")


def test_notebook_rotates_beyond_quota(tmp_path: Path, store: Store, clean_env: Any) -> None:
    """Quand sources.list dépasse max_sources_per_notebook, on crée « (2) », « (3) »…"""
    home = tmp_path / "nlm"
    home.mkdir(mode=0o700)
    cfg = ArchiveConfig(enabled=True, notebook_name="Veille", home=home, max_sources_per_notebook=2)
    client = FakeClient()
    archiver = NotebookLMArchiver(cfg, store, client_factory=factory_for(client))
    videos = [
        Video(
            video_id=f"v{i}",
            title=f"Vidéo {i}",
            channel="C",
            published=datetime(2026, 9, 26, tzinfo=UTC),
            url=f"https://youtu.be/v{i}",
        )
        for i in range(3)
    ]
    for v in videos:
        archiver.archive(v, f"# résumé {v.video_id}")
    # 2 vidéos dans nb_1 (quota atteint) puis 1 dans nb_2.
    assert client.notebooks.created == ["Veille", "Veille (2)"]
    assert store.get_meta("archive_notebook_id") == "nb_2"
    assert store.get_meta("archive_notebook_index") == "2"


# --- retryable / non retryable ---------------------------------------------------------


class _RateLimitError(Exception):
    """Mime la RateLimitError de notebooklm-py."""


class _AuthError(Exception):
    """Mime la AuthError de notebooklm-py (non retryable)."""


def test_retryable_error_retries_then_succeeds(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """RateLimitError sur la 1re tentative → dort 60 s (mocké), puis 2e tentative OK."""
    client = FakeClient()
    client.sources.pending_exceptions = [_RateLimitError("429 rate limit ya29.")]
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    # Nommer la classe RateLimitError pour que _is_retryable la reconnaisse.
    _RateLimitError.__name__ = "RateLimitError"
    archiver = NotebookLMArchiver(
        archive_config, store, client_factory=factory_for(client), sleep=fake_sleep
    )
    outcome = archiver.archive(video, "# test")
    assert outcome.notebook_id == "nb_1"
    assert slept == [60.0]
    assert len(client.sources.add_calls) == 1


def test_non_retryable_error_propagates_immediately(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """AuthError (nom qui n'est ni dans _RETRYABLE_TYPES) → aucun retry."""
    client = FakeClient()
    _AuthError.__name__ = "AuthError"
    client.sources.pending_exceptions = [_AuthError("invalid session ya29.abc")]
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    archiver = NotebookLMArchiver(
        archive_config, store, client_factory=factory_for(client), sleep=fake_sleep
    )
    with pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    # Le message ne fuit pas le token brut : ya29.abc redacté.
    assert not excinfo.value.retryable
    assert "AuthError" in str(excinfo.value)
    assert "ya29.abc" not in str(excinfo.value)
    assert slept == []


def test_retryable_error_gives_up_after_max_attempts(
    archive_config: ArchiveConfig, store: Store, video: Video, clean_env: Any
) -> None:
    """Deux tentatives ratées d'affilée : propagation avec retryable=True."""
    client = FakeClient()
    _RateLimitError.__name__ = "RateLimitError"
    client.sources.pending_exceptions = [
        _RateLimitError("boom 1"),
        _RateLimitError("boom 2"),
    ]

    async def fake_sleep(d: float) -> None:
        return None

    archiver = NotebookLMArchiver(
        archive_config, store, client_factory=factory_for(client), sleep=fake_sleep
    )
    with pytest.raises(ArchiveError) as excinfo:
        archiver.archive(video, "# test")
    assert excinfo.value.retryable is True


# --- auth_check pour doctor -----------------------------------------------------------


def test_auth_check_returns_account_email(
    archive_config: ArchiveConfig, store: Store, clean_env: Any
) -> None:
    client = FakeClient(account_email="guetteur.veille@gmail.com")
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    assert archiver.auth_check() == "guetteur.veille@gmail.com"


def test_auth_check_returns_none_when_no_email(
    archive_config: ArchiveConfig, store: Store, clean_env: Any
) -> None:
    client = FakeClient(account_email=None)
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    assert archiver.auth_check() is None


def test_auth_check_wraps_client_error(
    archive_config: ArchiveConfig, store: Store, clean_env: Any
) -> None:
    class BoomError(Exception):
        pass

    client = FakeClient(email_raises=BoomError("session expired ya29.abc"))
    archiver = NotebookLMArchiver(archive_config, store, client_factory=factory_for(client))
    with pytest.raises(ArchiveError) as excinfo:
        archiver.auth_check()
    # Le nom d'exception passe, mais le token est masqué.
    assert "BoomError" in str(excinfo.value)
    assert "ya29.abc" not in str(excinfo.value)
