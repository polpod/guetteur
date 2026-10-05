"""Lecteur d'article web.

Extraction du contenu principal :
- scrapling si installé (configurable, voir pyproject extra `scrapling`) ;
- repli stdlib `html.parser` : on parcourt le DOM, on supprime scripts et
  styles, on prend l'`<article>` ou le `<main>` quand il existe, sinon la
  concaténation des `<p>` les plus denses. Suffisant pour résumer.

Paywall : si le body texte extrait fait moins de ~500 caractères MAIS que la
page porte un indice de paywall (meta `isAccessibleForFree=false`, mots-clés
`subscribe` / `paywall` dans le HTML), on résume depuis les métadonnées (title,
description) et on mentionne explicitement « paywall détecté » dans l'extras.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from html.parser import HTMLParser

from guetteur.items import LinkContent
from guetteur.sources.liens.net import LinkFetchError, safe_fetch

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 50_000
_PAYWALL_HINTS = re.compile(
    r'isAccessibleForFree"\s*:\s*false|cx:["\']paywall["\']|paywall|data-paywall|'
    r"abonnez-vous|réservé aux abonnés",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    """Petit extracteur maison :
    - saute script/style/nav/aside/header/footer/form,
    - privilégie <article>/<main> si présents,
    - sinon concatène tout le texte visible.
    """

    _SKIP = frozenset(
        {"script", "style", "nav", "aside", "header", "footer", "form", "noscript"}
    )
    _MAIN = frozenset({"article", "main"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth = 0
        self._in_main_mode = False
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._meta: dict[str, str] = {}

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    @property
    def meta(self) -> dict[str, str]:
        return self._meta

    def text(self) -> str:
        chosen = self._main_parts if self._main_parts else self._parts
        text = "\n".join(line.strip() for line in "".join(chosen).splitlines())
        collapsed: list[str] = []
        prev_blank = False
        for line in text.splitlines():
            if not line:
                if prev_blank:
                    continue
                prev_blank = True
                collapsed.append("")
            else:
                prev_blank = False
                collapsed.append(line)
        return "\n".join(collapsed).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag in self._MAIN:
            self._main_depth += 1
            self._in_main_mode = True
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            d = dict(attrs)
            key = (d.get("name") or d.get("property") or "").lower()
            value = d.get("content") or ""
            if key and value:
                self._meta[key] = value
            return
        if tag in {"br"}:
            self._parts.append("\n")
            if self._in_main_mode:
                self._main_parts.append("\n")
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div"}:
            self._parts.append("\n")
            if self._in_main_mode:
                self._main_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self._MAIN:
            self._main_depth = max(0, self._main_depth - 1)
            if self._main_depth == 0:
                self._in_main_mode = False
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div"}:
            self._parts.append("\n")
            if self._in_main_mode and self._main_depth > 0:
                self._main_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._parts.append(data)
        if self._in_main_mode and self._main_depth > 0:
            self._main_parts.append(data)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _try_scrapling(html: str, url: str) -> tuple[str, str, dict[str, str]] | None:
    """Si scrapling est installé, renvoie (title, text, meta). None sinon.
    Importé paresseusement : pas de dépendance obligatoire pour les tests."""
    try:
        from scrapling import Adaptor  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        adaptor = Adaptor(body=html, url=url, auto_match=False)
        title = adaptor.css_first("title")
        title_text = title.text if title is not None else ""
        main = adaptor.css_first("article") or adaptor.css_first("main") or adaptor.body
        text = main.get_all_text() if main is not None else ""
        meta: dict[str, str] = {}
        for tag in adaptor.css("meta[name], meta[property]"):
            key = (tag.attrib.get("name") or tag.attrib.get("property") or "").lower()
            value = tag.attrib.get("content", "")
            if key and value:
                meta[key] = value
        return (str(title_text).strip(), str(text).strip(), meta)
    except Exception as exc:  # scrapling peut lever n'importe quoi
        log.info("article.scrapling_failed", extra={"error": str(exc)})
        return None


class ArticleReader:
    def read(self, url: str) -> LinkContent:
        result = safe_fetch(url)
        if "html" not in result.content_type and "xml" not in result.content_type:
            raise LinkFetchError(
                f"Content-Type non HTML pour {url} : {result.content_type!r}"
            )
        html = result.text
        extras: dict[str, str] = {}

        extracted = _try_scrapling(html, result.url)
        if extracted is not None:
            title, text, meta = extracted
            extras["extractor"] = "scrapling"
        else:
            parser = _TextExtractor()
            parser.feed(html)
            title = parser.title
            text = parser.text()
            meta = parser.meta
            extras["extractor"] = "stdlib"

        if _PAYWALL_HINTS.search(html) and len(text) < 500:
            desc = meta.get("og:description") or meta.get("description") or ""
            text = f"[paywall détecté — résumé depuis les métadonnées]\n\n{desc}".strip()
            extras["paywall"] = "true"

        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n\n[tronqué à 50 000 caractères]"
            extras["truncated"] = "true"

        author = meta.get("article:author") or meta.get("author") or meta.get("og:site_name") or ""
        published = _parse_date(
            meta.get("article:published_time") or meta.get("og:published_time")
        )
        if not title:
            title = meta.get("og:title") or url
        return LinkContent(
            url=result.url,
            kind="article",
            title=title[:200],
            author=author[:120],
            published_at=published,
            text=text,
            extras=extras,
        )
