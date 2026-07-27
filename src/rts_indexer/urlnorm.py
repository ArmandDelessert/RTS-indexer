"""Normalisation et filtrage des URLs.

C'est le point d'entrée unique de toutes les sources : sitemap, crawl et Wayback
passent leurs URLs brutes par :func:`normalize`. Une URL qui en ressort est
canonique, dans le périmètre, et prête à être écrite dans ``/data``.

Le filtrage est volontairement strict : la CDX de Wayback renvoie un bruit
considérable (URLs `.image`, chaînes malformées du type ``%22http://...``) qu'il
vaut mieux écarter en amont plutôt que de polluer le dépôt.
"""

from __future__ import annotations

import posixpath
import re
import string
from urllib.parse import unquote, urljoin, urlsplit

from . import config

#: Caractères qui ne doivent jamais apparaître dans un chemin, une fois celui-ci
#: entièrement décodé. Attrape notamment les artefacts CDX ``%22http://...``.
_ILLEGAL_IN_PATH = re.compile(r"""[\s<>"'\\{}|^\[\]`]""")

#: Caractères « non réservés » au sens de la RFC 3986 : leur encodage percent est
#: superflu et doit être défait pour que deux écritures d'une même URL se
#: rejoignent.
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")

_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def _decode_unreserved(path: str) -> str:
    """Décode les seules séquences ``%XX`` correspondant à des caractères non
    réservés. Les autres (``%2F``, ``%3F``…) restent encodées : les décoder
    changerait la structure de l'URL."""

    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else match.group(0).upper()

    return _PCT.sub(repl, path)


def _canonical_host(netloc: str) -> str | None:
    """Hôte en minuscules, sans port par défaut ni userinfo. ``None`` si l'hôte
    est hors périmètre ou syntaxiquement suspect."""
    if "@" in netloc:  # userinfo : jamais légitime ici
        return None
    host = netloc.lower()
    if host.endswith(":80"):
        host = host[:-3]
    elif host.endswith(":443"):
        host = host[:-4]
    if ":" in host:  # port non standard
        return None
    host = config.HOST_ALIASES.get(host, host)
    return host if host in config.HOSTS else None


def _is_html_leaf(leaf: str) -> bool:
    """Le segment terminal désigne-t-il une page HTML ?

    Sans point, c'est une rubrique (dossier). Avec un point, l'extension doit
    figurer dans :data:`config.HTML_EXTENSIONS` — ce qui écarte ``.image``,
    ``.json``, ``.jpg`` et consorts.
    """
    if "." not in leaf:
        return True
    ext = leaf[leaf.rindex(".") :].lower()
    return ext in config.HTML_EXTENSIONS


def normalize(raw: str, base: str | None = None) -> str | None:
    """Retourne l'URL canonique, ou ``None`` si elle est hors périmètre.

    Une URL canonique est en ``https``, sur un hôte de :data:`config.HOSTS`,
    sans fragment ni query, avec un slash final si elle désigne une rubrique.

    ``base`` permet de résoudre un lien relatif rencontré pendant le crawl.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.startswith(("mailto:", "javascript:", "tel:", "#")):
        return None

    if base:
        raw = urljoin(base, raw)

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None

    host = _canonical_host(parts.netloc)
    if host is None:
        return None

    path = parts.path or "/"
    if "://" in path:  # artefact CDX : une URL imbriquée dans le chemin
        return None
    if _ILLEGAL_IN_PATH.search(unquote(path)):
        return None

    path = _decode_unreserved(path)

    # Résout `.`, `..` et les slashes doublés. normpath supprime le slash final,
    # qui est réappliqué plus bas selon la nature du segment terminal.
    path = posixpath.normpath(path)
    if path == ".":
        path = "/"
    if not path.startswith("/"):
        return None

    segments = [s for s in path.split("/") if s]
    if not segments:
        return f"https://{host}/"

    leaf = segments[-1]
    if not _is_html_leaf(leaf):
        return None

    joined = "/".join(segments)
    # Pas de point dans le segment terminal => rubrique => slash final.
    trailing = "" if "." in leaf else "/"
    return f"https://{host}/{joined}{trailing}"


def normalize_many(raws: object, base: str | None = None) -> list[str]:
    """Normalise un itérable d'URLs et supprime les doublons, en préservant
    l'ordre de première apparition."""
    seen: dict[str, None] = {}
    for raw in raws:  # type: ignore[union-attr]
        url = normalize(raw, base)
        if url is not None:
            seen.setdefault(url, None)
    return list(seen)
