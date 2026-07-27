"""Source « sitemap » : les sitemaps XML déclarés dans robots.txt.

Attention à ne pas surestimer cette source. Les sitemaps de rts.ch ne
référencent que les **pages de rubrique** — ``pages/info.xml`` ne contient que
128 URLs, et aucun article. C'est donc une graine fiable et peu coûteuse pour le
crawl, pas une source d'exhaustivité.
"""

from __future__ import annotations

import gzip
import logging

from lxml import etree

from .. import config, net, urlnorm

log = logging.getLogger(__name__)

#: Les sitemaps de rts.ch déclarent le namespace sitemaps.org ; on interroge
#: sans namespace pour rester tolérant aux variations.
_LOC = "//*[local-name()='loc']/text()"
_SITEMAP_LOC = "/*[local-name()='sitemapindex']/*[local-name()='sitemap']/*[local-name()='loc']/text()"


def _parse(content: bytes) -> etree._Element | None:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        return etree.fromstring(content, parser=etree.XMLParser(recover=True, resolve_entities=False))
    except etree.XMLSyntaxError as exc:
        log.warning("XML illisible: %s", exc)
        return None


def _fetch_tree(http, url: str) -> etree._Element | None:
    response = net.get(http, url)
    if response is None:
        return None
    return _parse(response.content)


def collect(indexes: tuple[str, ...] | None = None) -> list[str]:
    """Retourne les URLs canoniques de tous les sitemaps, doublons exclus.

    Un sitemap index est suivi d'un niveau ; au-delà, on s'arrête (aucun besoin
    constaté sur rts.ch et cela borne le travail).
    """
    indexes = indexes if indexes is not None else config.SITEMAP_INDEXES + config.SITEMAP_EXTRA
    seen: dict[str, None] = {}

    with net.client() as http:
        for index_url in indexes:
            tree = _fetch_tree(http, index_url)
            if tree is None:
                continue

            children = [str(loc).strip() for loc in tree.xpath(_SITEMAP_LOC)]
            if children:
                log.info("%s: sitemap index, %d enfants", index_url, len(children))
                targets = children
            else:
                targets = []
                found = urlnorm.normalize_many(tree.xpath(_LOC))
                for url in found:
                    seen.setdefault(url, None)
                log.info("%s: %d URLs retenues", index_url, len(found))

            for child_url in targets:
                child = _fetch_tree(http, child_url)
                if child is None:
                    continue
                found = urlnorm.normalize_many(child.xpath(_LOC))
                for url in found:
                    seen.setdefault(url, None)
                log.info("  %s: %d URLs retenues", child_url, len(found))

    return list(seen)
