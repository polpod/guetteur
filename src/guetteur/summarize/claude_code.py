"""Backend « claude_code » : résumé via le binaire Claude Code (`claude -p`), sans clé API.

Le binaire utilise la session de l'utilisateur système (`claude auth login`). Aucune commande
ne passe par un shell ; la consigne va dans -p, la transcription sur stdin. Tous les outils
sont désactivés (--tools "" et --permission-mode plan), les réglages/hooks/serveurs MCP de
l'utilisateur ignorés, et la sortie est validée par --json-schema."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from guetteur.models import DetailLevel
from guetteur.summarize.base import (
    ChunkedSummarizer,
    SummarizeError,
    SummarizerUnavailableError,
    schema_for,
    system_prompt_for,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180.0
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
# Variables qui feraient utiliser une clé API au lieu de la session Claude Code.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY",)


class ClaudeCodeError(SummarizeError):
    """Réponse inexploitable du binaire (sortie tronquée, erreur API…) : retentable."""


class ClaudeCodeTimeoutError(ClaudeCodeError):
    """Le binaire n'a pas répondu dans le délai imparti."""


class ClaudeCodeNotFoundError(SummarizerUnavailableError):
    """Binaire `claude` introuvable."""


class ClaudeCodeNotLoggedInError(SummarizerUnavailableError):
    """Le binaire n'est pas connecté à un compte Claude."""


class Process(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


Spawn = Callable[..., Awaitable[Process]]


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


def _looks_not_logged_in(*texts: str) -> bool:
    joined = " ".join(texts).lower()
    return any(marker in joined for marker in ("not logged in", "/login", "invalid api key"))


def _decode_summary(result: str) -> dict[str, Any]:
    text = result.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeError(f"Claude Code : le champ result n'est pas du JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ClaudeCodeError("Claude Code : le résumé JSON n'est pas un objet")
    return data


class ClaudeCodeSummarizer(ChunkedSummarizer):
    def __init__(
        self,
        model: str,
        binary: str = "claude",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        spawn: Spawn | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._model = model
        self._binary = binary
        self._timeout_s = timeout_s
        self._spawn: Spawn = spawn or asyncio.create_subprocess_exec
        base_env = dict(os.environ if env is None else env)
        for key in _STRIPPED_ENV:
            base_env.pop(key, None)
        self._env = base_env

    # --- construction de la commande -------------------------------------------------------

    def _base_args(self, prompt: str) -> list[str]:
        return [
            self._binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self._model,
            "--tools",
            "",
            "--permission-mode",
            "plan",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "",
        ]

    def build_command(self, instruction: str, detail: DetailLevel = "standard") -> list[str]:
        return [
            *self._base_args(instruction),
            "--system-prompt",
            system_prompt_for(detail),
            "--json-schema",
            json.dumps(schema_for(detail), separators=(",", ":")),
        ]

    # --- exécution -------------------------------------------------------------------------

    async def _run(
        self, args: list[str], stdin: str, timeout_s: float | None = None
    ) -> CliResult:
        # Répertoire de travail vide : aucun CLAUDE.md de projet n'est chargé.
        effective_timeout = timeout_s if timeout_s is not None else self._timeout_s
        with tempfile.TemporaryDirectory(prefix="guetteur-claude-") as cwd:
            try:
                proc = await self._spawn(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=self._env,
                )
            except FileNotFoundError as exc:
                raise ClaudeCodeNotFoundError(
                    f"Binaire Claude Code introuvable : « {self._binary} ». Installez-le "
                    "(npm i -g @anthropic-ai/claude-code) ou réglez summarize.claude_code_bin."
                ) from exc
            except PermissionError as exc:
                raise ClaudeCodeNotFoundError(
                    f"Binaire Claude Code non exécutable : « {self._binary} »"
                ) from exc
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin.encode("utf-8")), timeout=effective_timeout
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ClaudeCodeTimeoutError(
                    f"Claude Code n'a pas répondu en {effective_timeout:.0f} s"
                ) from exc
        return CliResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )

    def _parse_envelope(self, res: CliResult) -> dict[str, Any]:
        """Décode l'enveloppe JSON de `--output-format json` et détecte les erreurs."""
        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(res.stdout)
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            envelope = None

        result_text = str(envelope.get("result", "")) if envelope else ""
        if envelope is not None and res.returncode == 0 and not envelope.get("is_error"):
            return envelope
        detail = (result_text or res.stderr or res.stdout).strip()
        # On ne cherche « not logged in » que dans une réponse en erreur : un résumé réussi
        # peut légitimement parler de connexion.
        if _looks_not_logged_in(result_text, res.stderr, "" if envelope else res.stdout):
            raise ClaudeCodeNotLoggedInError(
                "Claude Code n'est pas connecté pour cet utilisateur : lancez "
                f"`claude auth login` (détail : {detail[:200]})"
            )
        if envelope is None and res.returncode == 0:
            raise ClaudeCodeError(
                f"Claude Code : sortie JSON tronquée ou invalide ({detail[:300] or 'vide'})"
            )
        raise ClaudeCodeError(
            f"Claude Code : échec (code {res.returncode}) : {detail[:300] or 'sortie vide'}"
        )

    def _complete(self, instruction: str, document: str, detail: DetailLevel) -> dict[str, Any]:
        res = asyncio.run(self._run(self.build_command(instruction, detail), document))
        envelope = self._parse_envelope(res)
        result = envelope.get("result")
        if isinstance(result, str) and result.strip():
            try:
                return _decode_summary(result)
            except ClaudeCodeError:
                structured = envelope.get("structured_output")
                if isinstance(structured, dict):
                    return structured
                raise
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured
        raise ClaudeCodeError("Claude Code : champ result absent ou vide")

    # --- raw_call (Lot 7 : passes livre) --------------------------------------------------

    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str:
        """Appel brut via `claude -p` avec `--system-prompt`, et `--json-schema`
        quand `json_schema` est fourni. Mêmes garde-fous que `summarize` : aucun
        outil (`--tools ""`), permission plan, aucune session, settings utilisateur
        ignorés, ANTHROPIC_API_KEY retirée de l'env (voir `_STRIPPED_ENV`).

        Retourne la chaîne de résultat (JSON pour la passe plan, Markdown pour la
        passe chapitre). Lève `ClaudeCodeError` si la sortie est vide ou illisible,
        `ClaudeCodeTimeoutError` si le binaire dépasse `timeout_s`, et
        `ClaudeCodeNotLoggedInError` si Claude Code n'est pas connecté."""
        args = [
            *self._base_args(user_prompt),
            "--system-prompt",
            system_prompt,
        ]
        if json_schema is not None:
            args.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"))])
        res = asyncio.run(self._run(args, "", timeout_s=timeout_s))
        envelope = self._parse_envelope(res)
        result = envelope.get("result")
        if isinstance(result, str) and result.strip():
            return result
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            # `--json-schema` sans réponse texte : on reconstruit le JSON.
            return json.dumps(structured, ensure_ascii=False)
        raise ClaudeCodeError("Claude Code : réponse vide pour un appel livre (raw_call)")

    # --- diagnostic ------------------------------------------------------------------------

    def ping(self) -> str:
        """`claude -p "ping" --output-format json` doit répondre sans erreur."""
        res = asyncio.run(self._run(self._base_args("ping"), ""))
        envelope = self._parse_envelope(res)
        return str(envelope.get("result", "")).strip()
