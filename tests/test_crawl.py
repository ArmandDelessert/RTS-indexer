"""Tests du crawler, sur un faux site servi par un transport httpx simulé."""

import httpx
import pytest

from rts_indexer import config, robots
from rts_indexer.sources.crawl import Crawler, _extract
from rts_indexer.store import Store

# Faux rts.ch : une racine, deux rubriques, des articles, et du bruit hors
# périmètre à écarter.
PAGES = {
    "/": """
        <a href="/info/suisse/">Suisse</a>
        <a href="/info/culture/">Culture</a>
        <a href="https://img.rts.ch/x.image">image</a>
        <a href="https://www.srf.ch/news/">SRF</a>
    """,
    "/info/suisse/": """
        <a href="2026/article/premier-1.html">un</a>
        <a href="/info/suisse/2026/article/second-2.html">deux</a>
        <a href="/info/suisse/page/2">page 2</a>
        <script type="application/ld+json">
          {"url": "https://www.rts.ch/info/suisse/2026/article/via-jsonld-3.html"}
        </script>
    """,
    "/info/culture/": '<a href="/info/culture/2026/article/troisieme-4.html">trois</a>',
}


def _transport(log: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request.url.path)
        body = PAGES.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body, headers={"ETag": f'"{request.url.path}"'})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    monkeypatch.setattr(config, "CRAWL_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(config, "CRAWL_IDLE_TIMEOUT", 0.1)


def _crawler(tmp_path, store=None, **kwargs):
    kwargs.setdefault("max_pages", 50)
    kwargs.setdefault("transport", _transport(kwargs.pop("log", None)))
    kwargs.setdefault("rules", robots.parse("User-agent: *\nDisallow: /*/page/\n"))
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return Crawler(store or Store(tmp_path / "data"), **kwargs)


# -- extraction --------------------------------------------------------------


def test_extraction_des_liens():
    urls = _extract("https://www.rts.ch/info/suisse/", PAGES["/info/suisse/"])
    assert "https://www.rts.ch/info/suisse/2026/article/premier-1.html" in urls
    assert "https://www.rts.ch/info/suisse/2026/article/second-2.html" in urls
    # Récupéré par le balayage brut, pas par a/@href.
    assert "https://www.rts.ch/info/suisse/2026/article/via-jsonld-3.html" in urls


def test_extraction_ecarte_le_hors_perimetre():
    urls = _extract("https://www.rts.ch/", PAGES["/"])
    assert urls == ["https://www.rts.ch/info/suisse/", "https://www.rts.ch/info/culture/"]


def test_html_illisible_ne_fait_pas_echouer():
    assert _extract("https://www.rts.ch/", "") == []


# -- parcours ----------------------------------------------------------------


def test_crawl_complet(tmp_path):
    import asyncio

    crawler = _crawler(tmp_path)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    indexees = {url for url, _ in crawler.store.urls()}
    assert "https://www.rts.ch/info/suisse/2026/article/premier-1.html" in indexees
    assert "https://www.rts.ch/info/culture/2026/article/troisieme-4.html" in indexees
    assert "https://www.rts.ch/info/suisse/2026/article/via-jsonld-3.html" in indexees


def test_seules_les_rubriques_sont_telechargees(tmp_path):
    """Les articles sont indexés sans être visités : c'est ce qui rend le crawl
    réalisable, les rubriques se comptant en milliers et les articles en
    centaines de milliers."""
    import asyncio

    visites: list[str] = []
    crawler = _crawler(tmp_path, log=visites)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    assert sorted(visites) == ["/", "/info/culture/", "/info/suisse/"]
    assert not any(path.endswith(".html") for path in visites)


def test_robots_respecte(tmp_path):
    """`/*/page/` est interdit : la pagination n'est jamais téléchargée."""
    import asyncio

    visites: list[str] = []
    crawler = _crawler(tmp_path, log=visites)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))
    assert "/info/suisse/page/2" not in visites


def test_budget_de_pages_respecte(tmp_path):
    import asyncio

    crawler = _crawler(tmp_path, max_pages=1)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))
    assert crawler.fetched <= config.MAX_CONCURRENCY  # une passe par worker au plus


def test_second_run_utilise_le_cache(tmp_path):
    """Un 304 rejoue les liens mémorisés : le second run ne re-parse rien."""
    import asyncio

    cache_dir = tmp_path / "cache"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match"):
            return httpx.Response(304)
        body = PAGES.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body, headers={"ETag": f'"{request.url.path}"'})

    premier = _crawler(tmp_path, cache_dir=cache_dir)
    asyncio.run(premier.run(["https://www.rts.ch/"]))
    attendu = {url for url, _ in premier.store.urls()}
    assert premier.from_cache == 0
    premier.store.write()

    # Le second run repart de l'index déjà écrit : c'est le scénario incrémental
    # réel, celui du workflow hebdomadaire.
    second = _crawler(
        tmp_path,
        store=Store(tmp_path / "data").load(),
        cache_dir=cache_dir,
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(second.run(["https://www.rts.ch/"]))

    # Toutes les pages ont répondu 304 : aucun HTML n'a été ré-analysé…
    assert second.from_cache == second.fetched > 0
    # …et rien de nouveau n'a été trouvé, donc le dépôt ne bougera pas.
    assert second.discovered == 0
    assert {url for url, _ in second.store.urls()} == attendu
