"""Bot Telegram interactif du Lot 5 : long polling dans un thread partagé par
`guetteur run`, boutons sous chaque résumé (Bref / Standard / Détaillé / Question),
Q&A avec la transcription complète en contexte, commandes texte /status /retry /reset
/detail /last /help. Le tout dans le même processus que le pipeline, avec un verrou
partagé pour ne jamais avoir deux appels Claude en même temps.

Le bot ne touche jamais directement au notifier de bas niveau : il utilise `TelegramApi`
pour les appels HTTP (getUpdates, sendMessage, answerCallbackQuery) et le pipeline pour
les mutations sur la base."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from guetteur.sources.channel import VideoOrder

import httpx

from guetteur.config import Config, PlaylistConfig
from guetteur.logs import redact
from guetteur.models import DETAIL_LEVELS, DetailLevel, Summary, Video
from guetteur.notify.base import Message, NotifyError
from guetteur.notify.telegram import TelegramNotifier
from guetteur.store import Store
from guetteur.summarize.base import Summarizer, SummaryMeta, summary_from_json, summary_to_json
from guetteur.summarize.format import escape_md_v2

log = logging.getLogger(__name__)

# Clés meta persistées par le bot.
META_OFFSET = "telegram_bot_offset"  # dernier update_id + 1
META_HEARTBEAT = "telegram_bot_heartbeat"  # dernier getUpdates réussi (iso)
META_AWAITING_VIDEO = "telegram_awaiting_video"  # video_id en attente de question
META_AWAITING_UNTIL = "telegram_awaiting_until"  # ISO d'expiration
META_UNKNOWN_LOG_PREFIX = "telegram_unknown_"  # dernier log d'un chat inconnu

_BACKOFF_S = (5.0, 30.0, 60.0)  # backoff progressif après une exception

# Slugs de projet acceptés par `/idees <projet>` : minuscules ASCII + `-`/`_`, ≤ 64.
# Commence obligatoirement par une lettre ou un chiffre (pas de préfixe `-` piégeux,
# pas de `.` menant à un dossier caché ou à `..`). Correspond au format généré par
# `slugify_title` côté export Obsidian.
_VALID_PROJECT_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

# Format d'un video_id YouTube : 11 caractères base64url-safe. Utilisé partout où
# un video_id entre par une commande utilisateur (Lot 7 §1). Toute autre valeur
# est refusée avec un message clair : évite du bruit dans les logs, dans les
# callbacks Telegram, et bloque un futur bug où `video_id` finirait sur un chemin.
_VALID_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# Longueur maximale d'une question Q&A envoyée à Claude (Lot 7 §7). Au-delà, on
# refuse l'envoi : le prompt système exige 1500 caractères de RÉPONSE, aucune
# raison d'accepter une question qui gonfle le contexte au-delà du raisonnable.
QUESTION_MAX_CHARS = 2000

# Longueur maximale d'une commande texte reçue (protège contre les 4096 chars
# possibles côté Telegram — la plupart des commandes tiennent en 50 chars).
COMMAND_MAX_CHARS = 512


# --- API bas niveau Telegram ----------------------------------------------------------


class TelegramApiError(RuntimeError):
    """Erreur au niveau HTTP Telegram (transport, 4xx, 5xx). Le bot les logue et
    applique un backoff avant de retenter le prochain getUpdates."""


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Renvoie une copie du payload où les champs textuels passent par `redact`."""
    redacted: dict[str, Any] = dict(payload)
    for key in ("text", "caption"):
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = redact(value)
    return redacted


def _parse_livre_cmd_args(args: list[str]) -> tuple[str, VideoOrder, int | None]:
    """Parseur minimal pour `/livre <URL> [--order X] [--max N]`.

    Volontairement artisanal (pas d'argparse) : la commande reste
    permissive à l'ordre des flags, lève des `ValueError` lisibles, et
    évite d'exécuter du code depuis le payload Telegram (seules les
    valeurs des choix fermés sont acceptées). Retourne `(url, order, max_videos)`."""
    from guetteur.sources.channel import VIDEO_ORDERS

    order: VideoOrder = "date"
    max_videos: int | None = None
    url: str | None = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--order":
            if i + 1 >= len(args):
                raise ValueError("--order attend une valeur (date|views|duration)")
            value = args[i + 1]
            if value not in VIDEO_ORDERS:
                raise ValueError(
                    f"--order inconnu : {value!r} (attendu date|views|duration)"
                )
            # mypy narrowe `str` → VideoOrder via l'`in VIDEO_ORDERS` (tuple de Literal).
            order = value
            i += 2
        elif tok == "--max":
            if i + 1 >= len(args):
                raise ValueError("--max attend un entier positif")
            try:
                max_videos = int(args[i + 1])
            except ValueError as exc:
                raise ValueError(
                    f"--max : entier attendu, reçu {args[i + 1]!r}"
                ) from exc
            if max_videos <= 0:
                raise ValueError("--max doit être strictement positif")
            i += 2
        elif tok.startswith("--"):
            raise ValueError(f"option inconnue : {tok}")
        else:
            if url is not None:
                raise ValueError("une seule URL de chaîne attendue")
            url = tok
            i += 1
    if url is None:
        raise ValueError("URL de chaîne manquante")
    return url, order, max_videos


class TelegramApi:
    """Client HTTP minimal pour les endpoints utilisés par le bot. Utilise le même
    httpx.Client que le notifier historique quand il est fourni (tests e2e mockent
    un seul transport)."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.Client(timeout=60.0)

    def _post(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        # Dernière barrière : les champs textuels (`text`, `caption`) peuvent
        # porter un secret si le bot relaie un log ou une erreur — on les
        # redacte avant toute requête HTTP. Non destructif en nominal.
        payload = _redact_payload(payload)
        try:
            resp = self._client.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"{method} : {type(exc).__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            raise TelegramApiError(
                f"{method} HTTP {resp.status_code} : {data.get('description') or resp.text[:200]}"
            )
        return data.get("result")

    def get_updates(self, offset: int, timeout_s: int) -> list[dict[str, Any]]:
        # timeout HTTP = timeout_s + 5 pour laisser Telegram fermer proprement.
        result = self._post(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_s,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=float(timeout_s + 5),
        )
        return list(result or [])

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = "MarkdownV2",
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        result = self._post("sendMessage", payload)
        if isinstance(result, dict):
            mid = result.get("message_id")
            return int(mid) if isinstance(mid, int) else None
        return None

    def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]  # Telegram limite l'aperçu à 200 caractères
        self._post("answerCallbackQuery", payload)

    def send_document(
        self, chat_id: str, path: Path, caption: str | None = None
    ) -> int | None:
        """Envoie un fichier local via `sendDocument` (multipart). Utilisé par le
        Lot 7 pour livrer livre.md / livre.epub / livre.pdf en fin de génération.
        Retourne le message_id ou None."""
        if not path.exists():
            raise TelegramApiError(f"sendDocument : fichier absent {path}")
        try:
            with path.open("rb") as fh:
                files = {"document": (path.name, fh)}
                data: dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = redact(caption[:1024])
                resp = self._client.post(
                    f"{self._base}/sendDocument", data=data, files=files, timeout=180.0
                )
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"sendDocument : {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            raise TelegramApiError(
                f"sendDocument HTTP {resp.status_code} : "
                f"{body.get('description') or resp.text[:200]}"
            )
        result = body.get("result")
        if isinstance(result, dict):
            mid = result.get("message_id")
            return int(mid) if isinstance(mid, int) else None
        return None


# --- callback_data ---------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackAction:
    video_id: str
    kind: str  # "detail" | "question" | "keep" | "discard" | "idea"
    detail: DetailLevel | None = None
    project_slug: str | None = None


# Callback_data borné exactement au format YouTube (Lot 7 §6) : 11 chars pour le
# video_id, slug ≤ 32. La longueur totale reste ≤ 64 bytes (limite Telegram).
_CALLBACK_RE = re.compile(
    r"^v:([A-Za-z0-9_\-]{11}):"
    r"(?:"
    r"d:(bref|standard|detaille)"  # niveau détail
    r"|q"  # question
    r"|g"  # garder
    r"|e"  # écarter
    r"|i:([a-z0-9_-]{1,32})"  # idée pour <slug>
    r")$"
)


def parse_callback_data(raw: str) -> CallbackAction | None:
    """Parse tous les callbacks du bot (Lot 5 + 6) :

    - `v:<id>:d:<niveau>` → niveau détail (Lot 5).
    - `v:<id>:q` → question (Lot 5).
    - `v:<id>:g` / `v:<id>:e` → Garder / Écarter (Lot 6).
    - `v:<id>:i:<slug>` → envoyer le méga-prompt d'un projet (Lot 6).

    Retourne None sur un format inconnu (protection contre les callbacks forgés).
    """
    m = _CALLBACK_RE.match(raw or "")
    if not m:
        return None
    video_id = m.group(1)
    level = m.group(2)
    slug = m.group(3)
    if level:
        # La regex garantit level ∈ {"bref", "standard", "detaille"} : cast pour mypy.
        detail = cast("DetailLevel", level)
        return CallbackAction(video_id=video_id, kind="detail", detail=detail)
    if slug:
        return CallbackAction(video_id=video_id, kind="idea", project_slug=slug)
    tail = raw.rsplit(":", 1)[-1]
    return CallbackAction(
        video_id=video_id,
        kind={"q": "question", "g": "keep", "e": "discard"}[tail],
    )


def build_summary_keyboard(
    video_id: str,
    detail_shown: DetailLevel,
    project_slugs: list[str] | None = None,
    include_theme_buttons: bool = True,
) -> dict[str, Any]:
    """Construit les boutons sous un résumé.

    - Ligne 1 : niveaux (le niveau affiché est omis) + Question.
    - Ligne 2 (optionnelle Lot 6) : Garder / Écarter.
    - Lignes suivantes (Lot 6) : un bouton « Idée pour <PROJET> » par projet score ≥ 2.
    """
    labels: dict[DetailLevel, str] = {
        "bref": "Bref",
        "standard": "Standard",
        "detaille": "Détaillé",
    }
    rows: list[list[dict[str, str]]] = []
    level_row: list[dict[str, str]] = []
    for level in DETAIL_LEVELS:
        if level == detail_shown:
            continue
        level_row.append({"text": labels[level], "callback_data": f"v:{video_id}:d:{level}"})
    level_row.append({"text": "Question", "callback_data": f"v:{video_id}:q"})
    rows.append(level_row)
    if include_theme_buttons:
        rows.append(
            [
                {"text": "Garder", "callback_data": f"v:{video_id}:g"},
                {"text": "Écarter", "callback_data": f"v:{video_id}:e"},
            ]
        )
    for slug in project_slugs or []:
        rows.append(
            [
                {
                    "text": f"Idée pour {slug.upper()}",
                    "callback_data": f"v:{video_id}:i:{slug}",
                }
            ]
        )
    return {"inline_keyboard": rows}


# --- rate limit -------------------------------------------------------------------------


class RateLimiter:
    """Fenêtre glissante par utilisateur (chat_id). Le bot n'accepte qu'un unique
    chat, mais on garde une structure par identifiant pour le rendre trivialement
    réutilisable en cas d'extension multi-chat."""

    def __init__(self, per_hour: int, now: Callable[[], datetime] | None = None) -> None:
        self._per_hour = per_hour
        self._events: dict[str, deque[datetime]] = {}
        self._now = now or (lambda: datetime.now(UTC))

    def allow(self, chat_id: str) -> bool:
        now = self._now()
        events = self._events.setdefault(chat_id, deque())
        cutoff = now - timedelta(hours=1)
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= self._per_hour:
            return False
        events.append(now)
        return True

    def remaining(self, chat_id: str) -> int:
        events = self._events.get(chat_id) or deque()
        cutoff = self._now() - timedelta(hours=1)
        while events and events[0] < cutoff:
            events.popleft()
        return max(0, self._per_hour - len(events))


# --- Q&A --------------------------------------------------------------------------------


QuestionAnswerer = Callable[[str, str, list[tuple[str, str]]], str]
"""Signature d'un backend Q&A : (transcript_text, question, history) -> réponse texte."""


# --- helpers de rendu -------------------------------------------------------------------


def _render_video_line(video: Video) -> str:
    """Ligne 'Titre — canal' avec lien vidéo, utilisée en tête des Q&A envoyées."""
    return f"*{escape_md_v2(video.title)}*\n_{escape_md_v2(video.channel)}_"


def _summary_message(
    summary: Summary,
    video: Video,
    playlist_label: str,
    detail: DetailLevel,
) -> Message:
    """Construit un `Message` prêt à être envoyé par TelegramNotifier avec les boutons
    du bot (ligne inline_keyboard portée par `reply_markup`)."""
    from guetteur.pipeline import build_message

    message = build_message(summary, video, playlist_label)
    reply_markup = build_summary_keyboard(video.video_id, detail)
    return Message(
        markdown_v2=message.markdown_v2,
        plain=message.plain,
        short=message.short,
        markdown_v2_parts=message.markdown_v2_parts,
        plain_parts=message.plain_parts,
        reply_markup=reply_markup,
    )


# --- dispatcher haut niveau -------------------------------------------------------------


class TelegramBot:
    """Bot Telegram interactif. `start()` lance un thread de long polling, `stop()` le
    signale et attend son arrêt. La méthode `handle_update()` est aussi appelable en
    direct par les tests pour rejouer une update sans démarrer de thread."""

    def __init__(
        self,
        config: Config,
        store: Store,
        summarizer: Summarizer,
        question_answerer: QuestionAnswerer,
        claude_lock: threading.Lock,
        notifier: TelegramNotifier,
        api: TelegramApi | None = None,
        get_playlist: Callable[[str], PlaylistConfig] | None = None,
        pipeline_process: Callable[[str, DetailLevel | None], None] | None = None,
        link_processor: Callable[[str, str], str] | None = None,
        digest_runner: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        secrets = config.secrets
        if not secrets.telegram_bot_token or not secrets.telegram_chat_id:
            raise NotifyError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis pour le bot")
        self._config = config
        self._store = store
        self._summarizer = summarizer
        self._question_answerer = question_answerer
        self._claude_lock = claude_lock
        self._notifier = notifier
        self._api = api or TelegramApi(secrets.telegram_bot_token)
        self._chat_id = secrets.telegram_chat_id
        self._get_playlist = get_playlist or (lambda _: PlaylistConfig(id="", label="Veille"))
        self._pipeline_process = pipeline_process
        # Lot 8 : traitement d'un lien partagé — reçoit (url, source) et retourne un item_id.
        self._link_processor = link_processor
        # Lot 8 : digest hebdomadaire — reçoit un spec `--since` ("7d" par défaut).
        self._digest_runner = digest_runner
        self._now = now or (lambda: datetime.now(UTC))
        self._rate_limit = RateLimiter(config.telegram.rate_limit_per_hour, now=self._now)
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    # --- cycle de vie ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._loop, name="guetteur-telegram-bot", daemon=True
        )
        self._thread.start()
        log.info("telegram_bot.started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        log.info("telegram_bot.stopped")

    def _loop(self) -> None:
        """Boucle de polling avec backoff progressif sur exception."""
        attempts = 0
        while not self._stop_flag.is_set():
            try:
                self._poll_once()
                attempts = 0  # succès : reset du backoff
            except Exception as exc:
                delay = _BACKOFF_S[min(attempts, len(_BACKOFF_S) - 1)]
                log.warning(
                    "telegram_bot.poll_failed",
                    extra={"attempts": attempts + 1, "sleep_s": delay, "error": str(exc)},
                )
                attempts += 1
                self._stop_flag.wait(delay)

    def _poll_once(self) -> None:
        offset_raw = self._store.get_meta(META_OFFSET) or "0"
        offset = int(offset_raw)
        timeout_s = self._config.telegram.poll_timeout_s
        updates = self._api.get_updates(offset, timeout_s)
        self._store.set_meta(META_HEARTBEAT, self._now().isoformat(timespec="seconds"))
        for update in updates:
            update_id = int(update.get("update_id", 0))
            self._store.set_meta(META_OFFSET, str(update_id + 1))
            try:
                self.handle_update(update)
            except Exception:
                log.exception("telegram_bot.update_failed", extra={"update_id": update_id})

    # --- dispatch ----------------------------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_message(message)

    def _chat_matches(self, chat_id: Any) -> bool:
        try:
            return str(chat_id) == str(self._chat_id)
        except (TypeError, ValueError):
            return False

    def _log_unknown_chat(self, chat_id: Any) -> None:
        """Log INFO limité à 1 par heure et par id inconnu (protection contre le bruit)."""
        key = f"{META_UNKNOWN_LOG_PREFIX}{chat_id}"
        raw = self._store.get_meta(key)
        now = self._now()
        if raw:
            try:
                last = datetime.fromisoformat(raw)
            except ValueError:
                last = None
            if last and now - last < timedelta(hours=1):
                return
        log.info("telegram_bot.unknown_chat", extra={"chat_id": chat_id})
        self._store.set_meta(key, now.isoformat(timespec="seconds"))

    # --- messages entrants -------------------------------------------------------------

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._chat_matches(chat_id):
            self._log_unknown_chat(chat_id)
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text, message)
            return
        # Message texte normal : reply ou question en cours ?
        reply_to = message.get("reply_to_message")
        if isinstance(reply_to, dict):
            reply_id = reply_to.get("message_id")
            video_id = self._store.video_for_message(int(reply_id)) if reply_id else None
            if video_id:
                self._answer_question(video_id, text)
                return
        # Lot 8 : URL dans le message (hors reply, hors commande) = lien à analyser.
        from guetteur.sources.liens.extract import extract_urls

        urls = extract_urls(text)
        if urls:
            self._process_shared_urls(urls, source="telegram")
            return
        # Sinon : y a-t-il une question en attente ?
        video_id = self._awaiting_video()
        if video_id:
            self._clear_awaiting()
            self._answer_question(video_id, text)
            return
        self._send_plain(
            "Je n'ai pas trouvé de vidéo liée à ce message. Réponds à un résumé, "
            "appuie sur « Question » sous un résumé, partage une URL, "
            "ou tape /help."
        )

    def _process_shared_urls(self, urls: list[str], *, source: str) -> None:
        """Lot 8 : ACK immédiat puis traitement de chaque URL via `link_processor`.
        Un échec sur une URL ne bloque pas les suivantes."""
        if self._link_processor is None:
            self._send_plain("Traitement des liens désactivé (link_processor absent).")
            return
        n = len(urls)
        self._send_plain(
            "Lien reçu, analyse en cours."
            if n == 1
            else f"{n} liens reçus, analyse en cours."
        )
        for url in urls:
            try:
                self._link_processor(url, source)
            except Exception as exc:
                log.exception("telegram_bot.link_failed", extra={"url": url})
                self._send_plain(f"Échec sur {url} : {type(exc).__name__} — {exc}")

    # --- callback_query -----------------------------------------------------------------

    def _handle_callback(self, cb: dict[str, Any]) -> None:
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        if not self._chat_matches(chat_id):
            self._log_unknown_chat(chat_id)
            return
        callback_id = str(cb.get("id", ""))
        action = parse_callback_data(str(cb.get("data") or ""))
        if action is None:
            self._answer_cb(callback_id, "Bouton non reconnu")
            return
        if action.kind == "question":
            self._start_awaiting_question(action.video_id, callback_id)
            return
        if action.kind == "keep":
            self._move_note(action.video_id, callback_id, discard=False)
            return
        if action.kind == "discard":
            self._move_note(action.video_id, callback_id, discard=True)
            return
        if action.kind == "idea":
            assert action.project_slug is not None
            self._send_project_prompt(action.video_id, action.project_slug, callback_id)
            return
        assert action.detail is not None
        self._deliver_summary_from_button(action.video_id, action.detail, callback_id)

    # --- boutons Lot 6 : Garder / Écarter / Idée pour <projet> --------------------------

    def _move_note(self, video_id: str, callback_id: str, discard: bool) -> None:
        if not self._config.obsidian.enabled:
            self._answer_cb(callback_id, "Obsidian désactivé dans config.toml")
            return
        try:
            from guetteur.export.obsidian import ObsidianExporter
        except ImportError:
            self._answer_cb(callback_id, "Module d'export absent")
            return
        exporter = ObsidianExporter(self._config, self._store)
        note = self._store.obsidian_note(video_id)
        if note is None:
            self._answer_cb(callback_id, "Aucune note Obsidian pour cette vidéo")
            return
        if discard:
            target = exporter.move_to_discarded(video_id)
            label = "écartée"
        else:
            # « Garder » sans thème connu : on demande à l'utilisateur d'utiliser /theme.
            theme = note[2] or "Inbox"
            target = exporter.move_to_theme(video_id, theme)
            label = f"gardée dans « {theme} »"
        if target is None:
            self._answer_cb(callback_id, "Déplacement impossible")
            return
        self._answer_cb(callback_id, f"OK : {label}")
        self._send_plain(f"📁 Note {label} : {target.name}")

    def _send_project_prompt(self, video_id: str, project_slug: str, callback_id: str) -> None:
        rows = self._store.applicability_for(video_id)
        match = next((r for r in rows if r[0] == project_slug), None)
        if match is None or not match[6]:
            self._answer_cb(callback_id, "Aucun méga-prompt pour ce projet")
            return
        self._answer_cb(callback_id, f"Méga-prompt {project_slug.upper()} envoyé")
        prompt = match[6]
        header = f"Méga-prompt Claude Code — {project_slug.upper()}\n"
        # Découpage en tranches de ≤ 4096 caractères, texte brut copiable.
        chunks: list[str] = []
        limit = 4000
        text = header + prompt
        while text:
            chunks.append(text[:limit])
            text = text[limit:]
        for chunk in chunks:
            self._send_plain(chunk)

    def _answer_cb(self, callback_id: str, text: str) -> None:
        try:
            self._api.answer_callback_query(callback_id, text)
        except TelegramApiError as exc:
            log.warning("telegram_bot.answer_cb_failed", extra={"error": str(exc)})

    # --- boutons : génération / cache d'un niveau ---------------------------------------

    def _deliver_summary_from_button(
        self, video_id: str, detail: DetailLevel, callback_id: str
    ) -> None:
        # Lot 7 §1 : sanity check — la regex du callback l'a déjà validé, mais
        # cette méthode est aussi appelée par /detail après _run_admin.
        if not _VALID_VIDEO_ID.fullmatch(video_id):
            self._answer_cb(callback_id, "Identifiant vidéo invalide")
            return
        record = self._store.get(video_id)
        if record is None:
            self._answer_cb(callback_id, "Vidéo introuvable en base")
            return
        cached = self._store.cached_summary(video_id, detail)
        if cached is not None:
            self._answer_cb(callback_id, "Résumé disponible, envoi en cours")
            summary = summary_from_json(cached)
            self._send_summary(summary, record.to_video(), detail)
            return
        # Génération : rate limit puis appel Claude sous verrou partagé.
        if not self._rate_limit.allow(self._chat_id):
            per_hour = self._config.telegram.rate_limit_per_hour
            self._answer_cb(
                callback_id,
                f"Limite atteinte ({per_hour}/h), réessayez plus tard",
            )
            self._send_plain(
                f"Limite de {self._config.telegram.rate_limit_per_hour} générations par heure "
                "atteinte pour le bot. Réessayez plus tard."
            )
            return
        self._answer_cb(callback_id, "Génération en cours, ~3 min")
        try:
            transcript = self._require_transcript(record)
        except _MissingContextError as exc:
            self._send_plain(str(exc))
            return
        try:
            with self._claude_lock:
                playlist = self._get_playlist(record.playlist_id)
                summary = self._summarizer.summarize(
                    transcript,
                    SummaryMeta(video=record.to_video(), language=playlist.language, detail=detail),
                )
        except Exception as exc:
            log.exception("telegram_bot.summary_failed", extra={"video_id": video_id})
            self._send_plain(f"Erreur pendant la génération : {type(exc).__name__}")
            return
        self._store.cache_summary(video_id, detail, summary_to_json(summary))
        self._send_summary(summary, record.to_video(), detail)

    def _send_summary(self, summary: Summary, video: Video, detail: DetailLevel) -> None:
        playlist = self._get_playlist_safe(video.video_id)
        msg = _summary_message(summary, video, playlist.label, detail)
        try:
            result = self._notifier.send(msg)
        except NotifyError as exc:
            log.error(
                "telegram_bot.send_failed", extra={"video_id": video.video_id, "error": str(exc)}
            )
            self._send_plain(f"L'envoi Telegram a échoué : {exc}")
            return
        # Le notifier retourne « id1,id2,id3 » : on lie CHAQUE message envoyé à la vidéo
        # pour que reply-to-any-part fonctionne.
        for raw_id in (result or "").split(","):
            raw_id = raw_id.strip()
            if raw_id.isdigit():
                self._store.link_message(int(raw_id), video.video_id, kind=f"summary:{detail}")

    def _get_playlist_safe(self, video_id: str) -> PlaylistConfig:
        record = self._store.get(video_id)
        if record is None:
            return PlaylistConfig(id="", label="")
        return self._get_playlist(record.playlist_id)

    # --- questions ----------------------------------------------------------------------

    def _start_awaiting_question(self, video_id: str, callback_id: str) -> None:
        expiry = self._now() + timedelta(minutes=self._config.telegram.question_ttl_min)
        self._store.set_meta(META_AWAITING_VIDEO, video_id)
        self._store.set_meta(META_AWAITING_UNTIL, expiry.isoformat(timespec="seconds"))
        self._answer_cb(callback_id, "Envoyez votre question dans les 10 minutes")
        self._send_plain(
            "❓ Envoyez votre question dans les 10 prochaines minutes ; je répondrai "
            "avec des extraits horodatés de la vidéo."
        )

    def _awaiting_video(self) -> str | None:
        video_id = self._store.get_meta(META_AWAITING_VIDEO)
        until = self._store.get_meta(META_AWAITING_UNTIL)
        if not video_id or not until:
            return None
        try:
            deadline = datetime.fromisoformat(until)
        except ValueError:
            self._clear_awaiting()
            return None
        if self._now() > deadline:
            self._clear_awaiting()
            return None
        return video_id

    def _clear_awaiting(self) -> None:
        self._store.set_meta(META_AWAITING_VIDEO, "")
        self._store.set_meta(META_AWAITING_UNTIL, "")

    def _answer_question(self, video_id: str, question: str) -> None:
        # Lot 7 §1 : video_id validé avant tout accès au store / à Claude.
        if not _VALID_VIDEO_ID.fullmatch(video_id):
            self._send_plain("Identifiant vidéo invalide.")
            return
        # Lot 7 §7 : longueur bornée AVANT tout appel Claude (coût + DoS).
        if len(question) > QUESTION_MAX_CHARS:
            self._send_plain(
                f"Question trop longue ({len(question)} caractères, max "
                f"{QUESTION_MAX_CHARS}). Reformulez plus court."
            )
            return
        record = self._store.get(video_id)
        if record is None:
            self._send_plain("Vidéo introuvable.")
            return
        try:
            transcript = self._require_transcript(record)
        except _MissingContextError as exc:
            self._send_plain(str(exc))
            return
        if not self._rate_limit.allow(self._chat_id):
            self._send_plain(
                f"Limite de {self._config.telegram.rate_limit_per_hour} générations "
                "par heure atteinte. Réessayez plus tard."
            )
            return
        history = self._store.recent_qa(video_id, self._config.telegram.qa_history_size)
        try:
            with self._claude_lock:
                answer = self._question_answerer(
                    transcript.to_timestamped_text(), question, history
                )
        except Exception as exc:
            log.exception("telegram_bot.qa_failed", extra={"video_id": video_id})
            self._send_plain(f"Erreur pendant la Q&A : {type(exc).__name__}")
            return
        self._store.record_qa(video_id, question, answer)
        header = f"*Q\\.* {escape_md_v2(question[:200])}"
        body = escape_md_v2(answer)
        text = f"{header}\n\n{body}"
        message_id = self._send_markdown_v2(text)
        if message_id is not None:
            self._store.link_message(message_id, video_id, kind="answer")

    def _require_transcript(self, record: Any) -> Any:
        from guetteur.pipeline import transcript_from_json

        if record.transcript is None:
            raise _MissingContextError(
                f"Pas de transcription en base pour {record.video_id}. Lancez d'abord "
                "un cycle (`guetteur once`) pour la capturer."
            )
        return transcript_from_json(record.video_id, record.transcript)

    # --- commandes texte ---------------------------------------------------------------

    def _handle_command(self, text: str, _message: dict[str, Any]) -> None:
        # Lot 7 §7 : borne de sécurité — une commande légitime tient en < 100 chars.
        if len(text) > COMMAND_MAX_CHARS:
            self._send_plain("Commande trop longue.")
            return
        parts = text.split()
        cmd = parts[0].lower().split("@", 1)[0]
        args = parts[1:]
        if cmd == "/help":
            self._send_plain(self._help_text())
            return
        if cmd == "/status":
            self._send_plain(self._status_text())
            return
        if cmd == "/last":
            self._send_last_summary()
            return
        if cmd in ("/retry", "/reset", "/detail"):
            if not args:
                self._send_plain(f"Usage : {cmd} <video_id>[ <niveau>]")
                return
            self._run_admin(cmd, args)
            return
        if cmd == "/theme":
            self._cmd_theme(args)
            return
        if cmd == "/projets":
            self._cmd_projects()
            return
        if cmd == "/applicabilite":
            self._cmd_applicability(args)
            return
        if cmd == "/idees":
            self._cmd_ideas(args)
            return
        if cmd == "/livre":
            self._cmd_livre_create(args)
            return
        if cmd == "/livres":
            self._cmd_livres_list()
            return
        if cmd == "/lien":
            self._cmd_lien(args)
            return
        if cmd == "/liens":
            self._cmd_liens()
            return
        if cmd == "/digest":
            self._cmd_digest(args)
            return
        self._send_plain(f"Commande inconnue : {cmd}. /help pour la liste.")

    def _cmd_lien(self, args: list[str]) -> None:
        """`/lien <URL>` : traite explicitement une URL comme un lien à résumer."""
        from guetteur.sources.liens.extract import extract_urls

        if not args:
            self._send_plain("Usage : /lien <URL>")
            return
        urls = extract_urls(" ".join(args))
        if not urls:
            self._send_plain("Aucune URL http(s) trouvée dans l'argument.")
            return
        self._process_shared_urls(urls, source="telegram")

    def _cmd_liens(self) -> None:
        """`/liens` : 10 derniers items LIEN envoyés."""
        items = [i for i in self._store.list_items(limit=20) if i.really_sent][:10]
        if not items:
            self._send_plain("Aucun lien envoyé pour le moment.")
            return
        lines = ["*10 derniers liens*", ""]
        for item in items:
            label = {
                "tweet": "📎",
                "article": "📰",
                "github": "🐙",
                "youtube_oneshot": "🎞️",
            }.get(item.kind, "🔗")
            title = (item.title or item.url)[:70]
            lines.append(f"{label} [{title}]({item.url})")
        self._api.send_message(
            self._chat_id,
            "\n".join(lines),
            parse_mode="MarkdownV2",
        )

    def _cmd_digest(self, args: list[str]) -> None:
        """`/digest [spec]` : déclenche un digest. spec défaut 7d."""
        if self._digest_runner is None:
            self._send_plain("Digest désactivé (runner absent).")
            return
        spec = args[0] if args else "7d"
        try:
            self._digest_runner(spec)
        except Exception as exc:
            log.exception("telegram_bot.digest_failed")
            self._send_plain(f"Digest : échec — {type(exc).__name__} : {exc}")

    def _cmd_livre_create(self, args: list[str]) -> None:
        """`/livre <URL> [--order date|views|duration] [--max N]` : crée un job
        livre à partir d'une URL de chaîne, avec les valeurs par défaut de
        config (`[livre] max_videos_default`, `detail_default`). Les flags
        `--order` et `--max` reflètent `livre create --order/--max-videos` de
        la CLI. Pour les autres filtres (--since, --min-duration…) l'utilisateur
        passe toujours par un terminal. Le job n'est PAS lancé automatiquement :
        le bot renvoie l'estimation et attend la confirmation via
        `guetteur livre run <id>` (délégué à un terminal)."""
        if not args:
            self._send_plain(
                "Usage : /livre <URL de chaîne> [--order date|views|duration] [--max N]\n"
                "Ensuite : `guetteur livre run <id>` depuis un terminal pour lancer."
            )
            return
        try:
            url, order, max_videos = _parse_livre_cmd_args(args)
        except ValueError as exc:
            self._send_plain(f"/livre : {exc}")
            return
        if self._store.has_running_livre():
            self._send_plain("Un livre est déjà en cours. `/livres` pour voir l'état.")
            return
        try:
            from guetteur.jobs.livre import persist_new_livre, plan_book
            from guetteur.sources.channel import ChannelFilters

            filters = ChannelFilters(
                max_videos=max_videos or self._config.livre.max_videos_default,
                order=order,
            )
            plan = plan_book(self._config, url, title=None, filters=filters)
            livre_id = persist_new_livre(self._store, plan, url)
        except Exception as exc:
            self._send_plain(f"Création du livre en échec : {type(exc).__name__} — {exc}")
            return
        self._send_plain(
            f"Livre {livre_id} créé.\n\n{plan.render()}\n\n"
            f"Pour lancer : `guetteur livre run {livre_id}`"
        )

    def _cmd_livres_list(self) -> None:
        from guetteur.jobs.livre import list_livres_text

        self._send_plain(list_livres_text(self._store))

    def _cmd_ideas(self, args: list[str]) -> None:
        """`/idees <projet>` : renvoie les 5 dernières entrées de Projets/<projet>/IDEES.md.
        Chaque bloc commence par « ## <date> » ; on prend les 5 plus récents (à la fin
        du fichier) et on les envoie en texte brut (méga-prompts inclus).

        Le `slug` reçu de Telegram est traité comme entrée non fiable même si le bot
        filtre déjà par chat_id : validation stricte par regex, puis vérification que
        le chemin résolu reste bien sous `projets_dir` (défense contre `../../secret`,
        les liens symboliques, ou les caractères de séparation de chemin exotiques)."""
        if not args:
            self._send_plain("Usage : /idees <projet>")
            return
        if not self._config.obsidian.enabled:
            self._send_plain("Obsidian désactivé dans config.toml")
            return
        slug = args[0].lower()
        if not _VALID_PROJECT_SLUG.fullmatch(slug):
            self._send_plain(
                "Nom de projet invalide (autorisés : lettres minuscules, chiffres, '-', '_')."
            )
            return
        projets_root = (self._config.obsidian.path / self._config.obsidian.projets_dir).resolve()
        ideas_path = projets_root / slug / "IDEES.md"
        # Défense en profondeur : le chemin résolu doit rester sous projets_root
        # (bloque les symlinks pointant hors du vault).
        try:
            resolved = ideas_path.resolve()
        except OSError:
            self._send_plain("Nom de projet invalide.")
            return
        if projets_root not in resolved.parents:
            self._send_plain("Nom de projet invalide.")
            return
        if not ideas_path.exists():
            self._send_plain(f"Aucune idée pour « {slug} » (fichier absent).")
            return
        try:
            text = ideas_path.read_text(encoding="utf-8")
        except OSError as exc:
            # Lot 7 §6 : ne pas fuiter le chemin absolu du vault ; on logue le détail
            # côté serveur et on renvoie un message générique à l'utilisateur.
            log.warning(
                "telegram_bot.ideas_read_failed",
                extra={"slug": slug, "error": f"{type(exc).__name__}: {exc}"},
            )
            self._send_plain(f"Lecture de IDEES.md impossible ({type(exc).__name__}).")
            return
        # Découpe sur les frontières « \n## » (chaque entrée commence par « ## <date> »).
        blocks = [b.strip() for b in text.split("\n## ") if b.strip()]
        if not blocks:
            self._send_plain(f"IDEES.md de « {slug} » est vide.")
            return
        # Le premier bloc contient le « # Idées » d'en-tête ; on le drop s'il n'a pas
        # de date. Les autres commencent par « <date> — <lien> ».
        entries = [b for b in blocks if b[:4].isdigit()]
        last5 = entries[-5:]
        if not last5:
            self._send_plain(f"IDEES.md de « {slug} » n'a pas encore d'entrée datée.")
            return
        header = f"Dernières idées pour {slug.upper()} :"
        text_out = header + "\n\n## " + "\n\n## ".join(last5)
        # Envoi en tranches ≤ 4000 caractères (texte brut copiable).
        limit = 4000
        while text_out:
            self._send_plain(text_out[:limit])
            text_out = text_out[limit:]

    def _cmd_theme(self, args: list[str]) -> None:
        if len(args) < 2:
            self._send_plain("Usage : /theme <video_id> <thème>")
            return
        video_id, theme = args[0], " ".join(args[1:])
        if not self._config.obsidian.enabled:
            self._send_plain("Obsidian désactivé dans config.toml")
            return
        from guetteur.export.obsidian import ObsidianExporter, ObsidianExportError

        exporter = ObsidianExporter(self._config, self._store)
        try:
            target = exporter.move_to_theme(video_id, theme)
        except ObsidianExportError as exc:
            self._send_plain(f"Nom de thème invalide : {exc}")
            return
        if target is None:
            self._send_plain(f"Aucune note Obsidian pour {video_id}")
            return
        self._send_plain(f"📁 {video_id} déplacée dans Veille/{theme}/")

    def _cmd_projects(self) -> None:
        if not self._config.obsidian.enabled:
            self._send_plain("Obsidian désactivé dans config.toml")
            return
        from guetteur.export.obsidian import ObsidianExporter

        exporter = ObsidianExporter(self._config, self._store)
        sheets = exporter.load_project_sheets()
        if not sheets:
            self._send_plain("Aucune fiche projet dans Projets/")
            return
        # Ligne 1 : décompte des fiches actives (après filtrage `_` + statut).
        lines = [f"{len(sheets)} fiche(s) projet active(s) :"]
        for s in sheets:
            lines.append(f"• {s.slug} — {s.nom} ({s.statut})")
        # Ligne finale : slugs présélectionnés à la dernière passe (pipeline ou
        # /applicabilite). Le stockage passe par le meta store, donc l'info
        # survit à un redémarrage du bot.
        raw = self._store.get_meta("last_preselected_projects")
        if raw:
            try:
                last = json.loads(raw)
            except json.JSONDecodeError:
                last = []
            if isinstance(last, list) and last:
                lines.append("")
                joined = ", ".join(str(s) for s in last)
                lines.append(f"Dernière passe — présélection ({len(last)}) : {joined}")
        self._send_plain("\n".join(lines))

    def _cmd_applicability(self, args: list[str]) -> None:
        if not args:
            self._send_plain("Usage : /applicabilite <video_id>")
            return
        if not self._config.applicability.enabled:
            self._send_plain("applicability.enabled = false dans config.toml")
            return
        video_id = args[0]
        # Lot 7 §1 : format YouTube strict avant tout accès store/Claude.
        if not _VALID_VIDEO_ID.fullmatch(video_id):
            self._send_plain("Identifiant vidéo invalide (format YouTube attendu).")
            return
        record = self._store.get(video_id)
        if record is None or record.summary is None:
            self._send_plain("Vidéo sans résumé en base.")
            return
        # Lot 7 §2 : la passe applicabilité est un appel Claude → doit compter dans
        # le rate limit au même titre que /detail et les questions.
        if not self._rate_limit.allow(self._chat_id):
            self._send_plain(
                f"Limite de {self._config.telegram.rate_limit_per_hour} générations "
                "par heure atteinte. Réessayez plus tard."
            )
            return
        from guetteur.export.obsidian import ObsidianExporter
        from guetteur.summarize.applicability import (
            build_evaluator_from_summarizer,
            preselect_projects,
        )
        from guetteur.summarize.base import summary_from_json

        exporter = ObsidianExporter(self._config, self._store)
        exporter.ensure_vault_layout()
        sheets = exporter.load_project_sheets()
        if not sheets:
            self._send_plain("Aucune fiche projet dans Projets/")
            return
        summary = summary_from_json(record.summary)
        video = record.to_video()
        selected = preselect_projects(sheets, summary, self._config.applicability.max_projects)
        # Persiste la sélection pour que /projets affiche la même liste que le
        # pipeline automatique (source d'autorité : la dernière passe).
        self._store.set_meta(
            "last_preselected_projects",
            json.dumps([s.slug for s in selected], ensure_ascii=False),
        )
        evaluator = build_evaluator_from_summarizer(self._summarizer)
        try:
            with self._claude_lock:
                pertinences = evaluator.evaluate(video, summary, selected)
        except Exception as exc:
            log.exception("telegram_bot.applicability_failed", extra={"video_id": video_id})
            self._send_plain(f"Applicabilité en échec : {type(exc).__name__}")
            return
        self._store.clear_applicability(video_id)
        for p in pertinences:
            self._store.upsert_applicability(
                video_id,
                p.projet,
                p.score,
                p.idee,
                p.integration,
                p.effort,
                p.risques,
                p.prompt_claude_code,
            )
            if p.is_actionable:
                exporter.append_idea(
                    video,
                    p.projet,
                    p.score,
                    p.idee,
                    p.integration,
                    p.effort,
                    p.prompt_claude_code,
                )
        best = [f"{p.projet}({p.score})" for p in pertinences if p.score >= 1]
        self._send_plain(f"Applicabilité mise à jour : {', '.join(best) or 'aucune pertinence'}")

    def _help_text(self) -> str:
        return (
            "Commandes du bot :\n"
            "/status — 10 dernières vidéos\n"
            "/last — dernier résumé envoyé\n"
            "/retry <id> — remet une vidéo « failed » en file\n"
            "/reset <id> — retraitement complet\n"
            "/detail <id> <bref|standard|detaille> — génère un niveau\n"
            "/theme <id> <thème> — déplace la note dans Veille/<thème>/\n"
            "/projets — liste les fiches projet chargées\n"
            "/applicabilite <id> — relance la seconde passe Claude\n"
            "/idees <projet> — 5 dernières entrées de Projets/<projet>/IDEES.md\n"
            "/livre <URL> [--order date|views|duration] [--max N] — crée un job de "
            "compilation d'une chaîne YouTube en ebook\n"
            "/livres — liste les livres en base et leur état\n"
            "/lien <URL> — analyse et résume un lien (tweet, article, repo, vidéo)\n"
            "/liens — 10 derniers liens envoyés\n"
            "/digest [7d|24h|…] — digest hebdomadaire des liens et vidéos gardés\n"
            "/help — cette aide\n\n"
            "Réponds à n'importe quel message de résumé pour poser une question sur la vidéo, "
            "ou utilise le bouton « Question »."
        )

    def _status_text(self) -> str:
        records = self._store.list_videos(limit=10)
        if not records:
            return "Aucune vidéo en base."
        lines = ["Dernières vidéos :"]
        for r in records:
            title = (r.title or "?")[:40]
            lines.append(f"• {r.video_id} — {title} — {r.status.value}")
        return "\n".join(lines)

    def _send_last_summary(self) -> None:
        record = next(iter(self._store.list_videos(limit=1)), None)
        if record is None or record.summary is None:
            self._send_plain("Aucun résumé disponible.")
            return
        summary = summary_from_json(record.summary)
        self._send_summary(summary, record.to_video(), summary.detail)

    def _run_admin(self, cmd: str, args: list[str]) -> None:
        video_id = args[0]
        # Lot 7 §1 : format YouTube strict avant tout accès (protège logs + callbacks).
        if not _VALID_VIDEO_ID.fullmatch(video_id):
            self._send_plain("Identifiant vidéo invalide (format YouTube attendu).")
            return
        record = self._store.get(video_id)
        if record is None:
            self._send_plain(f"Vidéo inconnue : {video_id}")
            return
        if cmd == "/retry":
            self._store.retry_failed(video_id)
            self._send_plain(f"✅ {video_id} remise en file (au prochain cycle).")
            return
        if cmd == "/reset":
            self._store.reset(video_id)
            self._send_plain(f"✅ {video_id} remise en « new ». Sera retraitée.")
            return
        if cmd == "/detail":
            if len(args) < 2 or args[1] not in DETAIL_LEVELS:
                self._send_plain(f"Usage : /detail <id> {'|'.join(DETAIL_LEVELS)}")
                return
            # `args[1] in DETAIL_LEVELS` a rétréci le type ; cast explicite pour mypy.
            detail: DetailLevel = args[1]
            self._deliver_summary_from_button(video_id, detail, callback_id="")

    # --- utilitaires d'envoi ------------------------------------------------------------

    def _send_plain(self, text: str) -> int | None:
        try:
            return self._api.send_message(self._chat_id, text, parse_mode=None)
        except TelegramApiError as exc:
            log.warning("telegram_bot.plain_send_failed", extra={"error": str(exc)})
            return None

    def _send_markdown_v2(self, text: str) -> int | None:
        try:
            return self._api.send_message(self._chat_id, text, parse_mode="MarkdownV2")
        except TelegramApiError as exc:
            log.warning("telegram_bot.mdv2_send_failed", extra={"error": str(exc)})
            # Fallback plain : le bot est chargé de préserver la conversation, jamais
            # d'abandonner en silence.
            return self._send_plain(text)


class _MissingContextError(RuntimeError):
    """Le bot ne peut pas répondre car il manque une transcription ou un résumé."""
