"""Mécanique commune aux archives interrogeables en CDX.

Wayback Machine et Common Crawl exposent des interfaces CDX proches mais pas
identiques : le second est bâti sur pywb, qui nomme ``url`` le champ que le
premier appelle ``original`` et dont la sortie texte renvoie des ``-`` si on
lui demande le mauvais nom. D'où les deux *dialectes* ci-dessous. Tout le
reste — pagination, curseur de reprise, backoff, filtrage — est commun.

Deux contraintes dictent la conception :

* **Les requêtes sont lentes** (une dizaine de secondes chacune) et les
  serveurs limitent agressivement le débit. D'où le backoff sur 429/503 et
  l'absence de concurrence : ces archives ne sont pas des sites à crawler
  vite, ce sont des bases à interroger poliment.
* **Un parcours complet dure des heures.** Le curseur est donc persisté après
  *chaque* page, pour qu'une interruption ne coûte qu'une page et non tout le
  travail accompli.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .. import config, fsutil, net, urlnorm

log = logging.getLogger(__name__)


@dataclass
class Segment:
    """Une tranche interrogeable indépendamment (le domaine, un index...)."""

    #: Identifiant stable, utilisé comme clé de curseur.
    key: str
    #: Paramètres propres à la tranche, fusionnés à la requête.
    params: dict
    #: URL de base propre à la tranche, quand elle diffère (chaque index
    #: Common Crawl a la sienne). ``None`` = celle du client.
    base_url: str | None = None


@dataclass(frozen=True)
class Dialect:
    """Ce qui distingue une implémentation CDX d'une autre."""

    #: Paramètres de sortie (nom du champ URL, format).
    params: dict
    #: Extrait les URLs brutes du corps d'une réponse.
    parse: "Callable[[str], list[str]]"


def _parse_text(body: str) -> list[str]:
    return [ligne.strip() for ligne in body.splitlines() if ligne.strip()]


def _parse_json_lines(body: str) -> list[str]:
    """Une ligne = un objet JSON (format pywb). Les lignes illisibles sont
    ignorées plutôt que de faire échouer toute la page."""
    urls = []
    for ligne in body.splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            enregistrement = json.loads(ligne)
        except json.JSONDecodeError:
            continue
        url = enregistrement.get("url")
        if url:
            urls.append(url)
    return urls


#: Wayback : CDX classique, champ ``original``, sortie texte brute.
WAYBACK = Dialect(
    params={"fl": "original", "output": "text", "filter": ["mimetype:text/html", "statuscode:200"]},
    parse=_parse_text,
)

#: Common Crawl : pywb, champ ``url``, sortie JSON par lignes. Demander
#: ``fl=original`` en texte y renvoie silencieusement des ``-``.
COMMONCRAWL = Dialect(
    params={"output": "json", "filter": ["mimetype:text/html", "status:200"]},
    parse=_parse_json_lines,
)


class CdxClient:
    """Parcours paginé d'une archive CDX, reprenable."""

    def __init__(
        self,
        base_url: str,
        cursor_file: str,
        *,
        dialect: Dialect = WAYBACK,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        page_size: int | None = None,
    ) -> None:
        self.base_url = base_url
        self.dialect = dialect
        self.cursor_path = (cache_dir or config.CACHE_DIR) / cursor_file
        self.transport = transport
        self.page_size = page_size
        #: clé de tranche -> prochaine page à demander (une tranche absente
        #: n'a pas encore été entamée ; valeur ``None`` = tranche terminée)
        self.cursor: dict[str, int | None] = {}
        self.pages_fetched = 0
        self.rows_seen = 0

    # -- curseur -------------------------------------------------------------

    def load_cursor(self) -> None:
        if not self.cursor_path.is_file():
            return
        try:
            self.cursor = json.loads(fsutil.read_text(self.cursor_path))
        except (PermissionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s illisible (%s), reprise depuis le début", self.cursor_path, exc)
            self.cursor = {}
            return
        finies = sum(1 for v in self.cursor.values() if v is None)
        log.info("curseur: %d tranches terminées, %d en cours", finies, len(self.cursor) - finies)

    def save_cursor(self) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_text(
            self.cursor_path, json.dumps(self.cursor, ensure_ascii=False, sort_keys=True)
        )

    def is_done(self, segment: Segment) -> bool:
        return self.cursor.get(segment.key, 0) is None

    # -- requêtes ------------------------------------------------------------

    def _params(self, segment: Segment, page: int) -> dict:
        params = {
            "url": "rts.ch",
            "matchType": "domain",
            "collapse": "urlkey",
            **self.dialect.params,
            "page": str(page),
            **segment.params,
        }
        if self.page_size:
            params["pageSize"] = str(self.page_size)
        return params

    def _fetch(self, http: httpx.Client, segment: Segment, page: int) -> tuple[str, str]:
        """Retourne ``(issue, corps)`` où *issue* vaut :

        * ``"ok"`` — page récupérée (le corps peut être vide) ;
        * ``"end"`` — la tranche est épuisée, définitivement ;
        * ``"fail"`` — inaccessible malgré les reprises.

        Les 429 et 5xx sont retentés avec un délai croissant : ces archives
        limitent le débit de façon soutenue, et abandonner à la première alerte
        ferait échouer la quasi-totalité des parcours longs. Les autres 4xx ne
        le sont pas — une requête invalide le restera. Common Crawl répond
        d'ailleurs 400 pour une page au-delà de la dernière, là où Wayback
        renvoie une page vide : c'est une fin de tranche, pas une panne.
        """
        url = segment.base_url or self.base_url
        for attempt in range(1, config.CDX_ATTEMPTS + 1):
            try:
                response = http.get(url, params=self._params(segment, page))
            except httpx.HTTPError as exc:
                log.warning("%s p%d: %s (essai %d)", segment.key, page, exc, attempt)
            else:
                code = response.status_code
                if code == 200:
                    return "ok", response.text
                if 400 <= code < 500 and code != 429:
                    log.info("%s p%d: HTTP %d, fin de tranche", segment.key, page, code)
                    return "end", ""
                log.warning("%s p%d: HTTP %d (essai %d)", segment.key, page, code, attempt)
            time.sleep(config.CDX_BACKOFF * 2**attempt)
        return "fail", ""

    def _total_pages(self, http: httpx.Client, segment: Segment) -> int | None:
        """Nombre total de pages, via ``showNumPages``, ou ``None`` si
        indisponible. Interrogé une fois par tranche puis mis en cache dans le
        curseur : c'est la seule façon fiable de savoir quand une tranche
        domaine-entier est réellement épuisée, une simple série de pages
        creuses ne le permettant pas (voir la note de module).

        Le format de réponse diffère selon le dialecte : Wayback renvoie
        l'entier nu (``"1511"``), pywb/Common Crawl un objet JSON
        (``{"pages": 1511, ...}``) même quand la sortie normale est en texte.
        """
        params = {k: v for k, v in self._params(segment, 0).items() if k != "page"}
        params["showNumPages"] = "true"
        url = segment.base_url or self.base_url
        try:
            response = http.get(url, params=params)
        except httpx.HTTPError as exc:
            log.warning("%s: showNumPages injoignable (%s)", segment.key, exc)
            return None
        if response.status_code != 200:
            return None
        text = response.text.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return int(json.loads(text)["pages"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def iter_segment(self, http: httpx.Client, segment: Segment, max_pages: int = 0):
        """Produit les URLs canoniques d'une tranche, page par page.

        Le curseur est sauvegardé après chaque page : une interruption ne coûte
        que la page en cours.

        La borne de fin repose sur le total de pages (``showNumPages``), pas
        sur une série de pages vides consécutives. Constaté en réel sur
        Wayback : les pages 27 à 29 étaient vides alors que la page 700 en
        contenait 1008 — un domaine paginé dans son intégralité est creux de
        façon très inégale, si bien qu'aucun nombre de pages vides
        consécutives n'indique fiablement la fin. Le total sert de filet de
        secours seulement s'il reste introuvable (API indisponible).
        """
        page = self.cursor.get(segment.key, 0)
        if page is None:
            return
        total_key = f"{segment.key}:total"
        total = self.cursor.get(total_key)
        if total is None:
            total = self._total_pages(http, segment)
            if total is not None:
                self.cursor[total_key] = total
                self.save_cursor()
                log.info("%s: %d pages au total", segment.key, total)

        pages_here = 0
        vides = 0

        while True:
            if max_pages and pages_here >= max_pages:
                return
            if total is not None and page >= total:
                self._close(segment)
                return

            issue, body = self._fetch(http, segment, page)
            if issue == "fail":
                # Échec durable : on laisse le curseur en place pour reprendre
                # ici même au prochain run, plutôt que de sauter la page.
                log.error("%s p%d: abandon, tranche laissée en cours", segment.key, page)
                return
            if issue == "end":
                self._close(segment)
                return

            lignes = self.dialect.parse(body)
            self.pages_fetched += 1
            pages_here += 1
            self.rows_seen += len(lignes)

            if lignes:
                vides = 0
                urls = urlnorm.normalize_many(lignes)
                log.info(
                    "%s p%d/%s: %d lignes, %d URLs retenues",
                    segment.key,
                    page,
                    total if total is not None else "?",
                    len(lignes),
                    len(urls),
                )
                yield from urls
            else:
                vides += 1
                log.info("%s p%d/%s: vide", segment.key, page, total if total is not None else "?")
                if total is None and vides >= config.CDX_EMPTY_TOLERANCE:
                    # Filet de secours seulement si le total est resté
                    # inconnu : mieux vaut s'arrêter tôt que boucler sans fin
                    # sur une tranche dont on ignore la taille.
                    self._close(segment)
                    return

            page += 1
            self.cursor[segment.key] = page
            self.save_cursor()
            time.sleep(config.CDX_DELAY)

    def _close(self, segment: Segment) -> None:
        """Marque la tranche comme épuisée, définitivement."""
        self.cursor[segment.key] = None
        self.save_cursor()
        log.info("%s: tranche terminée", segment.key)

    def client(self) -> httpx.Client:
        return net.client(transport=self.transport, timeout=config.CDX_TIMEOUT)
