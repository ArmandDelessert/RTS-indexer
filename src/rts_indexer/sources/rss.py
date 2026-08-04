"""Source « rss » : les flux RSS des rubriques éditoriales.

Cette source existe pour combler un trou structurel des autres. Le ``crawl`` ne
voit que la première page de chaque rubrique — ``robots.txt`` interdit
``/*/page/`` et ``/*?*page=``, la pagination est donc hors d'atteinte. Un
article publié puis chassé de cette première page entre deux exécutions n'est
jamais vu, et rien ne le rattrape ensuite : le ``sitemap`` ne liste que des
rubriques, et Wayback n'archive qu'une fraction des articles, avec des mois de
retard. Le flux RSS est la seule fenêtre qui s'ouvre sur les publications au fil
de l'eau.

C'est aussi la source la moins chère du projet : une vingtaine de requêtes,
quelques dizaines de kilo-octets. Sa contrainte n'est pas le coût mais la
**fréquence** — voir :data:`config.RSS_FEEDS` pour la fenêtre mesurée.

Sur ``robots.txt`` : le chemin ``/flux/`` est interdit, mais ce n'est pas celui
qu'on emprunte — la forme ``<rubrique>/?format=rss/news`` passe par un paramètre
de requête qui, lui, n'est pas listé. En revanche les liens *contenus* dans le
flux portent ``?rts_source=rss_t``, qui est bel et bien interdit : rien ne les
suit jamais, et :func:`urlnorm.normalize` supprime la query, de sorte que seule
l'URL canonique de l'article est indexée.
"""

from __future__ import annotations

import logging

import httpx
from lxml import etree

from .. import config, net, urlnorm

log = logging.getLogger(__name__)

#: Les flux de rts.ch sont du RSS 2.0 sans namespace. On interroge tout de même
#: par ``local-name()``, comme pour les sitemaps : un flux qui passerait un jour
#: à Atom ou gagnerait un namespace continuerait d'être lu.
_ITEM_LINK = "//*[local-name()='item']/*[local-name()='link']/text()"


def _parse(content: bytes) -> etree._Element | None:
    try:
        return etree.fromstring(
            content, parser=etree.XMLParser(recover=True, resolve_entities=False)
        )
    except etree.XMLSyntaxError as exc:
        log.warning("flux illisible: %s", exc)
        return None


def feed_urls(paths: tuple[str, ...] | None = None) -> list[str]:
    """URLs des flux à interroger, construites depuis les rubriques configurées."""
    paths = paths if paths is not None else config.RSS_FEEDS
    return [config.RSS_FEED_TEMPLATE.format(path=path) for path in paths]


def collect(
    paths: tuple[str, ...] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Retourne les URLs canoniques citées par les flux, doublons exclus.

    Un flux inaccessible est journalisé et sauté : les rubriques sont
    indépendantes, et perdre les vingt autres parce que l'une répond 500 serait
    absurde pour une source dont tout l'intérêt est de tourner souvent et sans
    surveillance.

    ``transport`` n'existe que pour les tests, qui servent ainsi de faux flux
    sans toucher au réseau.
    """
    seen: dict[str, None] = {}
    kwargs = {"transport": transport} if transport is not None else {}

    with net.client(**kwargs) as http:
        for feed_url in feed_urls(paths):
            response = net.get(http, feed_url)
            if response is None:
                continue
            tree = _parse(response.content)
            if tree is None:
                continue
            found = urlnorm.normalize_many(tree.xpath(_ITEM_LINK))
            for url in found:
                seen.setdefault(url, None)
            log.info("%s: %d URLs retenues", feed_url, len(found))

    return list(seen)
