"""Canaux de notification ; le canal est choisi par playlist (playlist.notify)."""

from __future__ import annotations

from guetteur.config import Config, NotifyChannel
from guetteur.notify.base import Message, Notifier, NotifyError, split_message
from guetteur.notify.telegram import TelegramNotifier
from guetteur.notify.whatsapp_cloud import WhatsAppCloudNotifier

__all__ = ["Message", "Notifier", "NotifyError", "build_notifier", "split_message"]


def build_notifier(channel: NotifyChannel, config: Config) -> Notifier:
    s = config.secrets
    if channel == "telegram":
        return TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    return WhatsAppCloudNotifier(s.wa_token, s.wa_phone_id, s.wa_to, settings=config.whatsapp)
