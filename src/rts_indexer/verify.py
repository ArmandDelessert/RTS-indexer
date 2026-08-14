"""Contrôle de vivacité : pose ou retire le sigil ``!`` sur les URLs indexées.

Une requête ``HEAD`` suffit à trancher dans la grande majorité des cas et
économise le corps de la réponse ; on retombe sur ``GET`` quand le serveur
refuse le HEAD (405/501), ce que font certains CDN.

Le verdict est volontairement conservateur : seuls 404 et 410 marquent une URL
comme morte. Un 403, un 429 ou un 5xx sont *non concluants* — on ne touche pas
au sigil et on ne met rien en cache, pour recontrôler au prochain run. Sans
cette prudence, une coupure réseau passagère ou une limitation de débit
enterrerait des milliers d'URLs parfaitement vivantes d'un seul coup.

L'état de vérification vit dans ``.cache/verify.json``, pas dans ``/data`` :
la date du dernier contrôle changerait à chaque run et polluerait le dépôt
Git de diffs sans intérêt, alors que seul le verdict (le sigil) mérite d'être
versionné.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import config, fsutil, net
from .store import Store

log = logging.getLogger(__name__)

CACHE_FILE = "verify.json"

#: Le serveur refuse la méthode HEAD : on retente en GET.
_HEAD_REFUSED = frozenset({403, 405, 501})


def _prefixe_canonique(prefix: str | None) -> str | None:
    """Accepte ``www.rts.ch/meteo/`` aussi bien que l'URL complète.

    Les URLs de l'index sont toutes en ``https://`` ; on complète donc un
    préfixe donné sans schéma plutôt que d'exiger la forme longue.
    """
    if not prefix:
        return None
    if not prefix.startswith(("http://", "https://")):
        prefix = "https://" + prefix.lstrip("/")
    return prefix


class Verifier:
    def __init__(
        self,
        store: Store,
        *,
        max_urls: int = 0,
        recheck_days: int | None = None,
        path_prefix: str | None = None,
        retry_delay: float | None = None,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.max_urls = max_urls
        self.recheck_days = (
            config.VERIFY_RECHECK_DAYS if recheck_days is None else recheck_days
        )
        #: Délai avant un second avis. Abaissé à zéro par les tests, qui
        #: doivent exercer la logique de reprogrammation sans attendre.
        self.retry_delay = (
            config.VERIFY_RETRY_DELAY if retry_delay is None else retry_delay
        )
        self.path_prefix = _prefixe_canonique(path_prefix)
        self.cache_path = (cache_dir or config.CACHE_DIR) / CACHE_FILE
        self.transport = transport
        #: url -> {"checked_at": iso8601, "status": int}
        self.cache: dict[str, dict] = {}
        #: File des URLs à réessayer plus tard, ordonnée par échéance
        #: (monotonic). Un 404 isolé ou une réponse non concluante y atterrit
        #: plutôt que d'être tranché sur-le-champ.
        self._differes: list[tuple[float, str]] = []
        #: url -> nombre de tentatives déjà faites.
        self._essais: dict[str, int] = {}
        #: Contrôles en cours, pour ne pas conclure le run alors qu'un worker
        #: s'apprête à reprogrammer une URL.
        self._en_vol = 0
        self.checked = 0
        self.morts = 0
        self.ressuscites = 0
        self.non_concluants = 0
        self.reessais = 0
        #: url demandée -> URL finale, quand le serveur redirige ailleurs.
        self.redirections: dict[str, str] = {}

    # -- cache ---------------------------------------------------------------

    def load_cache(self) -> None:
        if not self.cache_path.is_file():
            return
        try:
            self.cache = json.loads(fsutil.read_text(self.cache_path))
        except (PermissionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s illisible (%s), cache repris à vide", self.cache_path, exc)
            return
        log.info("cache: %d URLs déjà contrôlées", len(self.cache))

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_text(
            self.cache_path, json.dumps(self.cache, ensure_ascii=False, sort_keys=True)
        )

    # -- sélection -----------------------------------------------------------

    def _is_stale(self, url: str) -> bool:
        """Cette URL mérite-t-elle un (nouveau) contrôle ?"""
        entry = self.cache.get(url)
        if entry is None:
            return True
        if self.recheck_days <= 0:
            return True
        try:
            checked_at = datetime.fromisoformat(entry["checked_at"])
        except (KeyError, ValueError):
            return True
        return datetime.now(UTC) - checked_at > timedelta(days=self.recheck_days)

    def pending(self) -> list[str]:
        """URLs à contrôler, les jamais-vues d'abord.

        L'ordre compte : avec un ``--limit``, on veut avancer sur le front
        inconnu plutôt que de re-contrôler indéfiniment les mêmes URLs
        anciennes.
        """
        jamais_vues, a_rafraichir = [], []
        for url, _ in self.store.urls():
            if self.path_prefix and not url.startswith(self.path_prefix):
                continue
            if url not in self.cache:
                jamais_vues.append(url)
            elif self._is_stale(url):
                a_rafraichir.append(url)
        selection = jamais_vues + a_rafraichir
        return selection[: self.max_urls] if self.max_urls > 0 else selection

    # -- contrôle ------------------------------------------------------------

    async def _status(
        self, http: httpx.AsyncClient, url: str
    ) -> tuple[int | None, str | None]:
        """Code HTTP final et URL finale après redirections.

        L'URL finale ne coûte rien : le client suit déjà les redirections, on
        se contentait de jeter l'information. Elle révèle les doublons — une
        variante de slug qui redirige vers l'article canonique répond 200 et
        paraissait donc parfaitement saine.
        """
        try:
            response = await http.head(url)
            if response.status_code in _HEAD_REFUSED:
                # Certains CDN n'implémentent pas HEAD : un GET tranchera.
                response = await http.get(url)
            return response.status_code, str(response.url)
        except httpx.HTTPError as exc:
            log.warning("%s: %s", url, exc)
            return None, None

    def _reprogrammer(self, url: str, motif: str) -> bool:
        """Remet ``url`` en file pour un second avis. ``False`` si les essais
        sont épuisés — l'appelant doit alors trancher."""
        essais = self._essais.get(url, 1)
        if essais >= config.VERIFY_ATTEMPTS:
            return False
        self._essais[url] = essais + 1
        heapq.heappush(self._differes, (time.monotonic() + self.retry_delay, url))
        self.reessais += 1
        log.debug("%s (%s) : second avis dans %.0fs", url, motif, self.retry_delay)
        return True

    async def _check(self, http: httpx.AsyncClient, limiter: net.RateLimiter, url: str) -> None:
        await limiter.wait()
        status, url_finale = await self._status(http, url)

        if status is None or (status not in config.VERIFY_DEAD_CODES and status >= 400):
            # Non concluant (403, 429, 5xx, réseau). Ces codes sont transitoires
            # par nature : un second essai quelques instants plus tard vaut
            # mieux que d'attendre le prochain run, des semaines plus tard.
            if self._reprogrammer(url, f"HTTP {status}"):
                return
            self.checked += 1
            self.non_concluants += 1
            return

        # Un 404 peut être un incident passager ; un 410 est une suppression
        # explicite, que rien ne sert de réinterroger (cf. VERIFY_RETRY_CODES).
        if status in config.VERIFY_RETRY_CODES and self._reprogrammer(url, f"HTTP {status}"):
            return

        self.checked += 1
        dead = status in config.VERIFY_DEAD_CODES
        etait_morte = self.store.status(url) is True
        self.store.add(url, dead=dead)
        self.cache[url] = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": status,
        }

        # Doublon : l'URL répond, mais le serveur renvoie ailleurs. Journalisé
        # seulement — rien n'est supprimé à ce stade, le but est d'abord de
        # mesurer l'ampleur réelle sur une confirmation réseau plutôt que sur
        # une heuristique de slug.
        if not dead and url_finale and url_finale.rstrip("/") != url.rstrip("/"):
            self.redirections[url] = url_finale
            self.store.anomalies.add(("doublon", url, url_finale))

        if dead and not etait_morte:
            self.morts += 1
            log.info("morte (HTTP %d): %s", status, url)
        elif not dead and etait_morte:
            self.ressuscites += 1
            log.info("de nouveau vivante (HTTP %d): %s", status, url)

    def _checkpoint(self) -> None:
        try:
            self.store.write()
            self.save_cache()
        except Exception:
            log.exception("échec du checkpoint à %d URLs, le contrôle continue", self.checked)
        else:
            log.info("checkpoint: %d URLs contrôlées", self.checked)

    def _differe_du(self) -> str | None:
        """Retire et retourne une URL différée dont l'échéance est atteinte.

        Sûr sans verrou : aucun ``await`` entre l'examen et le retrait, et
        asyncio n'interrompt une coroutine qu'à un point d'attente.
        """
        if self._differes and self._differes[0][0] <= time.monotonic():
            return heapq.heappop(self._differes)[1]
        return None

    async def _worker(
        self, http: httpx.AsyncClient, limiter: net.RateLimiter, queue: asyncio.Queue
    ) -> None:
        while True:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                url = self._differe_du()

            if url is None:
                # Rien d'immédiat. Le run n'est terminé que si plus rien
                # n'attend son échéance *et* qu'aucun contrôle en cours ne peut
                # encore reprogrammer une URL.
                if not self._differes and self._en_vol == 0:
                    return
                await asyncio.sleep(0.2)
                continue

            self._en_vol += 1
            try:
                await self._check(http, limiter, url)
            except Exception:
                # Comme pour le crawl : une URL ne doit jamais interrompre un
                # run qui peut durer des heures.
                log.exception("erreur inattendue en contrôlant %s, URL ignorée", url)
            finally:
                self._en_vol -= 1
            if config.VERIFY_CHECKPOINT_URLS and self.checked % config.VERIFY_CHECKPOINT_URLS == 0:
                self._checkpoint()

    async def run(self) -> None:
        self.load_cache()
        urls = self.pending()
        if not urls:
            log.info("rien à contrôler")
            return
        log.info("%d URLs à contrôler", len(urls))

        queue: asyncio.Queue[str] = asyncio.Queue()
        for url in urls:
            queue.put_nowait(url)

        limiter = net.RateLimiter(config.VERIFY_MIN_INTERVAL)
        try:
            async with net.async_client(transport=self.transport) as http:
                workers = [
                    asyncio.create_task(self._worker(http, limiter, queue))
                    for _ in range(config.MAX_CONCURRENCY)
                ]
                try:
                    await asyncio.gather(*workers)
                finally:
                    for worker in workers:
                        worker.cancel()
        finally:
            self.save_cache()


def verify(store: Store, **kwargs) -> Verifier:
    """Lance le contrôle et retourne le vérificateur, pour ses compteurs."""
    verifier = Verifier(store, **kwargs)
    asyncio.run(verifier.run())
    return verifier
