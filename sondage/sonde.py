#!/usr/bin/env python3
"""Sonde de faisabilité : un site est-il indexable par RTS-indexer ?

Répond, mesures à l'appui, aux questions qu'il faut trancher *avant* d'écrire
le profil d'un nouveau site — celles qui ont été tranchées à la main pour
rts.ch et qu'on ne veut pas refaire à l'aveugle :

* ``robots.txt`` nous laisse-t-il passer, et que déclare-t-il ?
* quelle est la forme des URLs (profondeur, longueur, extension terminale) ?
* la query string est-elle *porteuse* — c'est-à-dire la retirer change-t-il la
  page ? C'est LE point qui décide si ``urlnorm`` peut la jeter comme sur RTS ;
* la casse est-elle signifiante dans les chemins ?
* le site est-il derrière un paywall, et si oui répond-il quand même 200 ?
* y a-t-il un anti-bot qui rendra le crawl impraticable ?

Usage :

    python sonde.py www.lemonde.fr www.letemps.ch www.heidi.news
    python sonde.py www.lemonde.fr --sample 60 --json rapport.json

Dépendances : httpx, lxml (déjà dans requirements.txt du dépôt).
Débit : une requête par seconde et par hôte, ``robots.txt`` respecté.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit, parse_qsl, urlunsplit

import httpx
from lxml import etree
from lxml import html as lxml_html

UA = "url-indexer-probe/0.1 (+https://github.com/ArmandDelessert/RTS-indexer)"
DELAY = 1.0
TIMEOUT = 30.0

#: Signal normalisé schema.org du contenu payant. C'est le marqueur fiable —
#: les mots-clés ci-dessous ne sont qu'un filet de rattrapage.
_PAYWALL_JSONLD = re.compile(rb'"isAccessibleForFree"\s*:\s*"?(?:false|False)"?')
_PAYWALL_WORDS = (
    "réservé aux abonnés",
    "reserve aux abonnes",
    "abonnez-vous",
    "article payant",
    "pour lire la suite",
    "subscribers only",
)

_LOC = "//*[local-name()='loc']/text()"
_SITEMAPINDEX = "/*[local-name()='sitemapindex']"


# --------------------------------------------------------------------------
# Réseau
# --------------------------------------------------------------------------


class Client:
    """Client poli : un départ de requête par seconde, au plus."""

    def __init__(self) -> None:
        self.http = httpx.Client(
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._last = 0.0

    def get(self, url: str) -> httpx.Response | None:
        wait = DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            return self.http.get(url)
        except httpx.HTTPError as exc:
            print(f"    ! {type(exc).__name__} sur {url}", file=sys.stderr)
            return None

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@dataclass
class Robots:
    status: int = 0
    sitemaps: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    blocks_us: bool = False

    @classmethod
    def fetch(cls, client: Client, host: str) -> "Robots":
        out = cls()
        response = client.get(f"https://{host}/robots.txt")
        if response is None:
            return out
        out.status = response.status_code
        if response.status_code != 200:
            return out

        applies = False  # dans un groupe User-agent qui nous concerne
        for raw in response.text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            field_name = field_name.strip().lower()
            value = value.strip()
            if field_name == "sitemap":
                out.sitemaps.append(value)
            elif field_name == "user-agent":
                applies = value == "*"
            elif applies and field_name == "disallow" and value:
                out.disallow.append(value)
                if value == "/":
                    out.blocks_us = True
            elif applies and field_name == "crawl-delay":
                try:
                    out.crawl_delay = float(value)
                except ValueError:
                    pass
        return out


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------


def _parse_xml(content: bytes) -> etree._Element | None:
    if content[:2] == b"\x1f\x8b":
        import gzip

        content = gzip.decompress(content)
    try:
        return etree.fromstring(
            content, parser=etree.XMLParser(recover=True, resolve_entities=False)
        )
    except etree.XMLSyntaxError:
        return None


def collect_sitemap(client: Client, roots: list[str], budget: int) -> list[str]:
    """URLs des sitemaps, en suivant un niveau d'index. ``budget`` borne le
    nombre de documents téléchargés — un index de presse peut en compter des
    centaines."""
    urls: list[str] = []
    queue = list(roots)
    fetched = 0
    while queue and fetched < budget:
        tree = None
        response = client.get(queue.pop(0))
        fetched += 1
        if response is not None and response.status_code == 200:
            tree = _parse_xml(response.content)
        if tree is None:
            continue
        locs = [str(x).strip() for x in tree.xpath(_LOC)]
        if tree.xpath(_SITEMAPINDEX):
            queue.extend(locs)
        else:
            urls.extend(locs)
    return urls


def discover_urls(client: Client, host: str, robots: Robots, budget: int) -> tuple[list[str], str]:
    """Un échantillon d'URLs du site, et d'où il vient."""
    roots = robots.sitemaps or [f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"]
    urls = collect_sitemap(client, roots, budget)
    if urls:
        return urls, f"sitemap ({len(roots)} racine(s))"

    # Repli : les liens de la page d'accueil.
    response = client.get(f"https://{host}/")
    if response is None or response.status_code != 200:
        return [], "aucune (sitemap et accueil inaccessibles)"
    try:
        tree = lxml_html.fromstring(response.content)
    except (ValueError, lxml_html.etree.ParserError):
        return [], "accueil illisible"
    tree.make_links_absolute(str(response.url), resolve_base_href=True)
    same_host = [h for h in tree.xpath("//a/@href") if urlsplit(h).netloc == host]
    return same_host, "liens de la page d'accueil (pas de sitemap exploitable)"


# --------------------------------------------------------------------------
# Forme des URLs — analyse hors ligne
# --------------------------------------------------------------------------


def analyse_shape(urls: list[str], host: str) -> dict:
    """Profondeur, longueur des segments, extensions, query, casse."""
    depths: list[int] = []
    seg_lengths: list[int] = []
    extensions: Counter[str] = Counter()
    params: Counter[str] = Counter()
    with_query = 0
    uppercase: list[str] = []
    projected_too_long = 0

    for url in urls:
        parts = urlsplit(url)
        if parts.netloc != host:
            continue
        segments = [s for s in parts.path.split("/") if s]
        depths.append(len(segments))
        seg_lengths.extend(len(s) for s in segments)

        leaf = segments[-1] if segments else ""
        extensions[leaf[leaf.rindex(".") :].lower() if "." in leaf else "(aucune)"] += 1

        if parts.query:
            with_query += 1
            params.update(k for k, _ in parse_qsl(parts.query))

        if any(c.isupper() for c in parts.path):
            uppercase.append(url)

        # Longueur projetée sur disque : chaque majuscule devient %XX (3 car.).
        relpath = "/".join(segments[:-1]) if "." in leaf else "/".join(segments)
        inflated = len(relpath) + 2 * sum(1 for c in relpath if c.isupper())
        if len(f"data/{host}/{inflated * 'x'}/_index.txt") > 240:
            projected_too_long += 1

    total = len(depths) or 1
    return {
        "urls_analysees": len(depths),
        "profondeur": {
            "p50": statistics.median(depths) if depths else 0,
            "p90": sorted(depths)[int(len(depths) * 0.9)] if depths else 0,
            "max": max(depths) if depths else 0,
        },
        "segment_max": max(seg_lengths) if seg_lengths else 0,
        "chemins_trop_longs": projected_too_long,
        "extensions": extensions.most_common(6),
        "query_pct": round(100 * with_query / total, 1),
        "params": params.most_common(8),
        "majuscules": uppercase[:5],
        "majuscules_n": len(uppercase),
    }


# --------------------------------------------------------------------------
# Sondages en ligne
# --------------------------------------------------------------------------


def _is_paywalled(response: httpx.Response) -> bool:
    if _PAYWALL_JSONLD.search(response.content):
        return True
    text = response.text[:200_000].lower()
    return any(word in text for word in _PAYWALL_WORDS)


def sample_live(client: Client, urls: list[str], n: int) -> dict:
    """Échantillonne des URLs réelles : statut, redirection, paywall."""
    sample = random.sample(urls, min(n, len(urls)))
    codes: Counter[int] = Counter()
    redirects: list[tuple[str, str]] = []
    paywalled = 0
    offsite_redirects = 0
    failures = 0

    for url in sample:
        response = client.get(url)
        if response is None:
            failures += 1
            continue
        codes[response.status_code] += 1
        final = str(response.url)
        if final.rstrip("/") != url.rstrip("/"):
            redirects.append((url, final))
            if urlsplit(final).netloc != urlsplit(url).netloc:
                offsite_redirects += 1
        if response.status_code == 200 and _is_paywalled(response):
            paywalled += 1

    ok = codes.get(200, 0)
    blocked = sum(codes[c] for c in (401, 403, 429) if c in codes)
    return {
        "echantillon": len(sample),
        "codes": dict(codes.most_common()),
        "erreurs_reseau": failures,
        "taux_blocage_pct": round(100 * blocked / (len(sample) or 1), 1),
        "redirections": len(redirects),
        "redirections_hors_hote": offsite_redirects,
        "exemples_redirection": redirects[:3],
        "paywall_sur_200": f"{paywalled}/{ok}" if ok else "n/a",
    }


def test_query_bearing(client: Client, urls: list[str], n: int = 4) -> list[dict]:
    """La query string est-elle porteuse ? On compare la page avec et sans.

    C'est la mesure qui décide si ``urlnorm`` peut jeter la query comme il le
    fait pour rts.ch, ou si certains paramètres font partie de l'identité de
    la page et doivent survivre à la normalisation.
    """
    candidates = [u for u in urls if urlsplit(u).query][:n]
    results: list[dict] = []
    for url in candidates:
        parts = urlsplit(url)
        naked = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        with_q = client.get(url)
        without_q = client.get(naked)
        if with_q is None or without_q is None:
            continue
        same_status = with_q.status_code == without_q.status_code
        # 5 % de tolérance : pub, horodatage, jetons CSRF varient d'un appel
        # à l'autre sur une même page.
        biggest = max(len(with_q.content), len(without_q.content), 1)
        same_size = abs(len(with_q.content) - len(without_q.content)) / biggest < 0.05
        results.append(
            {
                "url": url,
                "params": [k for k, _ in parse_qsl(parts.query)],
                "avec": with_q.status_code,
                "sans": without_q.status_code,
                "porteur": not (same_status and same_size),
            }
        )
    return results


def test_case_sensitivity(client: Client, uppercase_urls: list[str]) -> str:
    """Une variante tout-minuscule d'un chemin à majuscules répond-elle ?

    Sur rts.ch elle répond 404 (``/JO_2012/`` vit, ``/jo_2012/`` non) : la casse
    est signifiante et doit être préservée dans le mapping disque.
    """
    for url in uppercase_urls[:3]:
        parts = urlsplit(url)
        lowered = urlunsplit((parts.scheme, parts.netloc, parts.path.lower(), "", ""))
        original = client.get(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
        variant = client.get(lowered)
        if original is None or variant is None or original.status_code != 200:
            continue
        if variant.status_code != 200:
            return f"SIGNIFIANTE (minuscule -> {variant.status_code} sur {parts.path})"
        if str(variant.url).rstrip("/") == str(original.url).rstrip("/"):
            return "insensible (la minuscule redirige vers la même page)"
        return "insensible (les deux formes répondent 200)"
    return "indéterminée (aucune URL à majuscules exploitable)"


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


def probe(host: str, sample: int, sitemap_budget: int) -> dict:
    client = Client()
    print(f"\n=== {host} " + "=" * (58 - len(host)))
    try:
        robots = Robots.fetch(client, host)
        print(f"robots.txt      : HTTP {robots.status}, {len(robots.disallow)} Disallow, "
              f"{len(robots.sitemaps)} Sitemap déclaré(s), "
              f"Crawl-delay={robots.crawl_delay or 'absent'}")
        if robots.blocks_us:
            print("                  ATTENTION : Disallow / pour User-agent * "
                  "— le crawl est interdit, seules les archives restent.")
        for url in robots.sitemaps[:4]:
            print(f"                  {url}")

        urls, origin = discover_urls(client, host, robots, sitemap_budget)
        print(f"découverte      : {len(urls)} URLs via {origin}")
        if not urls:
            print("VERDICT         : NON SONDABLE — ni sitemap ni accueil exploitable.")
            return {"host": host, "robots": robots.__dict__, "verdict": "non sondable"}

        shape = analyse_shape(urls, host)
        print(f"profondeur      : p50={shape['profondeur']['p50']} "
              f"p90={shape['profondeur']['p90']} max={shape['profondeur']['max']} segments")
        print(f"segment max     : {shape['segment_max']} caractères "
              f"({shape['chemins_trop_longs']} chemins dépasseraient MAX_REL_PATH_LEN)")
        print(f"extension feuille: {shape['extensions']}")
        print(f"query string    : {shape['query_pct']} % des URLs — params : "
              f"{[p for p, _ in shape['params']]}")
        print(f"majuscules      : {shape['majuscules_n']} URLs en contiennent")

        bearing = test_query_bearing(client, urls)
        for item in bearing:
            verdict = "PORTEUR (à conserver)" if item["porteur"] else "cosmétique (jetable)"
            print(f"  param {item['params']}: {verdict} "
                  f"[avec={item['avec']} sans={item['sans']}]")
        if not bearing:
            print("  (aucune URL à query dans le sitemap — query jetable par défaut)")

        casse = test_case_sensitivity(client, shape["majuscules"])
        print(f"casse des chemins: {casse}")

        live = sample_live(client, urls, sample)
        print(f"échantillon vif : {live['echantillon']} URLs -> {live['codes']}")
        print(f"  blocage       : {live['taux_blocage_pct']} % (401/403/429 = anti-bot)")
        print(f"  redirections  : {live['redirections']} "
              f"(dont {live['redirections_hors_hote']} hors hôte)")
        print(f"  paywall/200   : {live['paywall_sur_200']}")

        verdict = _verdict(robots, live)
        print(f"VERDICT         : {verdict}")
        return {
            "host": host,
            "robots": {
                "status": robots.status,
                "sitemaps": robots.sitemaps,
                "disallow": robots.disallow,
                "crawl_delay": robots.crawl_delay,
            },
            "origine_urls": origin,
            "forme": shape,
            "query_porteuse": bearing,
            "casse": casse,
            "vif": live,
            "verdict": verdict,
        }
    finally:
        client.close()


def _verdict(robots: Robots, live: dict) -> str:
    if robots.blocks_us:
        return "ARCHIVES SEULEMENT — robots.txt interdit tout le site"
    if live["taux_blocage_pct"] > 30:
        return (f"DIFFICILE — {live['taux_blocage_pct']} % de blocage, "
                "anti-bot actif : crawl peu fiable, privilégier sitemap + archives")
    if live["taux_blocage_pct"] > 5:
        return (f"INDEXABLE avec réserve — {live['taux_blocage_pct']} % de blocage, "
                "ralentir le crawl")
    return "INDEXABLE — pas d'obstacle structurel"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="+", help="hôtes à sonder (ex. www.lemonde.fr)")
    parser.add_argument("--sample", type=int, default=30,
                        help="URLs tirées au sort pour le sondage en ligne (défaut 30)")
    parser.add_argument("--sitemap-budget", type=int, default=12,
                        help="documents sitemap téléchargés au plus (défaut 12)")
    parser.add_argument("--json", help="écrit le rapport complet dans ce fichier")
    parser.add_argument("--seed", type=int, default=1, help="graine du tirage (reproductibilité)")
    args = parser.parse_args()

    random.seed(args.seed)
    reports = [probe(host, args.sample, args.sitemap_budget) for host in args.hosts]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(reports, handle, ensure_ascii=False, indent=2)
        print(f"\nRapport complet : {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
