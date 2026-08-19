"""Source « fichier » : des URLs relevées à la main, dans un simple ``.txt``.

Toutes les autres sources découvrent des URLs par elles-mêmes. Celle-ci sert
aux trouvailles occasionnelles qu'aucune ne ramène — typiquement des pages
repérées via un moteur de recherche, dont le crawl ne voit jamais le lien
parce que plus aucune page vivante ne pointe vers elles.

D'où la vérification optionnelle avant l'ajout : contrairement à un sitemap ou
à un flux RSS, qui viennent de rts.ch et sont par construction à jour, une
liste tapée à la main peut contenir des URLs mortes, des variantes obsolètes
qui redirigent ailleurs, ou de simples fautes de frappe.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import urlnorm

log = logging.getLogger(__name__)


def lire(chemin: Path | str) -> tuple[list[str], list[str]]:
    """Lit un fichier d'URLs, une par ligne.

    Les lignes vides et celles commençant par ``#`` sont ignorées, pour
    pouvoir annoter le fichier (d'où vient la trouvaille, à quelle date).

    Retourne ``(retenues, rejetées)`` : les premières sont canoniques et dans
    le périmètre, les secondes ont échoué à :func:`urlnorm.normalize` et sont
    rendues telles quelles pour pouvoir être signalées — une faute de frappe
    doit se voir, pas disparaître en silence.
    """
    contenu = Path(chemin).read_text(encoding="utf-8")
    retenues: dict[str, None] = {}
    rejetees: list[str] = []

    for ligne in contenu.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        url = urlnorm.normalize(ligne)
        if url is None:
            rejetees.append(ligne)
        else:
            retenues.setdefault(url, None)

    return list(retenues), rejetees
