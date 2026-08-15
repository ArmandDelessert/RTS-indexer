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


#: Nombre de lots avant d'abandonner définitivement un élément.
_BATCH_ROUNDS = 5
#: Base du délai entre deux lots (secondes), doublée à chaque tour.
_BATCH_DELAY = 1.0


def retry_many(items: list[tuple[Callable[[], None], str]]) -> list[str]:
    """Exécute plusieurs actions indépendantes, en regroupant les nouvelles
    tentatives par lots plutôt qu'en épuisant les essais un par un.

    Pensé pour l'obstacle qui n'est pas propre à un fichier précis mais à un
    scan externe (antivirus, indexeur) qui verrouille ce qu'il est en train
    d'examiner : marteler la même entrée cinq fois en trois secondes ne sert
    à rien si le scan met plus longtemps que ça. Regrouper par lots laisse
    le temps de passer, et sur 1'000+ éléments, le coût total de l'attente
    devient celui de quelques tours (secondes), pas celui de mille boucles
    de nouvelles tentatives individuelles (potentiellement des heures).

    Ne lève jamais pour un verrou : retourne les descriptions de ce qui
    échoue encore après tous les tours, à l'appelant de décider quoi en
    faire. Un dossier vide non supprimé n'est pas fatal, mais ne doit
    jamais disparaître en silence — voir l'incident réel qui a motivé ceci :
    ``_remonter_vides`` avalait ces échecs sans le moindre signalement,
    donnant l'illusion d'une progression pendant 9h50 pour un taux de
    réussite de 0 % sur les dossiers déjà tentés.
    """
    restants = list(items)
    for tour in range(1, _BATCH_ROUNDS + 1):
        echecs: list[tuple[Callable[[], None], str]] = []
        for action, desc in restants:
            try:
                action()
            except PermissionError:
                echecs.append((action, desc))
        if not echecs:
            return []
        restants = echecs
        if tour < _BATCH_ROUNDS:
            log.warning(
                "%d élément(s) verrouillé(s), nouveau lot dans %.0fs (tour %d/%d)",
                len(echecs),
                _BATCH_DELAY * tour,
                tour,
                _BATCH_ROUNDS,
            )
            time.sleep(_BATCH_DELAY * tour)
    return [desc for _, desc in restants]
