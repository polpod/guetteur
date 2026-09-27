"""Bot Telegram interactif du Lot 5 : long polling dans un thread partagé par
`guetteur run`, boutons sous chaque résumé (Bref / Standard / Détaillé / Question),
Q&A avec la transcription complète en contexte, commandes texte /status /retry /reset
/detail /last /help. Le tout dans le même processus que le pipeline, avec un verrou
partagé pour ne jamais avoir deux appels Claude en même temps.

Le bot ne touche jamais directement au notifier de bas niveau : il utilise `TelegramApi`
pour les appels HTTP (getUpdates, sendMessage, answerCallbackQuery) et le pipeline pour
les mutations sur la base."""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from guetteur.config import Config, PlaylistConfig
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


# --- API bas niveau Telegram ----------------------------------------------------------


class TelegramApiError(RuntimeError):
    """Erreur au niveau HTTP Telegram (transport, 4xx, 5xx). Le bot les logue et
    applique un backoff avant de retenter le prochain getUpdates."""


class TelegramApi:
    """Client HTTP minimal pour les endpoints utilisés par le bot. Utilise le même
    httpx.Client que le notifier historique quand il est fourni (tests e2e mockent
    un seul transport)."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.Client(timeout=60.0)

    def _post(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
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


# --- callback_data ---------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackAction:
    video_id: str
    kind: str  # "detail" ou "question"
    detail: DetailLevel | None = None


_CALLBACK_RE = re.compile(r"^v:([A-Za-z0-9_\-]+):(?:d:(bref|standard|detaille)|q)$")


def parse_callback_data(raw: str) -> CallbackAction | None:
    """Parse « v:<video_id>:d:<niveau> » ou « v:<video_id>:q ». Retourne None si
    le format ne correspond pas (attaquant qui forge un callback, id vidéo invalide…).
    """
    m = _CALLBACK_RE.match(raw or "")
    if not m:
        return None
    video_id = m.group(1)
    level = m.group(2)
    if level:
        return CallbackAction(video_id=video_id, kind="detail", detail=level)  # type: ignore[arg-type]
    return CallbackAction(video_id=video_id, kind="question")


def build_summary_keyboard(video_id: str, detail_shown: DetailLevel) -> dict[str, Any]:
    """Construit la ligne de boutons sous un résumé. Le niveau déjà affiché est omis."""
    labels: dict[DetailLevel, str] = {
        "bref": "Bref",
        "standard": "Standard",
        "detaille": "Détaillé",
    }
    row: list[dict[str, str]] = []
    for level in DETAIL_LEVELS:
        if level == detail_shown:
            continue
        row.append({"text": labels[level], "callback_data": f"v:{video_id}:d:{level}"})
    row.append({"text": "Question", "callback_data": f"v:{video_id}:q"})
    return {"inline_keyboard": [row]}


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
        # Sinon : y a-t-il une question en attente ?
        video_id = self._awaiting_video()
        if video_id:
            self._clear_awaiting()
            self._answer_question(video_id, text)
            return
        self._send_plain(
            "Je n'ai pas trouvé de vidéo liée à ce message. Réponds à un résumé, "
            "appuie sur « Question » sous un résumé, ou tape /help."
        )

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
        assert action.detail is not None
        self._deliver_summary_from_button(action.video_id, action.detail, callback_id)

    def _answer_cb(self, callback_id: str, text: str) -> None:
        try:
            self._api.answer_callback_query(callback_id, text)
        except TelegramApiError as exc:
            log.warning("telegram_bot.answer_cb_failed", extra={"error": str(exc)})

    # --- boutons : génération / cache d'un niveau ---------------------------------------

    def _deliver_summary_from_button(
        self, video_id: str, detail: DetailLevel, callback_id: str
    ) -> None:
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
        record = self._store.get(video_id)
        if record is None:
            self._send_plain(f"Vidéo {video_id} introuvable.")
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
        self._send_plain(f"Commande inconnue : {cmd}. /help pour la liste.")

    def _help_text(self) -> str:
        return (
            "Commandes du bot :\n"
            "/status — 10 dernières vidéos\n"
            "/last — dernier résumé envoyé\n"
            "/retry <id> — remet une vidéo « failed » en file\n"
            "/reset <id> — retraitement complet\n"
            "/detail <id> <bref|standard|detaille> — génère un niveau\n"
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
