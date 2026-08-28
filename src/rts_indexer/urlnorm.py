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
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from . import config

#: Caractères qui ne doivent jamais apparaître dans un chemin, une fois celui-ci
#: entièrement décodé. Attrape notamment les artefacts CDX ``%22http://...``,
#: les liens de partage social collés à la suite d'une URL rts.ch
#: (``.../l-ecole/&amp;title=...``) et les fragments de code JavaScript
#: archivés par Wayback comme s'ils étaient des URLs (``/;path=/;/``).
#: Aucun de ces caractères n'apparaît dans les 24'000 URLs légitimes déjà
#: indexées : leur présence signe un artefact d'extraction.
_ILLEGAL_IN_PATH = re.compile(r"""[\s<>"'\\{}|^\[\]`&;=]""")

#: Caractères « non réservés » au sens de la RFC 3986 : leur encodage percent est
#: superflu et doit être défait pour que deux écritures d'une même URL se
#: rejoignent.
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")

_PCT = re.compile(r"%([0-9A-Fa-f]{2})")

#: Une suite ininterrompue de séquences ``%XX`` : candidate à un décodage
#: UTF-8 complet (voir :func:`_decode_percent_utf8`).
_PCT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")

#: RTS Play route son contenu (articles, épisodes) sur un identifiant passé en
#: paramètre de requête (``?urn=...`` ou ``?id=...``), toujours retiré par cette
#: normalisation — indexer ces URLs revient donc à indexer des pages cassées
#: (redirection vers ``/play/not-found``). Confirmé par sondage direct sur la
#: quasi-totalité de ``/play/`` (129'068 URLs sur 129'069 lors de l'audit).
#: Seules ces pages statiques, qui ne dépendent d'aucun paramètre, sont
#: réellement fonctionnelles ; tout le reste de ``/play/`` est exclu du
#: périmètre plutôt que d'être indexé pour finir mort.
_PLAY_ALLOWLIST = frozenset(
    {
        "play/embed",
        "play/legacy-browser",
        "play/recherche",
        "play/tv",
        "play/tv/agid",
        "play/tv/aide",
        "play/tv/configuraziuns",
        "play/tv/einstellungen",
        "play/tv/emissions",
        "play/tv/emissiuns",
        "play/tv/favorite-guide-shows",
        "play/tv/favorite-guide-topics",
        "play/tv/guida",
        "play/tv/hilfe",
        "play/tv/i-miei-video",
        "play/tv/impostazioni",
        "play/tv/meine-videos",
        "play/tv/mes-videos",
        "play/tv/parametres",
        "play/tv/popupvideoplayer",
        "play/tv/programme-par-chaine",
        "play/tv/programmi",
        "play/tv/rtr-livestreams",
        "play/tv/rts-livestreams",
        "play/tv/sport-livestreams",
        "play/tv/streaming",
    }
)

#: Aucune page rts.ch légitime n'approche cette longueur (les navigateurs et la
#: plupart des serveurs plafonnent déjà autour de 2000-8000 caractères) ; un
#: candidat plus long est un artefact d'extraction (bloc JS minifié, texte
#: collé sans séparateur...). Rejeter tôt évite de faire tourner le reste de la
#: normalisation sur des chaînes de plusieurs kilo-octets pour rien.
_MAX_RAW_LEN = 2048


def _decode_unreserved(path: str) -> str:
    """Décode les seules séquences ``%XX`` correspondant à des caractères non
    réservés. Les autres (``%2F``, ``%3F``…) restent encodées : les décoder
    changerait la structure de l'URL."""

    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else match.group(0).upper()

    return _PCT.sub(repl, path)


def _decode_percent_utf8(path: str) -> str:
    """Décode un octet non-ASCII percent-encodé (ex. ``%E2%80%A6``) en le
    caractère Unicode réel qu'il représente (``…``), plutôt que de le laisser
    tel quel dans le chemin.

    Sans ça, une ellipse dans une URL archivée survit sous forme d'une suite
    de triplets pourcent — et le pire, c'est que :mod:`.pathmap` échappe
    ensuite *chaque lettre hexadécimale majuscule* de cette suite comme s'il
    s'agissait d'une vraie majuscule à préserver, produisant un nom de dossier
    doublement échappé et illisible (constaté en réel :
    ``%25%452%2580%25%416`` sur disque pour un simple « … »).

    Seuls les octets non-ASCII (``>= 0x80``) sont concernés : un octet ASCII
    (``%2F`` pour ``/``, par exemple) reste structurel et ne doit surtout pas
    être décodé ici, sous peine de changer le nombre de segments du chemin.
    Une suite invalide en UTF-8 est laissée intacte, par prudence.
    """

    def repl(match: re.Match[str]) -> str:
        octets = bytes(int(h, 16) for h in re.findall(r"%([0-9A-Fa-f]{2})", match.group(0)))
        if any(o < 0x80 for o in octets):
            return match.group(0)
        try:
            return octets.decode("utf-8")
        except UnicodeDecodeError:
            return match.group(0)

    return _PCT_RUN.sub(repl, path)


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


#: Identifiant en fin de valeur ``urn`` (ex. ``urn:rts:video:2045231`` -> ``2045231``).
_URN_ID = re.compile(r":(\d+)$")


def _play_id_from_query(path: str, query: str) -> str | None:
    """Identifiant RTS extrait de ``?id=<n>`` ou ``?urn=urn:rts:...:<n>`` sur
    une URL ``/play/``.

    RTS Play route son contenu sur ce paramètre, que ``normalize()`` retire
    partout ailleurs (aucune query string dans une URL canonique). Plutôt que
    de perdre l'information, on la convertit en ``https://www.rts.ch/a/<id>/``
    — le même identifiant, résolu vers la bonne page par RTS lui-même, sans
    dépendre d'un paramètre qu'on ne conserve jamais.
    """
    stripped = path.strip("/")
    if stripped != "play" and not stripped.startswith("play/"):
        return None
    params = parse_qs(query)
    id_values = params.get("id")
    if id_values and id_values[0].isdigit():
        return id_values[0]
    urn_values = params.get("urn")
    if urn_values:
        m = _URN_ID.search(urn_values[0])
        if m:
            return m.group(1)
    return None


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
    if len(raw) > _MAX_RAW_LEN:
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
    path = _decode_percent_utf8(path)

    if parts.query:
        play_id = _play_id_from_query(path, parts.query)
        if play_id is not None:
            return f"https://{host}/a/{play_id}/"

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
    if (joined == "play" or joined.startswith("play/")) and joined not in _PLAY_ALLOWLIST:
        return None

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
