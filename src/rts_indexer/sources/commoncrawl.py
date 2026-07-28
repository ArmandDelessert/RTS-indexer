"""Source « commoncrawl » : l'archive de la fondation Common Crawl.

**Rendement mesuré sur www.rts.ch : nul.** Sur les crawls 2026-17, 2026-21,
2026-25 et 2025-30, l'index ne contient qu'une trentaine de captures pour
``www.rts.ch``, presque toutes du seul ``robots.txt`` — aucun article, aucune
rubrique. Common Crawl échantillonne le web plutôt qu'il ne l'aspire, et ce
site n'y figure pratiquement pas. L'archive historique utile vient de Wayback
(cf. :mod:`.wayback`), qui a rapporté 1'270 URLs en trois pages.

Ce module reste néanmoins en place pour deux raisons concrètes :

* Les **sous-domaines** y sont, eux, bien couverts — 110 pages d'
  ``avecvous.rts.ch`` pour le seul crawl 2026-17. Le jour où ``config.HOSTS``
  les inclut, la source devient immédiatement productive.
* La couverture d'un site évolue d'un crawl à l'autre ; ce qui est vide
  aujourd'hui ne l'était pas forcément en 2015 et ne le sera pas forcément
  demain.

Chaque *crawl* (« CC-MAIN-2026-25 », mensuel ou presque, 125 disponibles à ce
jour) a sa propre API CDX. Ils forment donc autant de tranches, parcourues de
la plus récente à la plus ancienne.

Le serveur d'index est notoirement instable : les 502 et 504 sont fréquents et
transitoires. Le backoff commun de :mod:`.cdx` s'en charge ; c'est même la
source qui l'a rendu indispensable.
"""

from __future__ import annotations

import json
import logging

from .. import config, fsutil, net
from ..store import Store
from .cdx import COMMONCRAWL, CdxClient, Segment

log = logging.getLogger(__name__)

CURSOR_FILE = "commoncrawl_cursor.json"
INDEXES_CACHE = "commoncrawl_indexes.json"


def indexes(cache_dir=None, transport=None, refresh: bool = False) -> list[dict]:
    """Liste des crawls disponibles, mise en cache.

    La liste ne bouge qu'une fois par mois : la retélécharger à chaque run
    n'apporterait rien et solliciterait un serveur déjà fragile.
    """
    path = (cache_dir or config.CACHE_DIR) / INDEXES_CACHE
    if not refresh and path.is_file():
        try:
            return json.loads(fsutil.read_text(path))
        except (PermissionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s illisible (%s), re-téléchargement", path, exc)

    with net.client(transport=transport, timeout=config.CDX_TIMEOUT) as http:
        response = net.get(http, config.COMMONCRAWL_INDEXES, delay=0)
    if response is None:
        log.error("liste des index Common Crawl injoignable")
        return []

    try:
        liste = response.json()
    except json.JSONDecodeError as exc:
        log.error("liste des index illisible: %s", exc)
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(path, json.dumps(liste, ensure_ascii=False))
    log.info("%d index Common Crawl disponibles", len(liste))
    return liste


def segments(cache_dir=None, transport=None, limit: int = 0) -> list[Segment]:
    """Une tranche par crawl, de la plus récente à la plus ancienne.

    Il n'est **pas** possible de cibler ``www.rts.ch`` spécifiquement : pywb
    canonicalise les URLs en SURT, ce qui supprime le préfixe ``www.``, si
    bien que ``url=www.rts.ch`` retombe exactement sur ``url=rts.ch``. La
    requête couvre donc toujours l'ensemble des sous-domaines, à charge pour
    :mod:`..urlnorm` d'écarter ceux hors périmètre.

    ``limit`` borne le nombre de crawls considérés : les 125 index couvrent
    plus d'une décennie et les plus anciens n'apportent quasiment rien sur un
    site qui a beaucoup évolué.
    """
    liste = indexes(cache_dir, transport)
    crawls = [e for e in liste if e.get("id") and e.get("cdx-api")]
    if limit > 0:
        crawls = crawls[:limit]
    return [
        Segment(key=entree["id"], params={}, base_url=entree["cdx-api"])
        for entree in crawls
    ]


def collect(
    store: Store,
    *,
    max_pages: int = 0,
    pages_per_index: int = 0,
    max_indexes: int = 0,
    cache_dir=None,
    transport=None,
) -> CdxClient:
    """Alimente le store depuis Common Crawl.

    ``pages_per_index`` permet de balayer largement plusieurs crawls plutôt que
    d'épuiser le plus récent : deux crawls voisins se recouvrent beaucoup, et
    les URLs vraiment nouvelles se trouvent plutôt en changeant de crawl qu'en
    creusant le même.
    """
    client = CdxClient(
        "",  # chaque tranche porte sa propre URL de base
        CURSOR_FILE,
        dialect=COMMONCRAWL,
        cache_dir=cache_dir,
        transport=transport,
    )
    client.load_cursor()
    client.added = 0

    tranches = [
        s
        for s in segments(cache_dir, transport, limit=max_indexes)
        if not client.is_done(s)
    ]
    if not tranches:
        log.info("tous les index disponibles sont déjà parcourus")
        return client
    log.info("%d index à parcourir, du plus récent au plus ancien", len(tranches))

    try:
        with client.client() as http:
            for segment in tranches:
                if max_pages and client.pages_fetched >= max_pages:
                    log.info("budget de %d pages atteint", max_pages)
                    break
                restant = max_pages - client.pages_fetched if max_pages else 0
                budget = (
                    min(restant, pages_per_index)
                    if restant and pages_per_index
                    else (restant or pages_per_index)
                )
                for url in client.iter_segment(http, segment, max_pages=budget):
                    client.added += store.add(url)
    finally:
        client.save_cursor()

    return client
