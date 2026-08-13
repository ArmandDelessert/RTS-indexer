"""Résilience aux verrous transitoires du système de fichiers.

Le dépôt vit sous OneDrive (constaté dans cette session : un ``.pytest_cache``
a renvoyé "Accès refusé" pendant une synchronisation). OneDrive verrouille
brièvement un fichier le temps de le synchroniser ; un run de plusieurs heures
qui tombe dessus pendant une écriture ne doit pas s'arrêter là pour autant.
Ces enveloppes ré-essaient avec un backoff court avant d'abandonner pour de bon.

Seul ``PermissionError`` est retenté : un ``FileNotFoundError`` ou un disque
plein sont des erreurs réelles qu'il ne faut pas masquer derrière des essais
répétés.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_ATTEMPTS = 5
_BASE_DELAY = 0.3


def retry(action: Callable[[], T], *, desc: str) -> T:
    """Exécute ``action``, en ré-essayant sur ``PermissionError`` transitoire."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return action()
        except PermissionError:
            if attempt == _ATTEMPTS:
                raise
            log.warning("%s: verrouillé, nouvelle tentative (%d/%d)", desc, attempt, _ATTEMPTS)
            time.sleep(_BASE_DELAY * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def write_text(path: Path, text: str) -> None:
    retry(
        lambda: path.write_text(text, encoding="utf-8", newline="\n"),
        desc=f"écriture de {path}",
    )


def read_text(path: Path) -> str:
    return retry(lambda: path.read_text(encoding="utf-8"), desc=f"lecture de {path}")


def unlink(path: Path) -> None:
    retry(lambda: path.unlink(missing_ok=True), desc=f"suppression de {path}")


def rmdir(path: Path) -> None:
    retry(path.rmdir, desc=f"suppression du dossier {path}")
