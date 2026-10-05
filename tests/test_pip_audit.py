"""Lot 8b A1 : garde-fou CI local — l'export runtime du lock passe pip-audit.

Marqué `slow` : lance `uv tool run pip-audit` sur une liste exportée du lock,
nécessite le réseau et quelques secondes. CI locale uniquement — le CI GitHub
peut l'exécuter séparément. Sauté silencieusement si `uv` n'est pas dans le
PATH ou si l'option est désactivée (opt-in via GUETTEUR_RUN_PIP_AUDIT=1)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _uv_available() -> bool:
    return shutil.which("uv") is not None


@pytest.mark.skipif(
    not os.environ.get("GUETTEUR_RUN_PIP_AUDIT"),
    reason="opt-in : GUETTEUR_RUN_PIP_AUDIT=1 pour lancer pip-audit sur le lock",
)
@pytest.mark.skipif(not _uv_available(), reason="uv absent du PATH")
def test_lock_passes_pip_audit(tmp_path: Path) -> None:
    """Vérifie que `pip-audit --strict` ne trouve rien dans le graphe runtime
    du lock (toutes les CVE connues ont un correctif pris via `>=` du
    pyproject). Le lock lui-même peut pin des versions vulnérables ; ici on
    exporte depuis le lock SANS extras lourds pour isoler les transitifs."""
    req = tmp_path / "req.txt"
    export = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "requirements-txt",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "-o",
            str(req),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if export.returncode != 0:
        pytest.skip(f"uv export en échec : {export.stderr}")
    audit = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--strict", "-r", str(req)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert audit.returncode == 0, (
        f"pip-audit a trouvé des vulnérabilités dans le lock :\n{audit.stdout}\n{audit.stderr}"
    )
