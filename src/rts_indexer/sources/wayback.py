"""Source « wayback » : l'archive historique via l'API CDX d'Internet Archive.

C'est la seule source capable de remonter le temps. Le crawl ne voit que la
première page de chaque rubrique (``robots.txt`` interdit la pagination) et les
sitemaps ne référencent aucun article : sans Wayback, l'index reste cantonné à
ce qui est aujourd'hui visible depuis les pages de listing.

**Le découpage se fait par page CDX, pas par année.** Le plan initial prévoyait
une tranche annuelle via ``from``/``to`` ; c'est incompatible avec la
pagination, et silencieusement — pas par une erreur. ``page=N`` partitionne
l'index *complet* du domaine en blocs de clés d'URL, et les filtres (dont
``from``/``to``) ne s'appliquent qu'ensuite, à l'intérieur du bloc retenu. La
page 0 filtrée sur 2013 renvoyait donc 1 ligne, là où la même requête sans
``page`` en renvoie 8342. Paginer le domaine entier est à la fois plus simple
et correct : l'objectif est de toute façon d'obtenir toutes les URLs, quelle
que soit leur date.
"""

from __future__ import annotations

import logging

from .. import config
from ..store import Store
from .cdx import CdxClient, Segment

log = logging.getLogger(__name__)

CURSOR_FILE = "wayback_cursor.json"

#: Une seule tranche : l'intégralité du domaine, parcourue page par page.
#: Le nom sert de clé de curseur, d'où sa stabilité.
SEGMENT = Segment(key="domaine", params={})


def collect(
    store: Store,
    *,
    max_pages: int = 0,
    cache_dir=None,
    transport=None,
) -> CdxClient:
    """Alimente le store depuis Wayback. Retourne le client, pour ses compteurs.

    ``max_pages`` borne le run : un parcours complet représente plus de 1500
    pages à une dizaine de secondes chacune, soit plusieurs heures. Le curseur
    étant persisté après chaque page, on peut avancer par tranches successives.
    """
    client = CdxClient(
        config.WAYBACK_CDX, CURSOR_FILE, cache_dir=cache_dir, transport=transport
    )
    client.load_cursor()
    client.added = 0

    if client.is_done(SEGMENT):
        log.info("archive déjà entièrement parcourue (curseur: %s)", CURSOR_FILE)
        return client

    depart = client.cursor.get(SEGMENT.key, 0)
    log.info("reprise à la page %d%s", depart, f", budget {max_pages} pages" if max_pages else "")

    try:
        with client.client() as http:
            for url in client.iter_segment(http, SEGMENT, max_pages=max_pages):
                client.added += store.add(url)
    finally:
        client.save_cursor()

    return client
