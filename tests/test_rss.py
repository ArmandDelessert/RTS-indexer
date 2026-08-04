"""Tests de la source RSS, sur de faux flux servis par un transport simulé."""

import httpx
import pytest

from rts_indexer import config
from rts_indexer.sources import rss

#: Un flux réaliste : les liens portent le `?rts_source=rss_t` que rts.ch ajoute
#: réellement, et que robots.txt interdit — il ne doit jamais survivre à la
#: normalisation.
FLUX_SANTE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Santé</title>
    <link>https://www.rts.ch/info/sante/?rts_source=rss</link>
    <item>
      <title>Premier</title>
      <link>https://www.rts.ch/info/sante/2026/article/premier-1.html?rts_source=rss_t</link>
      <description><![CDATA[<a href="https://www.rts.ch/autre/-2.html">bruit</a>]]></description>
    </item>
    <item>
      <title>Second</title>
      <link>https://www.rts.ch/info/sante/2026/article/second-2.html?rts_source=rss_t</link>
    </item>
  </channel>
</rss>
"""

FLUX_SPORT = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <link>https://www.rts.ch/sport/football/2026/article/troisieme-3.html</link>
    </item>
    <item>
      <link>https://www.srf.ch/sport/hors-perimetre.html</link>
    </item>
    <item>
      <link>https://img.rts.ch/vignette.image</link>
    </item>
  </channel>
</rss>
"""


def _transport(flux: dict[str, str], log: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        body = flux.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode("utf-8"))

    return httpx.MockTransport(handler)


def test_feed_urls_construit_depuis_les_rubriques():
    urls = rss.feed_urls(("info/sante", "meteo"))
    assert urls == [
        "https://www.rts.ch/info/sante/?format=rss/news",
        "https://www.rts.ch/meteo/?format=rss/news",
    ]


def test_feed_urls_par_defaut_couvre_toutes_les_rubriques_configurees():
    assert len(rss.feed_urls()) == len(config.RSS_FEEDS)


def test_collecte_les_liens_des_items():
    transport = _transport({"/info/sante/": FLUX_SANTE})
    assert rss.collect(("info/sante",), transport=transport) == [
        "https://www.rts.ch/info/sante/2026/article/premier-1.html",
        "https://www.rts.ch/info/sante/2026/article/second-2.html",
    ]


def test_la_query_rts_source_est_supprimee():
    """robots.txt interdit `?rts_source=` : seule l'URL canonique est indexée."""
    transport = _transport({"/info/sante/": FLUX_SANTE})
    urls = rss.collect(("info/sante",), transport=transport)
    assert all("rts_source" not in url for url in urls)
    assert all("?" not in url for url in urls)


def test_le_lien_du_channel_n_est_pas_collecte():
    """Seuls les `item/link` comptent : le lien du channel est la rubrique
    elle-même, déjà connue, et le ramasser brouillerait le compte des
    nouveautés."""
    transport = _transport({"/info/sante/": FLUX_SANTE})
    urls = rss.collect(("info/sante",), transport=transport)
    assert "https://www.rts.ch/info/sante/" not in urls


def test_le_bruit_de_la_description_est_ignore():
    transport = _transport({"/info/sante/": FLUX_SANTE})
    urls = rss.collect(("info/sante",), transport=transport)
    assert "https://www.rts.ch/autre/-2.html" not in urls


def test_hors_perimetre_et_non_html_ecartes():
    transport = _transport({"/sport/football/": FLUX_SPORT})
    assert rss.collect(("sport/football",), transport=transport) == [
        "https://www.rts.ch/sport/football/2026/article/troisieme-3.html",
    ]


def test_doublons_entre_flux_fusionnes():
    """Une même actualité paraît dans la rubrique fille et dans `toute-info` :
    l'ordre de première apparition est conservé, sans répétition."""
    transport = _transport({"/info/sante/": FLUX_SANTE, "/info/toute-info/": FLUX_SANTE})
    urls = rss.collect(("info/sante", "info/toute-info"), transport=transport)
    assert len(urls) == len(set(urls)) == 2


def test_un_flux_absent_ne_compromet_pas_les_autres():
    """Une rubrique retirée du site répond 404 ; les autres doivent aboutir."""
    transport = _transport({"/sport/football/": FLUX_SPORT})
    urls = rss.collect(("info/disparue", "sport/football"), transport=transport)
    assert urls == ["https://www.rts.ch/sport/football/2026/article/troisieme-3.html"]


def test_un_flux_illisible_est_saute(caplog):
    transport = _transport({"/info/sante/": "<rss><channel><item></rss"})
    # `recover=True` répare ce qu'il peut ; l'essentiel est qu'aucune exception
    # ne remonte et que la collecte se termine.
    assert rss.collect(("info/sante",), transport=transport) == []


@pytest.mark.parametrize("path", config.RSS_FEEDS)
def test_les_rubriques_configurees_sont_des_chemins_relatifs(path):
    """Un chemin absolu ou traînant un slash produirait une URL de flux
    malformée une fois injecté dans le gabarit."""
    assert not path.startswith("/")
    assert not path.endswith("/")
