"""Backend « claude_api » : SDK anthropic avec clé API et sortie JSON structurée."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from guetteur.models import DetailLevel
from guetteur.summarize.base import (
    ChunkedSummarizer,
    SummarizeError,
    SummarizerUnavailableError,
    schema_for,
    system_prompt_for,
)

# Le mode détaillé produit ~1500 mots + JSON structure : on double la marge côté modèle.
MAX_TOKENS_STANDARD = 8_000
MAX_TOKENS_DETAILED = 16_000


def _max_tokens_for(detail: DetailLevel) -> int:
    return MAX_TOKENS_DETAILED if detail == "detaille" else MAX_TOKENS_STANDARD


class ClaudeApiSummarizer(ChunkedSummarizer):
    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model

    def _complete(self, instruction: str, document: str, detail: DetailLevel) -> dict[str, Any]:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=_max_tokens_for(detail),
                system=system_prompt_for(detail),
                messages=[{"role": "user", "content": f"{instruction}\n\n{document}"}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema_for(detail)},
                },
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise SummarizerUnavailableError(
                f"API Claude : clé refusée (HTTP {exc.status_code}) — vérifiez ANTHROPIC_API_KEY"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise SummarizeError(f"API Claude : HTTP {exc.status_code} : {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise SummarizeError(f"API Claude injoignable : {exc}") from exc

        if response.stop_reason in ("max_tokens", "refusal"):
            raise SummarizeError(f"Réponse Claude incomplète (stop_reason={response.stop_reason})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise SummarizeError("Réponse Claude sans bloc texte")
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SummarizeError(f"JSON invalide renvoyé par Claude : {exc}") from exc
        return data
