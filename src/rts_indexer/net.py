"""Couche HTTP partagée : User-Agent, timeouts, retries, débit."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)

#: rts.ch renvoie 403 aux User-Agent trop laconiques (constaté sur robots.txt).
#: On s'identifie explicitement tout en présentant les en-têtes d'un navigateur.
DEFAULT_HEADERS = {
    "User-Agent": config.USER_AGENT,
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
    kwargs.setdefault("headers", DEFAULT_HEADERS)
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(**kwargs)


def async_client(**kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("headers", DEFAULT_HEADERS)
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)


def get(
    http: httpx.Client,
    url: str,
    *,
    attempts: int = 3,
    delay: float = config.REQUEST_DELAY,
) -> httpx.Response | None:
    """GET avec backoff exponentiel. ``None`` si l'URL reste inaccessible."""
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
