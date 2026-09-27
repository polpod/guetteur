"""Rendu d'un résumé : MarkdownV2 échappé (Telegram) et texte brut (WhatsApp).

Le Lot 4 introduit trois niveaux de détail (« bref », « standard », « detaille ») ; le mode
« detaille » peut dépasser 4096 caractères, on découpe alors aux frontières de section avec
une numérotation « (i/N) » en tête de chaque message."""

from __future__ import annotations

import math
import re

from guetteur.models import Summary, Video

# https://core.telegram.org/bots/api#markdownv2-style
# Caractères réservés MarkdownV2 (Telegram) : doivent être échappés partout dans le texte,
# y compris les préfixes de ligne (`!`, `>`, `-`…). Le seul moyen d'écrire un `!` littéral
# est `\!`. Le bug corrigé venait de préfixes littéraux `!` et `>` dans le rendu detaille.
_MDV2_RESERVED = frozenset("_*[]()~`>#+-=|{}.!\\")
_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
# Dans l'URL d'un lien [texte](url), seuls `)` et `\` doivent être échappés.
_MDV2_URL_SPECIAL = re.compile(r"([)\\])")

WORDS_PER_MINUTE = 200
TELEGRAM_LIMIT = 4096

# Séparateur interne placé aux frontières de section, retiré au moment de l'envoi ; il
# permet à `split_markdown_v2_parts` de couper proprement même quand le rendu tient dans
# une seule partie (les couturent alors joyeusement sans laisser de marqueur).
SECTION_MARKER = "\x00SECTION\x00"


def escape_markdown_v2(text: str) -> str:
    """Échappe TOUS les caractères réservés MarkdownV2 (_ * [ ] ( ) ~ ` > # + - = | { } . !
    et \\). C'est le seul chemin autorisé pour insérer du texte issu du modèle : tout
    contournement (préfixe littéral `!`, `>`, `-`…) doit passer par ici."""
    return _MDV2_SPECIAL.sub(r"\\\1", text)


# Alias explicite conforme au vocabulaire du lot correctif (« passer par une seule
# fonction escape_md_v2 »). Même fonction, deux noms pour ne rien casser des usages.
escape_md_v2 = escape_markdown_v2


def escape_markdown_v2_url(url: str) -> str:
    return _MDV2_URL_SPECIAL.sub(r"\\\1", url)


# --- validation d'un texte MarkdownV2 -----------------------------------------------------

_TG_BYTE_OFFSET_RE = re.compile(r"byte offset (\d+)")


def validate_markdown_v2(text: str) -> tuple[bool, int]:
    """Simule l'analyseur MarkdownV2 de Telegram et retourne (valide, offset_faute).

    Reconnaît trois balises utilisées par nos rendus : `*bold*`, `_italic_` et
    `[text](url)`. À l'intérieur de `*..*` et `_.._`, les caractères réservés doivent
    toujours être échappés — c'est aussi ainsi que Telegram parse le texte. Dans le
    contenu du lien, tout caractère réservé doit être échappé ; dans son URL, seuls `)`
    et `\\` doivent l'être.

    Renvoyer (False, i) ne dit pas « Telegram va refuser à coup sûr » (l'analyseur
    officiel est un peu plus permissif sur certaines combinaisons), mais un True fiable
    garantit qu'aucun caractère réservé n'est laissé nu — c'est le contrat qui nous
    intéresse pour ne plus envoyer une partie non parsable."""

    def _text_valid(sub: str) -> tuple[bool, int]:
        i = 0
        n = len(sub)
        while i < n:
            c = sub[i]
            if c == "\\":
                # Un backslash consomme le caractère suivant, quel qu'il soit.
                if i + 1 >= n:
                    return False, i
                i += 2
                continue
            if c == "*":
                j = _find_unescaped(sub, i + 1, "*")
                if j < 0:
                    return False, i
                ok, off = _text_valid(sub[i + 1 : j])
                if not ok:
                    return False, i + 1 + off
                i = j + 1
                continue
            if c == "_":
                j = _find_unescaped(sub, i + 1, "_")
                if j < 0:
                    return False, i
                ok, off = _text_valid(sub[i + 1 : j])
                if not ok:
                    return False, i + 1 + off
                i = j + 1
                continue
            if c == "[":
                close_txt = _find_unescaped(sub, i + 1, "]")
                if close_txt < 0 or close_txt + 1 >= n or sub[close_txt + 1] != "(":
                    return False, i
                close_url = _find_unescaped(sub, close_txt + 2, ")")
                if close_url < 0:
                    return False, i
                ok, off = _text_valid(sub[i + 1 : close_txt])
                if not ok:
                    return False, i + 1 + off
                # Dans l'URL, seuls ')' et '\\' doivent être échappés — le reste est libre.
                # `_find_unescaped` a déjà validé la première ) non échappée : rien de plus
                # à vérifier ici (les autres caractères sont autorisés dans l'URL).
                i = close_url + 1
                continue
            if c in _MDV2_RESERVED:
                return False, i
            i += 1
        return True, -1

    return _text_valid(text)


def _find_unescaped(text: str, start: int, target: str) -> int:
    i = start
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == target:
            return i
        i += 1
    return -1


def telegram_byte_offset(error_message: str) -> int | None:
    """Extrait « at byte offset N » d'une erreur Telegram, si présent."""
    match = _TG_BYTE_OFFSET_RE.search(error_message)
    return int(match.group(1)) if match else None


def excerpt_around(text: str, offset: int, radius: int = 40) -> str:
    """Extrait ~2*radius caractères autour de `offset` pour aider au diagnostic."""
    start = max(0, offset - radius)
    end = min(len(text), offset + radius)
    return text[start:end]


def timestamp_url(video_id: str, seconds: int) -> str:
    return f"https://youtu.be/{video_id}?t={max(0, int(seconds))}"


def format_timestamp(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def reading_time_minutes(*texts: str) -> int:
    words = sum(len(t.split()) for t in texts)
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


# --- rendus par niveau ------------------------------------------------------------------


def _header_lines_md_v2(summary: Summary, video: Video, label: str) -> list[str]:
    e = escape_markdown_v2
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [f"*{e(summary.title)}*"]
    if header_meta:
        lines.append(f"_{e(header_meta)}_")
    lines.append(f"[{e('▶️ Voir la vidéo')}]({escape_markdown_v2_url(video.url)})")
    return lines


def _header_lines_plain(summary: Summary, video: Video, label: str) -> list[str]:
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [summary.title]
    if header_meta:
        lines.append(header_meta)
    lines.append(video.url)
    return lines


def _footer_md_v2(summary: Summary) -> str:
    return escape_markdown_v2(f"⏱ Lecture : {summary.reading_time_minutes} min")


def _footer_plain(summary: Summary) -> str:
    return f"⏱ Lecture : {summary.reading_time_minutes} min"


def render_markdown_v2(summary: Summary, video: Video, label: str = "") -> str:
    """Une seule string MarkdownV2 (peut contenir des marqueurs SECTION_MARKER si detaille).
    Pour un envoi Telegram > 4096 chars, passer par `split_markdown_v2_parts`."""
    if summary.detail == "detaille":
        return _render_md_v2_detailed(summary, video, label)
    if summary.detail == "bref":
        return _render_md_v2_brief(summary, video, label)
    return _render_md_v2_standard(summary, video, label)


def render_plain(summary: Summary, video: Video, label: str = "") -> str:
    if summary.detail == "detaille":
        return _render_plain_detailed(summary, video, label)
    if summary.detail == "bref":
        return _render_plain_brief(summary, video, label)
    return _render_plain_standard(summary, video, label)


def _render_md_v2_standard(summary: Summary, video: Video, label: str) -> str:
    e = escape_markdown_v2
    lines = _header_lines_md_v2(summary, video, label)
    lines += ["", f"*TL;DR* — {e(summary.tldr)}", "", "*Points clés*"]
    for kp in summary.key_points:
        link = escape_markdown_v2_url(timestamp_url(video.video_id, kp.seconds))
        lines.append(f"• [{e(format_timestamp(kp.seconds))}]({link}) {e(kp.text)}")
    lines += [
        "",
        f"*Pourquoi ça compte* — {e(summary.why_it_matters)}",
        "",
        _footer_md_v2(summary),
    ]
    return "\n".join(lines)


def _render_plain_standard(summary: Summary, video: Video, label: str) -> str:
    lines = _header_lines_plain(summary, video, label)
    lines += ["", f"TL;DR : {summary.tldr}", "", "Points clés :"]
    for kp in summary.key_points:
        lines.append(f"• {format_timestamp(kp.seconds)} — {kp.text}")
        lines.append(f"  {timestamp_url(video.video_id, kp.seconds)}")
    lines += [
        "",
        f"Pourquoi ça compte : {summary.why_it_matters}",
        "",
        _footer_plain(summary),
    ]
    return "\n".join(lines)


def _render_md_v2_brief(summary: Summary, video: Video, label: str) -> str:
    e = escape_markdown_v2
    lines = _header_lines_md_v2(summary, video, label)
    lines += ["", f"*TL;DR* — {e(summary.tldr)}", "", "*Points clés*"]
    for kp in summary.key_points:
        link = escape_markdown_v2_url(timestamp_url(video.video_id, kp.seconds))
        lines.append(f"• [{e(format_timestamp(kp.seconds))}]({link}) {e(kp.text)}")
    if summary.actions:
        lines += ["", "*À faire*"]
        for action in summary.actions:
            lines.append(f"→ {e(action)}")
    lines += ["", _footer_md_v2(summary)]
    return "\n".join(lines)


def _render_plain_brief(summary: Summary, video: Video, label: str) -> str:
    lines = _header_lines_plain(summary, video, label)
    lines += ["", f"TL;DR : {summary.tldr}", "", "Points clés :"]
    for kp in summary.key_points:
        lines.append(f"• {format_timestamp(kp.seconds)} — {kp.text}")
        lines.append(f"  {timestamp_url(video.video_id, kp.seconds)}")
    if summary.actions:
        lines += ["", "À faire :"]
        for action in summary.actions:
            lines.append(f"→ {action}")
    lines += ["", _footer_plain(summary)]
    return "\n".join(lines)


def _render_md_v2_detailed(summary: Summary, video: Video, label: str) -> str:
    """Rendu détaillé Telegram avec marqueur SECTION_MARKER aux frontières.

    Chaque frontière signale à `split_markdown_v2_parts` un endroit où la coupe est propre
    (jamais au milieu d'une puce ou d'une section). L'en-tête et le pied ne sont pas
    marqués : l'en-tête reste dans la 1ère partie, le pied dans la dernière."""
    e = escape_markdown_v2
    parts: list[str] = []
    header = "\n".join(_header_lines_md_v2(summary, video, label))
    intro = f"{header}\n\n*TL;DR* — {e(summary.tldr)}"
    parts.append(intro)
    for sec in summary.sections:
        section_lines = [
            SECTION_MARKER,
            f"*{e(sec.title)}* — [{e(format_timestamp(sec.seconds))}]"
            f"({escape_markdown_v2_url(timestamp_url(video.video_id, sec.seconds))})",
        ]
        for bullet in sec.bullets:
            section_lines.append(f"• {e(bullet)}")
        parts.append("\n".join(section_lines))
    if summary.citations:
        cite_lines = [SECTION_MARKER, "*Citations*"]
        for cit in summary.citations:
            link = escape_markdown_v2_url(timestamp_url(video.video_id, cit.seconds))
            # `>` en début de ligne est réservé (blockquote MarkdownV2) — on l'échappe
            # comme n'importe quel autre caractère de préfixe.
            cite_lines.append(
                f"{e('\u203a')} [{e(format_timestamp(cit.seconds))}]({link}) _{e(cit.text)}_"
            )
        parts.append("\n".join(cite_lines))
    if summary.actions:
        action_lines = [SECTION_MARKER, "*À faire*"]
        for action in summary.actions:
            # `→` est un caractère unicode non réservé mais on le passe quand même par
            # escape_md_v2 pour respecter la règle « tout texte du rendu passe par une
            # seule fonction ». `e(...)` sur du texte non réservé est idempotent.
            action_lines.append(f"{e('→')} {e(action)}")
        parts.append("\n".join(action_lines))
    if summary.reserves:
        reserve_lines = [SECTION_MARKER, "*Réserves*"]
        for reserve in summary.reserves:
            # `!` est réservé : c'est le bug corrigé qui faisait planter la partie 3/3.
            # Le préfixe passe désormais par escape_md_v2 comme tout le reste.
            reserve_lines.append(f"{e('!')} {e(reserve)}")
        parts.append("\n".join(reserve_lines))
    parts.append(f"{SECTION_MARKER}{_footer_md_v2(summary)}")
    return "\n\n".join(parts)


def _render_plain_detailed(summary: Summary, video: Video, label: str) -> str:
    lines = _header_lines_plain(summary, video, label)
    lines += ["", f"TL;DR : {summary.tldr}"]
    for sec in summary.sections:
        lines += [
            "",
            f"{sec.title} — {format_timestamp(sec.seconds)}",
            f"  {timestamp_url(video.video_id, sec.seconds)}",
        ]
        for bullet in sec.bullets:
            lines.append(f"• {bullet}")
    if summary.citations:
        lines += ["", "Citations :"]
        for cit in summary.citations:
            lines.append(f"> {format_timestamp(cit.seconds)} — « {cit.text} »")
    if summary.actions:
        lines += ["", "À faire :"]
        for action in summary.actions:
            lines.append(f"→ {action}")
    if summary.reserves:
        lines += ["", "Réserves :"]
        for reserve in summary.reserves:
            lines.append(f"! {reserve}")
    lines += ["", _footer_plain(summary)]
    return "\n".join(lines)


# --- découpage multi-messages ------------------------------------------------------------


def _clean_markers(text: str) -> str:
    return text.replace(SECTION_MARKER + "\n", "").replace(SECTION_MARKER, "")


def _prefix_len(index: int, total: int) -> int:
    """Longueur du préfixe « (i/N) » qui sera ajouté par le notifier (à réserver ici)."""
    if total <= 1:
        return 0
    return len(f"({index}/{total}) \n\n")


def split_markdown_v2_parts(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Découpe un rendu détaillé aux frontières de section (marqueur SECTION_MARKER).

    Retourne une liste de morceaux ≤ `limit` caractères une fois préfixés de « (i/N) ». Les
    marqueurs sont retirés dans la sortie. Si le texte tient d'une pièce, un seul élément
    est retourné (sans préfixe). Si une section unique dépasse encore `limit`, elle est
    envoyée telle quelle (Telegram acceptera de la refuser plutôt que de couper au milieu
    d'une puce échappée : le cas est loggé côté appelant si besoin)."""
    stripped = _clean_markers(text)
    if len(stripped) <= limit:
        return [stripped]

    # Découpage sur les marqueurs SECTION_MARKER, sinon sur les frontières de sections
    # informelles (double newline). Chaque « bloc » est un groupe cohérent qu'on ne coupe
    # jamais au milieu.
    if SECTION_MARKER in text:
        blocks = [_clean_markers(b).strip("\n") for b in text.split(SECTION_MARKER)]
    else:
        blocks = [b.strip("\n") for b in text.split("\n\n")]
    blocks = [b for b in blocks if b]

    # Assemble les blocs en morceaux respectant la limite. On accumule tant que ça rentre,
    # puis on flushe. Un bloc unique plus grand que la limite est émis seul (rare).
    # On fait une première estimation à N=1 puis on itère jusqu'à convergence : le préfixe
    # « (i/N) » consomme quelques caractères qu'il faut anticiper.
    def _assemble(total_hint: int) -> list[str]:
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for block in blocks:
            candidate_len = current_len + (2 if current else 0) + len(block)
            budget = limit - _prefix_len(len(chunks) + 1, max(total_hint, 1))
            if candidate_len <= budget:
                current.append(block)
                current_len = candidate_len
                continue
            if current:
                chunks.append(current)
            current = [block]
            current_len = len(block)
        if current:
            chunks.append(current)
        return ["\n\n".join(c) for c in chunks]

    hint = 1
    for _ in range(4):
        assembled = _assemble(hint)
        if len(assembled) == hint:
            return assembled
        hint = len(assembled)
    return _assemble(hint)


def split_plain_parts(text: str, limit: int) -> list[str]:
    """Même logique pour WhatsApp (pas de marqueur, on coupe sur double-newline)."""
    if len(text) <= limit:
        return [text]
    blocks = [b.strip("\n") for b in text.split("\n\n") if b.strip()]

    def _assemble(total_hint: int) -> list[str]:
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for block in blocks:
            candidate_len = current_len + (2 if current else 0) + len(block)
            budget = limit - _prefix_len(len(chunks) + 1, max(total_hint, 1))
            if candidate_len <= budget:
                current.append(block)
                current_len = candidate_len
                continue
            if current:
                chunks.append(current)
            current = [block]
            current_len = len(block)
        if current:
            chunks.append(current)
        return ["\n\n".join(c) for c in chunks]

    hint = 1
    for _ in range(4):
        assembled = _assemble(hint)
        if len(assembled) == hint:
            return assembled
        hint = len(assembled)
    return _assemble(hint)


def numbered(parts: list[str], escape: bool = False) -> list[str]:
    """Préfixe chaque partie de « (i/N) » quand il y en a plusieurs.
    `escape=True` (Telegram MarkdownV2) : les caractères sensibles du préfixe sont échappés."""
    if len(parts) <= 1:
        return list(parts)
    prefixed: list[str] = []
    for i, part in enumerate(parts, start=1):
        head = f"({i}/{len(parts)}) "
        if escape:
            head = escape_markdown_v2(head)
        prefixed.append(f"{head}\n\n{part}")
    return prefixed


def render_markdown(summary: Summary, video: Video, label: str = "") -> str:
    """Résumé en Markdown brut (non échappé), utilisé pour l'archivage NotebookLM.
    Reprend l'intégralité du résumé quel que soit son niveau (détails compris)."""
    header_meta = " · ".join(x for x in (video.channel, label) if x)
    lines = [f"# {summary.title}"]
    if header_meta:
        lines.append(f"_{header_meta}_")
    lines += ["", f"**TL;DR** — {summary.tldr}", ""]
    if summary.detail == "detaille" and summary.sections:
        for sec in summary.sections:
            stamp = format_timestamp(sec.seconds)
            link = timestamp_url(video.video_id, sec.seconds)
            lines += ["", f"## {sec.title} — [{stamp}]({link})", ""]
            for bullet in sec.bullets:
                lines.append(f"- {bullet}")
        if summary.citations:
            lines += ["", "## Citations", ""]
            for cit in summary.citations:
                stamp = format_timestamp(cit.seconds)
                link = timestamp_url(video.video_id, cit.seconds)
                lines.append(f"- [{stamp}]({link}) — « {cit.text} »")
        if summary.actions:
            lines += ["", "## À faire", ""]
            for action in summary.actions:
                lines.append(f"- {action}")
        if summary.reserves:
            lines += ["", "## Réserves", ""]
            for reserve in summary.reserves:
                lines.append(f"- {reserve}")
    else:
        lines += ["## Points clés", ""]
        for kp in summary.key_points:
            stamp = format_timestamp(kp.seconds)
            link = timestamp_url(video.video_id, kp.seconds)
            lines.append(f"- [{stamp}]({link}) — {kp.text}")
        if summary.actions:
            lines += ["", "## À faire", ""]
            for action in summary.actions:
                lines.append(f"- {action}")
        if summary.why_it_matters:
            lines += ["", f"**Pourquoi ça compte** — {summary.why_it_matters}"]
    lines += ["", f"_⏱ Lecture : {summary.reading_time_minutes} min_"]
    return "\n".join(lines)


def render_no_transcript(video: Video, markdown_v2: bool) -> str:
    text = f"⚠️ Pas de transcription disponible pour « {video.title} » — vidéo abandonnée."
    if markdown_v2:
        return f"{escape_markdown_v2(text)}\n{escape_markdown_v2(video.url)}"
    return f"{text}\n{video.url}"
