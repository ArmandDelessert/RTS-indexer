"""Source « crawl » : parcours en largeur des pages de rubrique.

Économie du crawler — seules les **rubriques** (URLs terminées par ``/``) sont
téléchargées. Ce sont les pages de listing : elles portent les liens vers les
articles, et elles se comptent en milliers là où les articles se comptent en
centaines de milliers. Les articles rencontrés sont indexés sans être visités,
sauf ``--include-articles``.

Limite connue, et c'est structurel : ``robots.txt`` interdit ``/*/page/`` et
``/*?*page=``. La pagination des rubriques est donc hors d'atteinte, et le
crawl ne voit que la première page de chaque listing. L'archive profonde vient
de la source Wayback, pas d'ici.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx
from lxml import html as lxml_html

from .. import config, fsutil, net, robots, urlnorm
from ..store import Store

log = logging.getLogger(__name__)

CACHE_FILE = "crawl.json"

#: Filet de rattrapage : certains liens d'article ne vivent pas dans un `a/@href`
#: mais dans un bloc JSON-LD. Un balayage brut du document les récupère.
#: `&` est exclu : rts.ch n'en met jamais dans un chemin, alors que les liens
#: de partage social (``sharer.php?u=<url>&amp;title=...``) le suivent d'assez
#: près pour que sans cette exclusion tout le texte du bouton soit avalé.
_RAW_URL = re.compile(r"""https?://(?:www\.)?rts\.ch/[^"'\s<>\\)&]+""")


class RateLimiter:
    """Intervalle minimal garanti entre deux départs de requête."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._min_interval
        if delay:
            await asyncio.sleep(delay)


def _extract(url: str, document: str) -> list[str]:
    """Toutes les URLs canoniques dans le périmètre citées par une page."""
    candidates: list[str] = []
    try:
        tree = lxml_html.fromstring(document)
    except (ValueError, lxml_html.etree.ParserError):
        log.warning("HTML illisible: %s", url)
    else:
        candidates += tree.xpath("//a/@href")
        candidates += tree.xpath("//link[@rel='canonical']/@href")
    candidates += _RAW_URL.findall(document)
    return urlnorm.normalize_many(candidates, base=url)


class Crawler:
    def __init__(
        self,
        store: Store,
        *,
        max_pages: int,
        include_articles: bool = False,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rules: robots.RobotsRules | None = None,
    ) -> None:
        self.store = store
        self.max_pages = max_pages
        self.include_articles = include_articles
        self.cache_path = (cache_dir or config.CACHE_DIR) / CACHE_FILE
        self.cache: dict[str, dict] = {}
        self.transport = transport
        #: Règles imposées (tests) plutôt que téléchargées par hôte.
        self.forced_rules = rules
        self.rules: dict[str, robots.RobotsRules] = {}
        self.visited: set[str] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.fetched = 0
        self.from_cache = 0
        self.discovered = 0

    # -- cache ---------------------------------------------------------------

    def load_cache(self) -> None:
        """Charge le cache ETag. Un cache corrompu (écriture interrompue par un
        crash précédent) ne doit pas bloquer *tous* les runs suivants : on
        repart d'un cache vide plutôt que de faire planter l'outil pour de bon."""
        if not self.cache_path.is_file():
            return
        try:
            self.cache = json.loads(fsutil.read_text(self.cache_path))
        except (PermissionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s illisible (%s), cache repris à vide", self.cache_path, exc)
            return
        log.info("cache: %d pages connues", len(self.cache))

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_text(self.cache_path, json.dumps(self.cache, ensure_ascii=False, sort_keys=True))

    # -- politesse -----------------------------------------------------------

    def _rules_for(self, url: str) -> robots.RobotsRules:
        if self.forced_rules is not None:
            return self.forced_rules
        host = url.split("/", 3)[2]
        if host not in self.rules:
            self.rules[host] = robots.fetch(host)
        return self.rules[host]

    def _fetchable(self, url: str) -> bool:
        """Une URL est-elle téléchargeable ?

        Les articles ne le sont pas par défaut (économie), et ``robots.txt``
        tranche le reste. Une URL non téléchargeable reste **indexée** : robots
        régit l'accès, pas le droit de connaître l'existence d'une adresse.
        """
        if not url.endswith("/") and not self.include_articles:
            return False
        return self._rules_for(url).allowed_url(url)

    # -- parcours ------------------------------------------------------------

    def _record(self, urls: list[str]) -> None:
        for url in urls:
            self.discovered += self.store.add(url)
            if url not in self.visited and self._fetchable(url):
                self.visited.add(url)
                self.queue.put_nowait(url)

    async def _worker(self, http: httpx.AsyncClient, limiter: RateLimiter) -> None:
        while True:
            if 0 < self.max_pages <= self.fetched:
                return
            try:
                url = await asyncio.wait_for(self.queue.get(), config.CRAWL_IDLE_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                return
            # Réservée ici, avant tout `await` ultérieur : avec plusieurs
            # workers, incrémenter seulement après la requête HTTP (comme
            # avant) laissait le temps à tous les autres de repasser la
            # vérification ci-dessus avec une valeur encore périmée, d'où un
            # dépassement systématique du budget de MAX_CONCURRENCY - 1 pages
            # (queue.get() ne cède la main que si elle est vide ; tant qu'elle
            # est pleine, tout ce bloc s'exécute d'un bloc sans interruption).
            self.fetched += 1
            try:
                await self._visit(http, limiter, url)
            finally:
                self.queue.task_done()
            if config.CRAWL_CHECKPOINT_PAGES and self.fetched % config.CRAWL_CHECKPOINT_PAGES == 0:
                self._checkpoint()

    async def _visit(self, http: httpx.AsyncClient, limiter: RateLimiter, url: str) -> None:
        entry = self.cache.get(url, {})
        headers = {}
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]

        await limiter.wait()
        try:
            response = await http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("%s: %s", url, exc)
            return

        try:
            self._process(url, entry, response)
        except Exception:
            # Une page ne doit jamais faire échouer tout le crawl : ce run doit
            # pouvoir tourner sans surveillance pendant des heures. C'est
            # exactement la classe de bug qui a fait perdre un crawl entier
            # (350 pages, ~13'600 URLs) sur une seule page au slug démesuré.
            log.exception("erreur inattendue en traitant %s, page ignorée", url)

    def _checkpoint(self) -> None:
        """Écrit l'index et le cache accumulés à intervalles réguliers.

        Sans ça, un run de plusieurs heures interrompu par un incident qui ne
        se laisse pas rattraper en Python (coupure de courant, ``kill -9``,
        fermeture du terminal) perdrait tout le travail depuis le début : seule
        l'écriture finale existait jusqu'ici. Volontairement synchrone (bloque
        brièvement les autres workers) : plus simple qu'une écriture en
        arrière-plan, et son coût est proportionnel à la taille de l'index
        (quelques centaines de milliers d'URLs tiennent en un instant).
        """
        try:
            self.store.write()
            self.save_cache()
        except Exception:
            log.exception("échec du checkpoint à %d pages, le crawl continue", self.fetched)
        else:
            log.info("checkpoint: %d URLs écrites sur disque", self.store.stats()["urls"])

    def _process(self, url: str, entry: dict, response: httpx.Response) -> None:
        if response.status_code == 304:
            # Page inchangée : on rejoue les liens mémorisés, sans re-parser.
            self.from_cache += 1
            self._record(entry.get("links", []))
            return
        if response.status_code >= 400:
            log.warning("%s: HTTP %d", url, response.status_code)
            return

        links = _extract(url, response.text)
        self.cache[url] = {
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "links": links,
        }
        self._record(links)
        if self.fetched % 50 == 0:
            log.info(
                "%d pages visitées, %d en file, %d URLs découvertes",
                self.fetched,
                self.queue.qsize(),
                self.discovered,
            )

    async def run(self, seeds: list[str]) -> None:
        self.load_cache()
        # robots.fetch est synchrone : on le résout avant de lancer la boucle
        # d'événements plutôt que de la bloquer depuis un worker.
        if self.forced_rules is None:
            for host in config.HOSTS:
                self.rules.setdefault(host, robots.fetch(host))
        self._record(seeds)
        limiter = RateLimiter(config.CRAWL_MIN_INTERVAL)
        try:
            async with httpx.AsyncClient(
                headers=net.DEFAULT_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
                follow_redirects=True,
                transport=self.transport,
            ) as http:
                workers = [
                    asyncio.create_task(self._worker(http, limiter))
                    for _ in range(config.MAX_CONCURRENCY)
                ]
                try:
                    await asyncio.gather(*workers)
                finally:
                    for worker in workers:
                        worker.cancel()
        finally:
            # Le cache ETag doit survivre même à un imprévu : c'est lui qui
            # évite de re-télécharger tout ce qui a déjà été visité au
            # prochain run.
            self.save_cache()


def crawl(store: Store, seeds: list[str], **kwargs) -> Crawler:
    """Lance le crawl et retourne le crawler, pour ses compteurs."""
    crawler = Crawler(store, **kwargs)
    asyncio.run(crawler.run(seeds))
    return crawler
