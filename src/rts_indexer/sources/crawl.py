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


#: Réexporté pour ne pas casser les imports existants ; l'implémentation vit
#: dans net.py depuis que `verify` en a besoin elle aussi.
RateLimiter = net.RateLimiter


def _duree(secondes: float) -> str:
    """Durée lisible : ``2 h 05 min``, ``4 min 12 s``, ``8 s``.

    Doublon volontaire de l'équivalent dans verify.py/cli.py plutôt qu'un
    module partagé pour une fonction de cette taille (voir la justification
    dans verify.py).
    """
    if secondes < 60:
        return f"{secondes:.0f} s"
    minutes, sec = divmod(int(secondes), 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    heures, minutes = divmod(minutes, 60)
    return f"{heures} h {minutes:02d} min"


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
        self._debut = 0.0

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
            except TimeoutError:
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

    def _progression(self) -> str:
        """``150/500 (30%), 4 min 12 s écoulées, ~9 min 48 s restantes`` quand
        un budget de pages est fixé ; un simple compteur sinon (``--max-pages
        0`` = illimité, aucun total à afficher). Même logique que
        Verifier._progression() : estimation qui suppose un débit constant,
        approximative mais mieux qu'un silence total sur un run de plusieurs
        heures.
        """
        if self.max_pages <= 0:
            return f"{self.fetched} pages visitées"
        pct = 100 * self.fetched / self.max_pages
        ecoule = time.monotonic() - self._debut
        resultat = f"{self.fetched}/{self.max_pages} ({pct:.0f}%), {_duree(ecoule)} écoulées"
        if 0 < self.fetched < self.max_pages and ecoule > 0:
            taux = self.fetched / ecoule
            restant = (self.max_pages - self.fetched) / taux
            resultat += f", ~{_duree(restant)} restantes"
        return resultat

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
            log.info(
                "checkpoint (écrit sur disque) : %s, %d URLs dans l'index",
                self._progression(),
                self.store.stats()["urls"],
            )

    def _process(self, url: str, entry: dict, response: httpx.Response) -> None:
        if response.status_code == 304:
            # Page inchangée : on rejoue les liens mémorisés, sans re-parser.
            self.from_cache += 1
            self._record(entry.get("links", []))
            return
        if response.status_code >= 400:
            log.warning("%s: HTTP %d", url, response.status_code)
            # 410 est une suppression explicite : verify() lui-même ne le
            # retente jamais (cf. VERIFY_RETRY_CODES), pas de raison d'attendre
            # le prochain verify pour poser le sigil alors qu'on a la réponse
            # sous la main. 404 reste volontairement intact : sans second avis
            # différé comme verify(), un seul essai n'est pas une preuve
            # suffisante (incident passager possible) — cf. la même prudence
            # dans Verifier._check().
            if response.status_code in config.VERIFY_DEAD_CODES - config.VERIFY_RETRY_CODES:
                self.store.add(url, dead=True)
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
                "%s, %d en file, %d URLs découvertes",
                self._progression(),
                self.queue.qsize(),
                self.discovered,
            )

    async def run(self, seeds: list[str]) -> None:
        self._debut = time.monotonic()
        self.load_cache()
        # robots.fetch est synchrone : on le résout avant de lancer la boucle
        # d'événements plutôt que de la bloquer depuis un worker.
        if self.forced_rules is None:
            for host in config.HOSTS:
                self.rules.setdefault(host, robots.fetch(host))
        self._record(seeds)
        limiter = RateLimiter(config.CRAWL_MIN_INTERVAL)
        try:
            async with net.async_client(transport=self.transport) as http:
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


def _cursor_path(cache_dir: Path) -> Path:
    return cache_dir / config.CRAWL_SEED_CURSOR_FILE


def select_seeds(
    seeds: list[str],
    *,
    limit: int,
    cache_dir: Path | None = None,
    reset: bool = False,
) -> list[str]:
    """Choisit une tranche de graines à parcourir cette exécution, en rotation.

    L'index compte des dizaines de milliers de rubriques ; un run budgété
    (``limit``) qui repartirait toujours du début ne visiterait jamais que les
    mêmes premières, indéfiniment (l'ordre de :func:`Store.urls` est trié, donc
    stable d'un run à l'autre). Un curseur persisté fait avancer la tranche à
    chaque exécution, de sorte qu'un enchaînement de runs planifiés couvre à
    terme la totalité des rubriques plutôt que de piétiner sur les mêmes.

    ``limit`` à 0 (illimité) renvoie toutes les graines sans toucher au
    curseur : un run sans budget n'a pas besoin de rotation, et ne doit pas
    faire dériver la reprise d'un run budgété ultérieur.
    """
    if limit <= 0 or not seeds:
        return seeds

    path = _cursor_path(cache_dir or config.CACHE_DIR)
    offset = 0
    if not reset and path.is_file():
        try:
            offset = json.loads(fsutil.read_text(path)).get("offset", 0)
        except (PermissionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s illisible (%s), rotation reprise à 0", path, exc)

    # `seeds` change de taille d'un run à l'autre (nouvelles rubriques
    # découvertes) ; le modulo absorbe ça sans logique de resynchronisation
    # particulière, quitte à visiter une graine un peu plus tôt ou plus tard
    # que sa place idéale. Ce n'est pas un partitionnement exact, seulement une
    # garantie que la tête de liste ne monopolise pas tous les runs.
    offset %= len(seeds)
    selected = (seeds[offset:] + seeds[:offset])[:limit]

    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(path, json.dumps({"offset": (offset + limit) % len(seeds)}))
    return selected
