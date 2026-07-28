"""Tests de la source Wayback et de la mécanique CDX commune.

Les points sensibles : la reprise sur curseur (un parcours dure des heures,
une interruption ne doit coûter qu'une page) et le backoff (ces archives
limitent le débit de façon soutenue, abandonner trop tôt ferait échouer la
quasi-totalité des runs longs).
"""

import json

import httpx
import pytest

from rts_indexer import config
from rts_indexer.sources import wayback
from rts_indexer.sources.cdx import CdxClient, Segment
from rts_indexer.store import Store


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    monkeypatch.setattr(config, "CDX_DELAY", 0.0)
    monkeypatch.setattr(config, "CDX_BACKOFF", 0.0)


def _pages(pages: list[list[str]]):
    """Faux CDX paginé : `pages[n]` = lignes brutes de la page n."""
    appels: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 0))
        appels.append(page)
        corps = "\n".join(pages[page]) if page < len(pages) else ""
        return httpx.Response(200, text=corps)

    return httpx.MockTransport(handler), appels


def _cursor(cache):
    return json.loads((cache / "wayback_cursor.json").read_text())


# -- découpage ---------------------------------------------------------------


def test_pagination_sans_filtre_d_annee():
    """`page=` et `from`/`to` sont incompatibles : la pagination partitionne
    l'index complet du domaine et les filtres ne s'appliquent qu'ensuite, à
    l'intérieur du bloc retenu. Constaté en réel : page 0 filtrée sur 2013
    renvoyait 1 ligne, la même requête sans `page` en renvoyait 8342."""
    assert wayback.SEGMENT.params == {}


# -- collecte ----------------------------------------------------------------


def test_collecte_et_pagination(tmp_path):
    transport, appels = _pages([
        ["http://www.rts.ch/info/suisse/a-1.html", "http://www.rts.ch/info/suisse/b-2.html"],
        ["http://www.rts.ch/info/culture/c-3.html"],
    ])
    store = Store(tmp_path / "data")
    client = wayback.collect(
        store, cache_dir=tmp_path / "cache", transport=transport
    )

    urls = {u for u, _ in store.urls()}
    assert "https://www.rts.ch/info/suisse/a-1.html" in urls
    assert "https://www.rts.ch/info/culture/c-3.html" in urls
    # Pages 0 et 1 pleines, puis les pages vides jusqu'à la tolérance.
    assert appels == [0, 1, 2, 3, 4]
    assert client.added == 3


def test_le_bruit_cdx_est_filtre(tmp_path):
    """Wayback archive des fragments de JavaScript comme s'ils étaient des
    URLs ; ils ne doivent pas polluer l'index."""
    transport, _ = _pages([[
        "http://www.rts.ch/info/suisse/vrai-1.html",
        "http://www.rts.ch/;path=/;/",
        "http://www.rts.ch/;a.style.position=",
        'http://www.rts.ch/"http://www.rts.ch/x.image"',
        "http://img.rts.ch/2012/image/abc.image",
    ]])
    store = Store(tmp_path / "data")
    wayback.collect(store, cache_dir=tmp_path / "cache", transport=transport)
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/info/suisse/vrai-1.html"}


def test_page_vide_intermediaire_n_arrete_pas_le_parcours(tmp_path):
    """Les filtres s'appliquant après le découpage en blocs, une page peut être
    vide alors que les suivantes ont des données. S'arrêter à la première
    tronquerait silencieusement l'archive."""
    transport, appels = _pages([
        ["http://www.rts.ch/a-1.html"],
        [],  # trou
        [],
        ["http://www.rts.ch/b-2.html"],
    ])
    store = Store(tmp_path / "data")
    wayback.collect(store, cache_dir=tmp_path / "cache", transport=transport)

    assert {u for u, _ in store.urls()} == {
        "https://www.rts.ch/a-1.html",
        "https://www.rts.ch/b-2.html",
    }


# -- reprise -----------------------------------------------------------------


def test_curseur_reprend_ou_il_s_est_arrete(tmp_path):
    cache = tmp_path / "cache"
    pages = [["http://www.rts.ch/a-1.html"], ["http://www.rts.ch/b-2.html"]]

    transport, appels = _pages(pages)
    wayback.collect(
        Store(tmp_path / "d1"), max_pages=1, cache_dir=cache, transport=transport
    )
    assert appels == [0]
    assert _cursor(cache)[wayback.SEGMENT.key] == 1

    # Second run : reprend à la page 1, sans refaire la 0.
    transport, appels = _pages(pages)
    store = Store(tmp_path / "d2")
    wayback.collect(store, max_pages=1, cache_dir=cache, transport=transport)
    assert appels == [1]
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/b-2.html"}


def test_archive_terminee_n_est_pas_reparcourue(tmp_path):
    cache = tmp_path / "cache"
    transport, _ = _pages([["http://www.rts.ch/a-1.html"]])
    wayback.collect(Store(tmp_path / "d1"), cache_dir=cache, transport=transport)
    assert _cursor(cache)[wayback.SEGMENT.key] is None

    transport, appels = _pages([["http://www.rts.ch/a-1.html"]])
    wayback.collect(Store(tmp_path / "d2"), cache_dir=cache, transport=transport)
    assert appels == []  # rien à refaire


def test_curseur_corrompu_repart_du_debut(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "wayback_cursor.json").write_bytes(b"{pas du JSON")

    transport, appels = _pages([["http://www.rts.ch/a-1.html"]])
    wayback.collect(Store(tmp_path / "data"), cache_dir=cache, transport=transport)
    assert appels[0] == 0


# -- robustesse réseau -------------------------------------------------------


def test_backoff_sur_limitation_de_debit(tmp_path):
    """Un 429 doit être retenté, pas abandonné : ces archives limitent le
    débit de façon soutenue (constaté en réel, connexion coupée par l'hôte)."""
    essais = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        essais["n"] += 1
        if essais["n"] <= 2:
            return httpx.Response(429)
        if essais["n"] == 3:
            return httpx.Response(200, text="http://www.rts.ch/a-1.html")
        return httpx.Response(200, text="")

    store = Store(tmp_path / "data")
    wayback.collect(
        store, cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler)
    )
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/a-1.html"}


def test_echec_durable_laisse_la_tranche_reprenable(tmp_path):
    """Après épuisement des essais, le curseur doit rester sur la page en
    échec pour la reprendre au prochain run, pas la sauter."""
    cache = tmp_path / "cache"
    wayback.collect(
        Store(tmp_path / "data"),
        cache_dir=cache,
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    curseur = _cursor(cache)
    assert curseur.get(wayback.SEGMENT.key, 0) == 0  # ni terminée (None), ni avancée


def test_404_traite_comme_tranche_vide(tmp_path):
    """Common Crawl répond 404 quand un motif n'a aucune capture : c'est une
    absence de données, pas une erreur."""
    client = CdxClient(
        "https://exemple.test/cdx", "curseur.json",
        cache_dir=tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    with client.client() as http:
        assert list(client.iter_segment(http, Segment("x", {}))) == []
    assert client.cursor["x"] is None  # tranche close, pas en échec
