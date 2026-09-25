"""Chargement de la configuration (config.toml) et des secrets (variables d'environnement)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
    return PlaylistConfig(
        id=pid,
        label=str(raw.get("label", pid)),
        language=str(raw.get("language", "fr")),
        notify=notify,
        private=bool(raw.get("private", False)),
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


_SECTIONS = frozenset({"general", "transcript", "summarize", "notify", "whatsapp", "playlists"})


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
        secrets=secrets if secrets is not None else Secrets.from_env(),
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_config(data)
