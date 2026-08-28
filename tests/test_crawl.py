"""Tests du crawler, sur un faux site servi par un transport httpx simulé."""

import httpx
import pytest

from rts_indexer import config, robots
from rts_indexer.sources.crawl import Crawler, _extract, select_seeds
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


def _transport(log: list[str] | None = None, pages: dict[str, str] | None = None) -> httpx.MockTransport:
    pages = PAGES if pages is None else pages

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request.url.path)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body, headers={"ETag": f'"{request.url.path}"'})

    return httpx.MockTransport(handler)


def _site_avec_beaucoup_de_rubriques(n: int) -> dict[str, str]:
    """Un faux site à N rubriques, toutes liées depuis la racine : la file
    reste occupée bien au-delà d'un petit budget de pages, condition
    nécessaire pour exercer un dépassement par course entre workers."""
    pages = {"/": "".join(f'<a href="/rubrique-{i}/">r{i}</a>' for i in range(n))}
    pages.update({f"/rubrique-{i}/": "sans lien" for i in range(n)})
    return pages


def _site_en_chaine(n: int) -> dict[str, str]:
    """N rubriques découvertes progressivement (chacune ne révèle la
    suivante qu'une fois visitée), pour observer la croissance de l'index
    entre deux checkpoints plutôt qu'une découverte totale dès la racine."""
    pages = {"/": '<a href="/rubrique-0/">suite</a>'}
    for i in range(n):
        suite = f'<a href="/rubrique-{i + 1}/">suite</a>' if i + 1 < n else ""
        pages[f"/rubrique-{i}/"] = suite
    return pages


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


def test_extraction_ecarte_les_liens_de_partage_social():
    """Un bouton de partage (``sharer.php?u=<url>&amp;title=...``) ne doit pas
    faire capturer `&amp;title=...` à la suite de l'URL réelle."""
    html = (
        '<a href="https://www.facebook.com/sharer/sharer.php?u='
        'https://www.rts.ch/education/monde-et-societe/culture-et-sport/l-ecole/'
        '&amp;title=L%27%C3%A9cole">partager</a>'
    )
    urls = _extract("https://www.rts.ch/", html)
    assert urls == ["https://www.rts.ch/education/monde-et-societe/culture-et-sport/l-ecole/"]


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


def test_410_marque_mort_immediatement(tmp_path):
    """410 est une suppression explicite : verify() lui-même ne le retente
    jamais, pas de raison d'attendre le prochain verify pour poser le sigil
    alors que crawl vient de faire la requête."""
    import asyncio

    pages = dict(PAGES)
    pages["/"] += '<a href="/rubrique-410/">morte</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rubrique-410/":
            return httpx.Response(410)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body, headers={"ETag": f'"{request.url.path}"'})

    crawler = _crawler(tmp_path, transport=httpx.MockTransport(handler))
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    assert dict(crawler.store.urls())["https://www.rts.ch/rubrique-410/"] is True


def test_404_ne_marque_pas_mort_sans_second_avis(tmp_path):
    """Contrairement au 410 : un seul 404 pendant le crawl ne suffit pas.
    Sans second avis différé comme Verifier._check(), ce n'est pas une preuve
    suffisante — laissé au prochain verify."""
    import asyncio

    pages = dict(PAGES)
    pages["/"] += '<a href="/rubrique-404/">morte</a>'
    # /rubrique-404/ n'a pas de page définie -> le handler par défaut renvoie 404.

    crawler = _crawler(tmp_path, transport=_transport(pages=pages))
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    assert dict(crawler.store.urls())["https://www.rts.ch/rubrique-404/"] is False


def test_progression_avec_budget(tmp_path):
    import time

    crawler = _crawler(tmp_path, max_pages=100)
    crawler._debut = time.monotonic() - 100
    crawler.fetched = 25

    assert crawler._progression() == "25/100 (25%), 1 min 40 s écoulées, ~5 min 00 s restantes"


def test_progression_sans_budget_illimite(tmp_path):
    crawler = _crawler(tmp_path, max_pages=0)
    crawler.fetched = 7

    assert crawler._progression() == "7 pages visitées"


def test_une_url_trop_longue_ne_fait_pas_echouer_le_crawl(tmp_path):
    """Incident réel : une page Play au slug démesuré faisait planter
    `asyncio.gather` et perdre tout le crawl, faute d'être rattrapée jusqu'à
    `store.add()`. Elle doit maintenant être ignorée sans arrêter le parcours."""
    import asyncio

    pages = dict(PAGES)
    pages["/info/suisse/"] += (
        '<a href="/play/tv/19h30/video/' + ("mot-" * 70) + '/">longue</a>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body, headers={"ETag": f'"{request.url.path}"'})

    crawler = _crawler(tmp_path, transport=httpx.MockTransport(handler))
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    urls = {url for url, _ in crawler.store.urls()}
    assert "https://www.rts.ch/info/suisse/2026/article/premier-1.html" in urls
    assert not any("mot-mot-mot" in url for url in urls)


def test_robots_respecte(tmp_path):
    """`/*/page/` est interdit : la pagination n'est jamais téléchargée."""
    import asyncio

    visites: list[str] = []
    crawler = _crawler(tmp_path, log=visites)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))
    assert "/info/suisse/page/2" not in visites


def test_budget_de_pages_est_un_plafond_exact(tmp_path):
    """Incident réel : avec MAX_CONCURRENCY=4 workers et une file bien
    fournie, le budget était dépassé de façon quasi systématique de
    MAX_CONCURRENCY - 1 pages (constaté sur 4 runs réels : 20/100/50/2000
    demandées, 23/103/53/2003 visitées). La réservation du compteur avant
    tout `await` doit désormais en faire un plafond exact."""
    import asyncio

    site = _site_avec_beaucoup_de_rubriques(50)
    crawler = _crawler(tmp_path, max_pages=10, transport=_transport(pages=site))
    asyncio.run(crawler.run(["https://www.rts.ch/"]))
    assert crawler.fetched == 10


def test_budget_illimite_s_arrete_de_lui_meme(tmp_path):
    """`--max-pages 0` ne doit pas tourner indéfiniment : la file de rubriques
    est finie, donc le crawl s'arrête naturellement une fois tout visité."""
    import asyncio

    site = _site_avec_beaucoup_de_rubriques(12)
    crawler = _crawler(tmp_path, max_pages=0, transport=_transport(pages=site))
    asyncio.run(crawler.run(["https://www.rts.ch/"]))
    assert crawler.fetched == 13  # 12 rubriques + la racine


def test_page_qui_plante_n_interrompt_pas_le_crawl(tmp_path, monkeypatch):
    """Incident réel : une exception inattendue pendant le traitement d'une
    page (post-téléchargement) tuait tout `asyncio.gather`, perdant un crawl de
    350 pages. Une page en erreur doit désormais être ignorée, pas fatale."""
    import asyncio

    from rts_indexer.sources import crawl as crawl_module

    original = crawl_module._extract

    def extraction_qui_plante(url, document):
        if url.endswith("/info/culture/"):
            raise RuntimeError("boom")
        return original(url, document)

    monkeypatch.setattr(crawl_module, "_extract", extraction_qui_plante)

    crawler = _crawler(tmp_path)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    urls = {url for url, _ in crawler.store.urls()}
    # La page qui plante n'a pas empêché les autres d'être traitées.
    assert "https://www.rts.ch/info/suisse/2026/article/premier-1.html" in urls
    assert "https://www.rts.ch/info/culture/" in urls  # découverte, même si son contenu a planté
    assert "https://www.rts.ch/info/culture/2026/article/troisieme-4.html" not in urls


def test_cache_corrompu_repart_a_vide_sans_planter(tmp_path):
    """Un cache abîmé par une écriture interrompue ne doit pas bloquer *tous*
    les runs suivants : mieux vaut reperdre le bénéfice des ETags qu'un outil
    qui ne redémarre plus jamais."""
    import asyncio

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "crawl.json").write_bytes(b"{ceci n'est pas du JSON valide")

    crawler = _crawler(tmp_path, cache_dir=cache_dir)
    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    assert crawler.cache != {}  # repeuplé normalement malgré le cache corrompu
    urls = {url for url, _ in crawler.store.urls()}
    assert "https://www.rts.ch/info/suisse/2026/article/premier-1.html" in urls


def test_checkpoint_ecrit_periodiquement_sur_disque(tmp_path, monkeypatch):
    """Sans checkpoint, un incident non rattrapable en Python (coupure de
    courant, kill -9) au milieu d'un long crawl perdrait tout depuis le début.
    L'index doit donc apparaître sur disque avant la fin du run."""
    import asyncio

    monkeypatch.setattr(config, "CRAWL_CHECKPOINT_PAGES", 3)
    monkeypatch.setattr(config, "MAX_CONCURRENCY", 1)  # découverte strictement progressive
    site = _site_en_chaine(10)
    crawler = _crawler(tmp_path, max_pages=0, transport=_transport(pages=site))

    disque_pendant_le_run: list[int] = []
    original_write = crawler.store.write

    def write_espionne():
        stats = original_write()
        disque_pendant_le_run.append(stats["urls"])
        return stats

    monkeypatch.setattr(crawler.store, "write", write_espionne)

    asyncio.run(crawler.run(["https://www.rts.ch/"]))

    # Plusieurs checkpoints ont eu lieu avant la fin naturelle du run (aucune
    # écriture finale n'est déclenchée par run() lui-même, seul cmd_crawl le
    # fait) : le compteur observé grandit au fil des checkpoints.
    assert len(disque_pendant_le_run) >= 3
    assert disque_pendant_le_run == sorted(disque_pendant_le_run)
    assert disque_pendant_le_run[-1] > disque_pendant_le_run[0]

    # Et le cache ETag est lui aussi rafraîchi à chaque checkpoint.
    assert crawler.cache_path.is_file()


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


# -- rotation des graines ------------------------------------------------


def test_select_seeds_avance_le_curseur_d_un_run_a_l_autre(tmp_path):
    seeds = [f"https://www.rts.ch/r{i}/" for i in range(10)]
    premier = select_seeds(seeds, limit=4, cache_dir=tmp_path)
    second = select_seeds(seeds, limit=4, cache_dir=tmp_path)
    assert premier == seeds[0:4]
    assert second == seeds[4:8]
    # Les deux tranches ne se recouvrent pas : c'est le point de la rotation.
    assert not set(premier) & set(second)


def test_select_seeds_boucle_a_la_fin_de_la_liste(tmp_path):
    seeds = [f"https://www.rts.ch/r{i}/" for i in range(10)]
    select_seeds(seeds, limit=4, cache_dir=tmp_path)  # 0:4
    select_seeds(seeds, limit=4, cache_dir=tmp_path)  # 4:8
    troisieme = select_seeds(seeds, limit=4, cache_dir=tmp_path)  # 8:10 puis 0:2
    assert troisieme == [seeds[8], seeds[9], seeds[0], seeds[1]]


def test_select_seeds_illimite_renvoie_tout_sans_toucher_au_curseur(tmp_path):
    seeds = [f"https://www.rts.ch/r{i}/" for i in range(10)]
    assert select_seeds(seeds, limit=0, cache_dir=tmp_path) == seeds
    # Un run illimité ne doit pas décaler la reprise d'un run budgété suivant.
    assert select_seeds(seeds, limit=4, cache_dir=tmp_path) == seeds[0:4]


def test_select_seeds_reset_repart_du_debut(tmp_path):
    seeds = [f"https://www.rts.ch/r{i}/" for i in range(10)]
    select_seeds(seeds, limit=4, cache_dir=tmp_path)  # 0:4
    repris = select_seeds(seeds, limit=4, cache_dir=tmp_path, reset=True)
    assert repris == seeds[0:4]


def test_select_seeds_curseur_corrompu_repart_a_zero(tmp_path):
    (tmp_path / config.CRAWL_SEED_CURSOR_FILE).write_text("pas du json", encoding="utf-8")
    seeds = [f"https://www.rts.ch/r{i}/" for i in range(10)]
    assert select_seeds(seeds, limit=4, cache_dir=tmp_path) == seeds[0:4]


def test_select_seeds_absorbe_une_liste_qui_change_de_taille(tmp_path):
    """De nouvelles rubriques apparaissent d'un run à l'autre : le curseur ne
    doit pas planter, même s'il ne pointe plus exactement où avant."""
    petite = [f"https://www.rts.ch/r{i}/" for i in range(5)]
    select_seeds(petite, limit=4, cache_dir=tmp_path)  # offset -> 4
    grande = [f"https://www.rts.ch/r{i}/" for i in range(20)]
    suite = select_seeds(grande, limit=4, cache_dir=tmp_path)
    assert suite == grande[4:8]


def test_select_seeds_liste_vide(tmp_path):
    assert select_seeds([], limit=4, cache_dir=tmp_path) == []
