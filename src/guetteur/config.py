"""Chargement de la configuration (config.toml) et des secrets (variables d'environnement)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from guetteur.models import DETAIL_LEVELS, DetailLevel

NotifyChannel = Literal["telegram", "whatsapp"]
_CHANNELS: tuple[NotifyChannel, ...] = ("telegram", "whatsapp")
SummarizeProvider = Literal["claude_code", "claude_api"]
_PROVIDERS: tuple[SummarizeProvider, ...] = ("claude_code", "claude_api")

DEFAULT_POLL_INTERVAL = 300
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_VIDEOS_PER_CYCLE = 3


class ConfigError(ValueError):
    """Configuration invalide ou incomplète."""


@dataclass(frozen=True)
class PlaylistConfig:
    id: str
    label: str
    language: str = "fr"
    notify: NotifyChannel = "telegram"
    private: bool = False
    # Niveau de détail du résumé (surchargeable en CLI via --detail).
    detail: DetailLevel = "standard"


@dataclass(frozen=True)
class TranscriptConfig:
    languages: tuple[str, ...] = ("fr", "en")
    whisper_enabled: bool = False
    whisper_model: str = "small"
    max_retries: int = 3


@dataclass(frozen=True)
class SummarizeConfig:
    provider: SummarizeProvider = "claude_code"
    claude_code_bin: str = "claude"
    timeout_s: float = 180.0


@dataclass(frozen=True)
class NotifyConfig:
    # Canal tenté après un échec définitif sur le canal principal de la playlist.
    fallback: NotifyChannel | None = None
    # Tentatives par canal (erreurs passagères seulement : 5xx, 429, timeout).
    max_attempts: int = 3
    # Attente après la tentative n (la dernière valeur sert au-delà).
    retry_delays_s: tuple[float, ...] = (2.0, 8.0, 30.0)
    # Une vidéo en « sending » depuis plus longtemps est reprise au cycle suivant.
    sending_timeout_min: int = 10


def _default_archive_home() -> Path:
    """Emplacement dédié pour NOTEBOOKLM_HOME (audit §8.4).

    - En production LXC (`/opt/guetteur` existe) : `/opt/guetteur/data/nlm`.
    - Sinon (dev / tests) : `~/.guetteur-nlm` — jamais `~` ni un dossier partagé
      qui risquerait d'être en 0755 ou de contenir d'autres données."""
    if Path("/opt/guetteur").is_dir():
        return Path("/opt/guetteur/data/nlm")
    return Path.home() / ".guetteur-nlm"


@dataclass(frozen=True)
class ArchiveConfig:
    # Archivage de la veille dans Google NotebookLM (extra optionnel notebooklm).
    enabled: bool = False
    notebook_name: str = "Veille YouTube"
    account: str = ""  # compte Google dédié attendu (guetteur doctor vérifie l'égalité)
    home: Path = field(default_factory=_default_archive_home)
    max_sources_per_notebook: int = 45
    # Version épinglée dans l'extra, vérifiée par doctor (voir audit §9).
    pinned_version: str = "0.8.3"


@dataclass(frozen=True)
class TelegramConfig:
    # Bot Telegram interactif : long polling, boutons sous chaque résumé, questions
    # libres via reply ou bouton. Voir docs/README.md § « Bot interactif ».
    interactive: bool = True
    # Timeout du long polling getUpdates, en secondes (max 50 côté Telegram).
    poll_timeout_s: int = 50
    # Fenêtre glissante pour le rate limit des générations par le bot.
    rate_limit_per_hour: int = 10
    # Durée de vie d'un état « en attente de question » posé par le bouton Question.
    question_ttl_min: int = 10
    # Nombre d'échanges Q&A précédents réinjectés dans le contexte pour permettre
    # les relances sur la même vidéo.
    qa_history_size: int = 6


FilenameDate = Literal["publication", "traitement"]


@dataclass(frozen=True)
class ObsidianConfig:
    # Export vers un vault Obsidian (Lot 6). Le vault reçoit une note Markdown par
    # vidéo dans `Veille/Inbox/`, plus des fiches projet dans `Projets/<slug>.md`.
    enabled: bool = False
    # Chemin absolu du vault (contenant `.obsidian/`). Refusé s'il est inclus dans
    # /opt/guetteur/data/nlm ou dans /home/<user> pour ne jamais mélanger les
    # secrets d'auth (audit §8.4).
    path: Path = field(default_factory=lambda: Path("/opt/guetteur/vault"))
    # Sync git après chaque écriture. Le remote peut être injoignable ; dans ce
    # cas GUETTEUR fait un commit local seulement et retentera au prochain export.
    git_sync: bool = True
    git_remote: str = ""
    # Nom du dossier racine de la veille dans le vault (ne pas confondre avec `path`).
    veille_dir: str = "Veille"
    projets_dir: str = "Projets"
    # Date utilisée dans le nom de fichier des notes (finitions Lot 6 §2) :
    # « publication » = date de la vidéo YouTube, « traitement » = date d'export.
    filename_date: FilenameDate = "publication"


@dataclass(frozen=True)
class ApplicabilityConfig:
    # Seconde passe Claude qui score chaque projet chargé face au résumé détaillé.
    enabled: bool = True
    # Timeout de la passe applicabilité (Claude). Plus court qu'un résumé complet.
    timeout_s: float = 120.0
    # Score minimal pour ajouter une entrée dans Projets/<slug>/IDEES.md.
    idea_threshold: int = 2
    # Score minimal pour la ligne « Pertinent pour » du message Telegram.
    mention_threshold: int = 1


@dataclass(frozen=True)
class WhatsAppConfig:
    api_version: str = "v20.0"
    template_name: str = "hello_world"
    template_language: str = "en_US"
    # True si le modèle (template) contient une variable {{1}} dans son corps.
    template_body_param: bool = False


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    wa_token: str = ""
    wa_phone_id: str = ""
    wa_to: str = ""

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            wa_token=os.environ.get("WA_TOKEN", ""),
            wa_phone_id=os.environ.get("WA_PHONE_ID", ""),
            wa_to=os.environ.get("WA_TO", ""),
        )


@dataclass(frozen=True)
class Config:
    playlists: tuple[PlaylistConfig, ...]
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL
    claude_model: str = DEFAULT_MODEL
    max_videos_per_cycle: int = DEFAULT_MAX_VIDEOS_PER_CYCLE
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    transcript: TranscriptConfig = field(default_factory=TranscriptConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    applicability: ApplicabilityConfig = field(default_factory=ApplicabilityConfig)
    secrets: Secrets = field(default_factory=Secrets)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "guetteur.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def client_secret_path(self) -> Path:
        return self.data_dir / "client_secret.json"

    def playlist(self, playlist_id: str) -> PlaylistConfig:
        for p in self.playlists:
            if p.id == playlist_id:
                return p
        raise ConfigError(f"Playlist inconnue dans config.toml : {playlist_id}")


def _positive_int(raw: Any, name: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ConfigError(f"{name} doit être un entier strictement positif (reçu : {raw!r})")
    return raw


def _parse_playlist(raw: dict[str, Any]) -> PlaylistConfig:
    pid = raw.get("id")
    if not isinstance(pid, str) or not pid:
        raise ConfigError("Chaque [[playlists]] doit avoir un champ 'id' non vide")
    notify = raw.get("notify", "telegram")
    if notify not in _CHANNELS:
        raise ConfigError(f"Playlist {pid} : notify doit valoir 'telegram' ou 'whatsapp'")
    detail = raw.get("detail", "standard")
    if detail not in DETAIL_LEVELS:
        raise ConfigError(
            f"Playlist {pid} : detail doit valoir "
            f"{', '.join(repr(d) for d in DETAIL_LEVELS)} (reçu : {detail!r})"
        )
    return PlaylistConfig(
        id=pid,
        label=str(raw.get("label", pid)),
        language=str(raw.get("language", "fr")),
        notify=notify,
        private=bool(raw.get("private", False)),
        detail=detail,
    )


def _parse_summarize(raw: dict[str, Any]) -> SummarizeConfig:
    provider = raw.get("provider", "claude_code")
    if provider not in _PROVIDERS:
        raise ConfigError(
            f"summarize.provider doit valoir 'claude_code' ou 'claude_api' (reçu : {provider!r})"
        )
    timeout = raw.get("timeout_s", 180)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(f"summarize.timeout_s doit être un nombre positif (reçu : {timeout!r})")
    binary = str(raw.get("claude_code_bin", "claude")).strip()
    if not binary:
        raise ConfigError("summarize.claude_code_bin ne peut pas être vide")
    return SummarizeConfig(provider=provider, claude_code_bin=binary, timeout_s=float(timeout))


def _parse_notify(raw: dict[str, Any]) -> NotifyConfig:
    fallback = raw.get("fallback")
    if fallback in ("", "none", None):
        fallback = None
    elif fallback not in _CHANNELS:
        raise ConfigError(
            f"notify.fallback doit valoir 'telegram', 'whatsapp' ou \"\" (reçu : {fallback!r})"
        )
    delays = raw.get("retry_delays_s", [2, 8, 30])
    if (
        not isinstance(delays, list)
        or not delays
        or any(isinstance(d, bool) or not isinstance(d, int | float) or d < 0 for d in delays)
    ):
        raise ConfigError(f"notify.retry_delays_s doit être une liste de durées ≥ 0 : {delays!r}")
    return NotifyConfig(
        fallback=fallback,
        max_attempts=_positive_int(raw.get("max_attempts", 3), "notify.max_attempts"),
        retry_delays_s=tuple(float(d) for d in delays),
        sending_timeout_min=_positive_int(
            raw.get("sending_timeout_min", 10), "notify.sending_timeout_min"
        ),
    )


def _parse_archive(raw: dict[str, Any]) -> ArchiveConfig:
    default = ArchiveConfig()
    home_raw = raw.get("home")
    home = Path(str(home_raw)) if home_raw else default.home
    if home.is_absolute() is False and str(home) not in ("", "."):
        # Un chemin relatif serait résolu depuis le cwd du service — ambigu.
        raise ConfigError(f"archive.home doit être un chemin absolu (reçu : {home!r})")
    account = str(raw.get("account", ""))
    pinned = str(raw.get("pinned_version", default.pinned_version))
    return ArchiveConfig(
        enabled=bool(raw.get("enabled", False)),
        notebook_name=str(raw.get("notebook_name", default.notebook_name)),
        account=account,
        home=home,
        max_sources_per_notebook=_positive_int(
            raw.get("max_sources_per_notebook", default.max_sources_per_notebook),
            "archive.max_sources_per_notebook",
        ),
        pinned_version=pinned,
    )


def _parse_telegram(raw: dict[str, Any]) -> TelegramConfig:
    return TelegramConfig(
        interactive=bool(raw.get("interactive", True)),
        poll_timeout_s=_positive_int(raw.get("poll_timeout_s", 50), "telegram.poll_timeout_s"),
        rate_limit_per_hour=_positive_int(
            raw.get("rate_limit_per_hour", 10), "telegram.rate_limit_per_hour"
        ),
        question_ttl_min=_positive_int(
            raw.get("question_ttl_min", 10), "telegram.question_ttl_min"
        ),
        qa_history_size=_positive_int(raw.get("qa_history_size", 6), "telegram.qa_history_size"),
    )


_FILENAME_DATES: tuple[FilenameDate, ...] = ("publication", "traitement")


def _parse_obsidian(raw: dict[str, Any]) -> ObsidianConfig:
    default = ObsidianConfig()
    path_raw = raw.get("path")
    path = Path(str(path_raw)) if path_raw else default.path
    if not path.is_absolute() and str(path) not in ("", "."):
        raise ConfigError(f"obsidian.path doit être un chemin absolu (reçu : {path!r})")
    # Sécurité : refuser un vault posé dans le home NotebookLM ou dans un dossier
    # système qui contient déjà des identifiants (audit §8).
    for forbidden in ("/opt/guetteur/data/nlm", "/root", "/etc"):
        try:
            path.resolve().relative_to(Path(forbidden))
        except (ValueError, OSError):
            continue
        raise ConfigError(
            f"obsidian.path ({path}) est sous {forbidden}, chemin interdit "
            "(risque de mélange avec les secrets)."
        )
    filename_date_raw = str(raw.get("filename_date", default.filename_date))
    if filename_date_raw not in _FILENAME_DATES:
        raise ConfigError(
            f"obsidian.filename_date doit valoir 'publication' ou 'traitement' "
            f"(reçu : {filename_date_raw!r})"
        )
    # `filename_date_raw in _FILENAME_DATES` a rétréci le type ci-dessus, mais mypy
    # ne le propage pas depuis une comparaison à un tuple ; assign narrow direct.
    filename_date: FilenameDate = (
        "traitement" if filename_date_raw == "traitement" else "publication"
    )
    return ObsidianConfig(
        enabled=bool(raw.get("enabled", False)),
        path=path,
        git_sync=bool(raw.get("git_sync", default.git_sync)),
        git_remote=str(raw.get("git_remote", "")),
        veille_dir=str(raw.get("veille_dir", default.veille_dir)),
        projets_dir=str(raw.get("projets_dir", default.projets_dir)),
        filename_date=filename_date,
    )


def _parse_applicability(raw: dict[str, Any]) -> ApplicabilityConfig:
    default = ApplicabilityConfig()
    timeout = raw.get("timeout_s", default.timeout_s)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(
            f"applicability.timeout_s doit être un nombre positif (reçu : {timeout!r})"
        )
    return ApplicabilityConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        timeout_s=float(timeout),
        idea_threshold=_positive_int(
            raw.get("idea_threshold", default.idea_threshold), "applicability.idea_threshold"
        ),
        mention_threshold=_positive_int(
            raw.get("mention_threshold", default.mention_threshold),
            "applicability.mention_threshold",
        ),
    )


_SECTIONS = frozenset(
    {
        "general",
        "transcript",
        "summarize",
        "notify",
        "whatsapp",
        "telegram",
        "archive",
        "obsidian",
        "applicability",
        "playlists",
    }
)


def parse_config(data: dict[str, Any], secrets: Secrets | None = None) -> Config:
    unknown = sorted(set(data) - _SECTIONS)
    if unknown:
        hint = " (en-tête [[playlists]] oublié ou commenté ?)" if "id" in unknown else ""
        raise ConfigError(f"Clés inconnues à la racine de config.toml : {', '.join(unknown)}{hint}")
    general: dict[str, Any] = data.get("general", {})
    tr: dict[str, Any] = data.get("transcript", {})
    wa: dict[str, Any] = data.get("whatsapp", {})
    playlists = tuple(_parse_playlist(p) for p in data.get("playlists", []))
    ids = [p.id for p in playlists]
    if len(ids) != len(set(ids)):
        raise ConfigError("Identifiants de playlist en double dans config.toml")

    return Config(
        playlists=playlists,
        poll_interval_seconds=_positive_int(
            general.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL), "poll_interval_seconds"
        ),
        claude_model=str(general.get("claude_model", DEFAULT_MODEL)),
        max_videos_per_cycle=_positive_int(
            general.get("max_videos_per_cycle", DEFAULT_MAX_VIDEOS_PER_CYCLE),
            "max_videos_per_cycle",
        ),
        data_dir=Path(str(general.get("data_dir", "data"))),
        log_level=str(general.get("log_level", "INFO")).upper(),
        transcript=TranscriptConfig(
            languages=tuple(str(x) for x in tr.get("languages", ["fr", "en"])),
            whisper_enabled=bool(tr.get("whisper_enabled", False)),
            whisper_model=str(tr.get("whisper_model", "small")),
            max_retries=_positive_int(tr.get("max_retries", 3), "max_retries"),
        ),
        summarize=_parse_summarize(data.get("summarize", {})),
        notify=_parse_notify(data.get("notify", {})),
        whatsapp=WhatsAppConfig(
            api_version=str(wa.get("api_version", "v20.0")),
            template_name=str(wa.get("template_name", "hello_world")),
            template_language=str(wa.get("template_language", "en_US")),
            template_body_param=bool(wa.get("template_body_param", False)),
        ),
        telegram=_parse_telegram(data.get("telegram", {})),
        archive=_parse_archive(data.get("archive", {})),
        obsidian=_parse_obsidian(data.get("obsidian", {})),
        applicability=_parse_applicability(data.get("applicability", {})),
        secrets=secrets if secrets is not None else Secrets.from_env(),
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_config(data)
