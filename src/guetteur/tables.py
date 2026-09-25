"""Tableaux texte alignés pour la CLI (status, health)."""

from __future__ import annotations

from collections.abc import Sequence


def truncate(text: str | None, width: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def render_rows(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(row) for row in rows]
    return "\n".join(out)
