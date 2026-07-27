"""Correspondance entre une URL canonique et son emplacement sur disque.

Principe : chaque segment du chemin devient un dossier, le segment terminal
devient une *ligne* dans le fichier d'index du dossier parent.

    https://www.rts.ch/info/suisse/2026/article/titre-29312521.html
    -> dossier data/www.rts.ch/info/suisse/2026/article/
       ligne   titre-29312521.html

Le mapping est délibérément simple : sur 679 URLs de rubriques réelles, la
profondeur maximale est de 5 segments, le segment le plus long fait 40
caractères, et aucun ne contient de majuscule. Pas de percent-encodage général
ni de troncature par hachage, donc — seulement ce qu'il faut pour que NTFS ne
fusionne rien en silence (voir :mod:`.store` pour le détecteur de collision).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import config

_WIN_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

#: Caractères que NTFS refuse dans un nom de fichier.
_ILLEGAL_FS = frozenset('<>:"|?*\\/') | {chr(c) for c in range(32)}

_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


class PathMappingError(ValueError):
    """Le chemin projeté viole une contrainte du système de fichiers."""


def escape_segment(segment: str) -> tuple[str, bool]:
    """Rend un segment d'URL utilisable comme nom de dossier.

    Retourne ``(segment_sûr, casse_modifiée)``. Le second élément permet à
    l'appelant de consigner l'anomalie : les URLs de rts.ch sont en minuscules,
    une majuscule mérite qu'on aille regarder.
    """
    lowered = segment.lower()
    case_changed = lowered != segment

    out: list[str] = []
    for char in lowered:
        if char == "%":
            # Échappé en premier pour que le décodage reste sans ambiguïté.
            out.append("%25")
        elif char in _ILLEGAL_FS:
            out.append(f"%{ord(char):02X}")
        else:
            out.append(char)
    safe = "".join(out)

    # Windows tronque silencieusement les points et espaces finaux.
    if safe and safe[-1] in ". ":
        safe = f"{safe[:-1]}%{ord(safe[-1]):02X}"

    # Noms de périphériques réservés : préfixe marqueur. Sans ambiguïté avec un
    # échappement, car aucun nom réservé ne commence par deux chiffres hexa.
    if safe.split(".")[0] in _WIN_RESERVED:
        safe = f"%{safe}"

    return safe, case_changed


def unescape_segment(segment: str) -> str:
    """Opération inverse de :func:`escape_segment` (à la casse près)."""
    if segment.startswith("%") and segment[1:].split(".")[0] in _WIN_RESERVED:
        segment = segment[1:]
    return _PCT.sub(lambda m: chr(int(m.group(1), 16)), segment)


def split_url(url: str) -> tuple[str, list[str], str | None]:
    """Éclate une URL canonique en ``(hôte, segments de dossier, feuille)``.

    La feuille vaut ``None`` pour une URL de rubrique (terminée par ``/``) :
    c'est le dossier lui-même qui porte l'URL.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if parts.path.endswith("/") or not segments:
        return parts.netloc, segments, None
    return parts.netloc, segments[:-1], segments[-1]


def url_to_location(url: str) -> tuple[str, str | None, bool]:
    """Retourne ``(chemin de dossier relatif à data/, feuille, casse_modifiée)``.

    Le chemin est en notation POSIX ; il est converti en :class:`pathlib.Path`
    au moment des entrées/sorties seulement, pour que la logique et les tests
    restent identiques sur toutes les plateformes.
    """
    host, dir_segments, leaf = split_url(url)

    case_changed = False
    safe_segments = []
    for segment in [host, *dir_segments]:
        safe, changed = escape_segment(segment)
        case_changed = case_changed or changed
        safe_segments.append(safe)

    relpath = "/".join(safe_segments)
    _check_length(relpath)
    return relpath, leaf, case_changed


def location_to_url(relpath: str, leaf: str | None = None) -> str:
    """Reconstruit l'URL depuis un chemin de dossier et une ligne de fichier."""
    segments = [unescape_segment(s) for s in relpath.split("/") if s]
    if not segments:
        raise PathMappingError(f"chemin vide: {relpath!r}")
    host, dir_segments = segments[0], segments[1:]
    path = "/".join(dir_segments)
    if leaf is None:
        return f"https://{host}/{path}/" if path else f"https://{host}/"
    prefix = f"{path}/" if path else ""
    return f"https://{host}/{prefix}{leaf}"


def _check_length(relpath: str) -> None:
    """Garde-fou MAX_PATH.

    Ne se déclenche pas aux profondeurs observées sur rts.ch, mais un clone sans
    ``git config core.longpaths true`` casserait au-delà de 260 caractères, y
    compris sur un Windows où ``LongPathsEnabled`` est actif.
    """
    projected = len(f"data/{relpath}/{config.INDEX_BASENAME}{config.INDEX_SUFFIX}")
    if projected > config.MAX_REL_PATH_LEN:
        raise PathMappingError(
            f"chemin trop long ({projected} > {config.MAX_REL_PATH_LEN}): data/{relpath}"
        )


def shard_key(slug: str) -> str:
    """Caractère de shard d'un slug, pour les dossiers dépassant le seuil."""
    first = slug[:1].lower()
    return first if first.isascii() and first.isalnum() else "_"
