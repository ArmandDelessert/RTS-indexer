"""Correspondance entre une URL canonique et son emplacement sur disque.

Principe : chaque segment du chemin devient un dossier, le segment terminal
devient une *ligne* dans le fichier d'index du dossier parent.

    https://www.rts.ch/info/suisse/2026/article/titre-29312521.html
    -> dossier data/www.rts.ch/info/suisse/2026/article/
       ligne   titre-29312521.html

Le mapping est délibérément simple : sur 679 URLs de rubriques réelles, la
profondeur maximale est de 5 segments et le segment le plus long fait 40
caractères. Pas de troncature par hachage, donc.

Une majuscule, en revanche, n'est *pas* sans conséquence : rts.ch a des
rubriques historiques sensibles à la casse (``/sport/dossiers/2012/JO_2012/``
répond, sa variante tout-minuscule ``jo_2012`` renvoie une 404). La mettre en
minuscule casserait la reconstruction d'une URL qui fonctionne. Chaque
majuscule est donc percent-encodée plutôt que perdue — ce qui, au passage,
règle aussi la collision NTFS sans qu'un détecteur séparé soit nécessaire pour
ce cas précis : deux segments qui ne diffèrent que par la casse produisent
deux noms de dossier différents (voir :mod:`.store` pour le détecteur de
collision, qui reste utile pour les vrais doublons du site).
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


def escape_segment(segment: str) -> str:
    """Rend un segment d'URL utilisable comme nom de dossier, casse comprise.

    Une majuscule ASCII est percent-encodée plutôt que mise en minuscule :
    voir la note de module sur ``JO_2012``. C'est réversible sans perte via
    :func:`unescape_segment`, et ça élimine par construction toute collision
    NTFS entre un segment et sa variante tout-minuscule.
    """
    out: list[str] = []
    for char in segment:
        if char == "%":
            # Échappé en premier pour que le décodage reste sans ambiguïté.
            out.append("%25")
        elif char.isascii() and char.isupper():
            out.append(f"%{ord(char):02X}")
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
    # Une variante en majuscules (CON, Con...) est déjà neutralisée par
    # l'échappement ci-dessus : seule la forme tout-minuscule peut encore
    # correspondre littéralement ici.
    if safe.split(".")[0] in _WIN_RESERVED:
        safe = f"%{safe}"

    return safe


def unescape_segment(segment: str) -> str:
    """Opération inverse exacte de :func:`escape_segment`."""
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


def url_to_location(url: str) -> tuple[str, str | None]:
    """Retourne ``(chemin de dossier relatif à data/, feuille)``.

    Le chemin est en notation POSIX ; il est converti en :class:`pathlib.Path`
    au moment des entrées/sorties seulement, pour que la logique et les tests
    restent identiques sur toutes les plateformes.
    """
    host, dir_segments, leaf = split_url(url)
    relpath = "/".join(escape_segment(segment) for segment in [host, *dir_segments])
    _check_length(relpath)
    return relpath, leaf


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
