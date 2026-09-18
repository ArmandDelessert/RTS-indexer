"""Couche HTTP partagée : User-Agent, timeouts, retries, débit."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config, profiles

log = logging.getLogger(__name__)

def default_headers() -> dict[str, str]:
    """En-têtes communs à toutes les requêtes.

    Construits à l'appel et non à l'import : le User-Agent vient du profil, qui
    n'est posé qu'au démarrage du CLI. Un dictionnaire figé à l'import gèlerait
    le profil par défaut quel que soit celui demandé ensuite.

    rts.ch renvoie 403 aux User-Agent trop laconiques (constaté sur
    robots.txt) : on s'identifie explicitement tout en présentant les en-têtes
    d'un navigateur.
    """
    return {
        "User-Agent": profiles.active().user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CH,fr;q=0.9",
    }


class RateLimiter:
    """Intervalle minimal garanti entre deux départs de requête.

    Partagé par tous les workers d'un même parcours : c'est le débit global
    vers le site qui compte, pas celui de chaque worker pris isolément.
    """

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


def client(**kwargs) -> httpx.Client:
    kwargs.setdefault("headers", default_headers())
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(**kwargs)


def async_client(**kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("headers", default_headers())
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)


def get(
    http: httpx.Client,
    url: str,
    *,
    attempts: int = 3,
    delay: float | None = None,
) -> httpx.Response | None:
    """GET avec backoff exponentiel. ``None`` si l'URL reste inaccessible.

    ``delay`` à ``None`` reprend le débit poli du profil actif.
    """
    delay = delay if delay is not None else profiles.active().request_delay
    for attempt in range(1, attempts + 1):
        try:
            response = http.get(url)
        except httpx.HTTPError as exc:
            log.warning("%s: %s (tentative %d/%d)", url, exc, attempt, attempts)
        else:
            if response.status_code < 400:
                time.sleep(delay)
                return response
            if response.status_code < 500 and response.status_code != 429:
                log.warning("%s: HTTP %d", url, response.status_code)
                return None
            log.warning(
                "%s: HTTP %d (tentative %d/%d)", url, response.status_code, attempt, attempts
            )
        time.sleep(delay * 2**attempt)
    return None
