"""Tests de la source Common Crawl.

Le point sensible est le *dialecte* : Common Crawl est bâti sur pywb, qui
nomme ``url`` le champ que Wayback appelle ``original``. Demander le mauvais
nom ne provoque pas d'erreur — l'API renvoie silencieusement des ``-``, ce qui
donnerait un index vide sans que rien ne le signale.
"""

import json

import httpx
import pytest

from rts_indexer import config
from rts_indexer.sources import commoncrawl
from rts_indexer.sources.cdx import COMMONCRAWL, WAYBACK
from rts_indexer.store import Store

INDEXES = [
    {"id": "CC-MAIN-2026-25", "cdx-api": "https://index.test/CC-MAIN-2026-25-index"},
    {"id": "CC-MAIN-2026-21", "cdx-api": "https://index.test/CC-MAIN-2026-21-index"},
]

#: Clés de curseur : une par crawl.
CLE_25 = "CC-MAIN-2026-25"
CLE_21 = "CC-MAIN-2026-21"


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    monkeypatch.setattr(config, "CDX_DELAY", 0.0)
    monkeypatch.setattr(config, "CDX_BACKOFF", 0.0)
    monkeypatch.setattr(config, "COMMONCRAWL_INDEXES", "https://index.test/collinfo.json")


def _jsonl(urls):
    return "\n".join(json.dumps({"url": u, "mime": "text/html", "status": "200"}) for u in urls)


def _transport(par_index: dict[str, list[list[str]]], indexes=None):
    """Répond aussi à `showNumPages` au format pywb (objet JSON, pas l'entier
    nu de Wayback), sans compter ces requêtes dans `appels`."""
    appels: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("collinfo.json"):
            return httpx.Response(200, json=INDEXES if indexes is None else indexes)
        crawl_id = request.url.path.strip("/").replace("-index", "")
        if request.url.params.get("showNumPages") == "true":
            total = len(par_index.get(crawl_id, []))
            return httpx.Response(200, json={"pages": total, "pageSize": 5, "blocks": total})
        page = int(request.url.params.get("page", 0))
        appels.append((crawl_id, page))
        pages = par_index.get(crawl_id, [])
        corps = _jsonl(pages[page]) if page < len(pages) else ""
        return httpx.Response(200, text=corps)

    return httpx.MockTransport(handler), appels


# -- dialecte ----------------------------------------------------------------


def test_dialecte_demande_le_bon_champ():
    """Wayback veut `fl=original` en texte, Common Crawl `output=json` : leur
    inverser produit des `-` silencieux côté Common Crawl."""
    assert WAYBACK.params["fl"] == "original"
    assert WAYBACK.params["output"] == "text"
    assert COMMONCRAWL.params["output"] == "json"
    assert "fl" not in COMMONCRAWL.params
    # Les noms de filtre diffèrent aussi (statuscode vs status).
    assert "statuscode:200" in WAYBACK.params["filter"]
    assert "status:200" in COMMONCRAWL.params["filter"]


def test_parsing_json_par_lignes():
    lignes = _jsonl(["https://www.rts.ch/a.html", "https://www.rts.ch/b.html"])
    assert COMMONCRAWL.parse(lignes) == [
        "https://www.rts.ch/a.html",
        "https://www.rts.ch/b.html",
    ]


def test_ligne_json_illisible_ignoree_sans_perdre_la_page():
    corps = "{cassé\n" + json.dumps({"url": "https://www.rts.ch/ok.html"}) + "\n{aussi cassé"
    assert COMMONCRAWL.parse(corps) == ["https://www.rts.ch/ok.html"]


# -- collecte ----------------------------------------------------------------


def test_collecte_sur_plusieurs_index(tmp_path):
    transport, appels = _transport({
        "CC-MAIN-2026-25": [["https://www.rts.ch/info/suisse/a-1.html"]],
        "CC-MAIN-2026-21": [["https://www.rts.ch/info/culture/b-2.html"]],
    })
    store = Store(tmp_path / "data")
    client = commoncrawl.collect(
        store, pages_per_index=1, cache_dir=tmp_path / "cache", transport=transport
    )

    assert {u for u, _ in store.urls()} == {
        "https://www.rts.ch/info/suisse/a-1.html",
        "https://www.rts.ch/info/culture/b-2.html",
    }
    # Du plus récent au plus ancien.
    assert [crawl for crawl, _ in appels] == ["CC-MAIN-2026-25", "CC-MAIN-2026-21"]
    assert client.added == 2


def test_chaque_index_a_sa_propre_url_de_base(tmp_path):
    """Chaque crawl expose sa propre API CDX ; taper toujours la même
    rapporterait le même contenu en boucle."""
    tranches = commoncrawl.segments(
        cache_dir=tmp_path / "cache", transport=_transport({})[0]
    )
    assert [s.base_url for s in tranches] == [e["cdx-api"] for e in INDEXES]


def test_sous_domaines_hors_perimetre_ecartes_silencieusement(tmp_path):
    """Common Crawl ne permet pas de cibler www.rts.ch : pywb canonicalise en
    SURT, ce qui supprime le préfixe `www.`, si bien que `url=www.rts.ch`
    retombe sur `url=rts.ch`. La requête ramène donc tous les sous-domaines et
    c'est urlnorm qui tranche — d'où des pages entièrement écartées, ce qui
    n'est pas une anomalie."""
    transport, _ = _transport({
        "CC-MAIN-2026-25": [[
            "https://avecvous.rts.ch/contact",
            "https://boutique.rts.ch/",
            "https://www.rts.ch/info/suisse/garde-1.html",
        ]],
    }, indexes=INDEXES[:1])
    store = Store(tmp_path / "data")
    commoncrawl.collect(store, cache_dir=tmp_path / "cache", transport=transport)
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/info/suisse/garde-1.html"}


def test_budget_par_index_balaie_plusieurs_crawls(tmp_path):
    """Sans budget par tranche, le crawl le plus récent consommerait tout et
    les suivants ne seraient jamais entamés — or c'est en changeant de crawl
    qu'on trouve des URLs nouvelles, deux voisins se recouvrant beaucoup."""
    transport, appels = _transport({
        "CC-MAIN-2026-25": [[f"https://www.rts.ch/a-{p}.html"] for p in range(5)],
        "CC-MAIN-2026-21": [[f"https://www.rts.ch/b-{p}.html"] for p in range(5)],
    })
    commoncrawl.collect(
        Store(tmp_path / "data"), pages_per_index=1,
        cache_dir=tmp_path / "cache", transport=transport,
    )
    assert appels == [("CC-MAIN-2026-25", 0), ("CC-MAIN-2026-21", 0)]


def test_max_indexes_borne_le_nombre_de_crawls(tmp_path):
    tranches = commoncrawl.segments(
        cache_dir=tmp_path / "cache", transport=_transport({})[0], limit=1
    )
    assert [s.key for s in tranches] == [CLE_25]


# -- liste des index ---------------------------------------------------------


def test_liste_des_index_mise_en_cache(tmp_path):
    cache = tmp_path / "cache"
    appels = {"n": 0}

    def handler(request):
        appels["n"] += 1
        return httpx.Response(200, json=INDEXES)

    transport = httpx.MockTransport(handler)
    commoncrawl.indexes(cache, transport)
    commoncrawl.indexes(cache, transport)
    assert appels["n"] == 1  # la liste ne bouge qu'une fois par mois


def test_liste_corrompue_est_retelechargee(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "commoncrawl_indexes.json").write_bytes(b"{pas du JSON")

    transport, _ = _transport({})
    assert commoncrawl.indexes(cache, transport) == INDEXES


def test_liste_injoignable_ne_fait_pas_echouer(tmp_path):
    """Le serveur d'index Common Crawl est notoirement instable : son
    indisponibilité doit dégrader le run, pas le faire planter."""
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    store = Store(tmp_path / "data")
    client = commoncrawl.collect(store, cache_dir=tmp_path / "cache", transport=transport)
    assert client.added == 0


# -- robustesse --------------------------------------------------------------


def test_502_transitoire_est_retente(tmp_path):
    """Les 502/504 sont fréquents et transitoires sur index.commoncrawl.org
    (constaté en réel) ; les traiter comme définitifs viderait la source."""
    essais = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("collinfo.json"):
            return httpx.Response(200, json=INDEXES[:1])
        essais["n"] += 1
        if essais["n"] <= 2:
            return httpx.Response(502)
        if essais["n"] == 3:
            return httpx.Response(200, text=_jsonl(["https://www.rts.ch/ok-1.html"]))
        return httpx.Response(200, text="")

    store = Store(tmp_path / "data")
    commoncrawl.collect(
        store, cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler)
    )
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/ok-1.html"}


def test_total_de_pages_au_format_pywb(tmp_path):
    """`showNumPages` répond en JSON chez Common Crawl (`{"pages": N, ...}`),
    pas en entier nu comme chez Wayback (`"1511"`) : le mauvais format ferait
    échouer le total pour de bon, pas seulement se dégrader."""
    from rts_indexer.sources.cdx import CdxClient
    from rts_indexer.sources.cdx import COMMONCRAWL as dialecte
    from rts_indexer.sources.cdx import Segment as Seg

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("showNumPages") == "true":
            return httpx.Response(200, json={"pages": 3, "pageSize": 5, "blocks": 3})
        return httpx.Response(200, text="")

    client = CdxClient(
        "https://exemple.test/cdx", "curseur.json",
        dialect=dialecte, cache_dir=tmp_path, transport=httpx.MockTransport(handler),
    )
    with client.client() as http:
        assert client._total_pages(http, Seg("x", {})) == 3


def test_400_hors_limites_termine_la_tranche_sans_reprise(tmp_path):
    """Constaté en réel : Common Crawl répond 400 pour une page au-delà de la
    dernière (showNumPages annonçait `{"pages": 1}`), là où Wayback renvoie une
    page vide. C'est une fin de tranche, pas une panne : la retenter quatre
    fois ne fait que perdre du temps sur un serveur déjà fragile."""
    appels = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("collinfo.json"):
            return httpx.Response(200, json=INDEXES[:1])
        if request.url.params.get("showNumPages") == "true":
            # Indisponible pour ce test : on veut isoler le comportement du
            # 400 lui-même, pas celui (couvert ailleurs) du total de pages.
            return httpx.Response(503)
        appels["n"] += 1
        if int(request.url.params.get("page", 0)) == 0:
            return httpx.Response(200, text=_jsonl(["https://www.rts.ch/a-1.html"]))
        return httpx.Response(400)

    cache = tmp_path / "cache"
    store = Store(tmp_path / "data")
    commoncrawl.collect(store, cache_dir=cache, transport=httpx.MockTransport(handler))

    assert appels["n"] == 2  # page 0 puis page 1, sans reprise inutile du 400
    curseur = json.loads((cache / "commoncrawl_cursor.json").read_text())
    assert curseur[CLE_25] is None  # tranche close, pas en échec
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/a-1.html"}


def test_curseur_reprend_par_index(tmp_path):
    cache = tmp_path / "cache"
    par_index = {"CC-MAIN-2026-25": [["https://www.rts.ch/a-1.html"], ["https://www.rts.ch/b-2.html"]]}

    transport, _ = _transport(par_index, indexes=INDEXES[:1])
    commoncrawl.collect(
        Store(tmp_path / "d1"), pages_per_index=1, cache_dir=cache, transport=transport
    )
    curseur = json.loads((cache / "commoncrawl_cursor.json").read_text())
    assert curseur[CLE_25] == 1

    transport, appels = _transport(par_index, indexes=INDEXES[:1])
    store = Store(tmp_path / "d2")
    commoncrawl.collect(store, pages_per_index=1, cache_dir=cache, transport=transport)
    assert appels == [("CC-MAIN-2026-25", 1)]
    assert {u for u, _ in store.urls()} == {"https://www.rts.ch/b-2.html"}
