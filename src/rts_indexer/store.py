"""Lecture et écriture de l'arborescence ``/data``.

``/data`` *est* la base de données : chaque source la charge, y ajoute ses
trouvailles, puis la réécrit intégralement. Cette réécriture complète est ce qui
rend le sharding trivial (aucune logique incrémentale de split/merge) et le
résultat déterministe — un second run sans nouveauté amont doit produire un
``git diff`` vide.

Format d'un fichier d'index :

    ./
    !vieux-article-7422738.html
    titre-29312521.html

* ``./`` en tête : le dossier lui-même est une URL valide.
* ``!`` en préfixe : URL confirmée morte (404/410).
* Tri byte-wise **sur le slug nu**, sigil exclu — une URL qui meurt produit donc
  une ligne modifiée sur place, et non une suppression suivie d'un ajout.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, fsutil, pathmap

log = logging.getLogger(__name__)


@dataclass
class DirIndex:
    """Contenu indexé d'un dossier."""

    #: slug -> mort ?
    slugs: dict[str, bool] = field(default_factory=dict)
    #: le dossier lui-même est-il une URL valide ?
    is_page: bool = False

    def __len__(self) -> int:
        return len(self.slugs)


class Store:
    """Index complet en mémoire, adossé à l'arborescence ``/data``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR
        self.dirs: dict[str, DirIndex] = {}
        #: chemin disque -> chemin d'URL d'origine, pour détecter les collisions
        self._dir_source: dict[str, str] = {}
        #: (type, url, détail) — consignées dans _anomalies.tsv
        self.anomalies: set[tuple[str, str, str]] = set()
        self.added = 0

    # -- alimentation --------------------------------------------------------

    def add(self, url: str, *, dead: bool | None = None) -> bool:
        """Ajoute une URL canonique. Retourne ``True`` si elle était inconnue.

        ``dead`` à ``None`` laisse le statut existant inchangé, ce qui permet à
        une source de réaffirmer une URL sans écraser le verdict de ``verify``.

        Une URL individuelle dont le chemin projeté dépasse
        :data:`config.MAX_REL_PATH_LEN` est journalisée puis ignorée plutôt que
        de faire échouer l'appelant : lors d'un crawl de plusieurs centaines de
        pages, perdre tout le travail déjà accompli pour une seule page Play au
        slug démesuré (URLs de type ``play/tv/.../<slug-phrase-entiere>/``,
        sans borne de longueur connue) coûte bien plus cher que d'ignorer cette
        page et de continuer.
        """
        try:
            relpath, leaf = pathmap.url_to_location(url)
        except pathmap.PathMappingError as exc:
            self.anomalies.add(("trop_long", url, str(exc)))
            log.warning("URL ignorée (%s): %s", exc, url)
            return False

        # Deux URLs qui ne diffèrent que par la casse produisent désormais des
        # chemins différents (voir pathmap.escape_segment), donc plus de faux
        # positif ici. Ce détecteur ne réagit plus qu'à un vrai doublon du
        # site (ex. deux chemins réellement identiques servis par des routes
        # distinctes) — rare, mais pas impossible.
        source = url.rstrip("/") if leaf is None else url.rsplit("/", 1)[0]
        previous = self._dir_source.setdefault(relpath, source)
        if previous != source:
            # NTFS étant insensible à la casse, laisser passer fusionnerait
            # silencieusement deux rubriques distinctes. On journalise et on
            # ignore plutôt que de faire échouer tout un run pour cette seule
            # URL : un crawl de plusieurs centaines de pages ne doit jamais
            # perdre son travail pour un cas qui se compte à l'unité.
            self.anomalies.add(("collision", url, f"{previous} vs {source}"))
            log.warning("collision ignorée: %r et %r visent %r", previous, source, relpath)
            return False

        entry = self.dirs.setdefault(relpath, DirIndex())
        if leaf is None:
            is_new = not entry.is_page
            entry.is_page = True
        else:
            is_new = leaf not in entry.slugs
            entry.slugs[leaf] = bool(dead) if dead is not None else entry.slugs.get(leaf, False)

        self.added += is_new
        return is_new

    def add_many(self, urls: object) -> int:
        """Ajoute un itérable d'URLs, retourne le nombre de nouveautés."""
        return sum(self.add(url) for url in urls)  # type: ignore[union-attr]

    # -- chargement ----------------------------------------------------------

    def load(self) -> Store:
        """Relit l'arborescence existante. Sans effet si ``/data`` est absent.

        Un fichier isolé illisible (verrou transitoire déjà épuisé, encodage
        corrompu par une écriture interrompue) est journalisé et ignoré plutôt
        que de faire échouer la relecture de tout le dépôt : un seul dossier
        abîmé ne doit pas rendre l'index entier inexploitable.
        """
        if not self.data_dir.is_dir():
            return self
        for path in sorted(self.data_dir.rglob(f"{config.INDEX_BASENAME}*{config.INDEX_SUFFIX}")):
            relpath = path.parent.relative_to(self.data_dir).as_posix()
            try:
                content = fsutil.read_text(path)
            except (PermissionError, UnicodeDecodeError) as exc:
                log.warning("%s illisible (%s), dossier ignoré", path, exc)
                continue
            entry = self.dirs.setdefault(relpath, DirIndex())
            # Le chemin sur disque préserve désormais la casse d'origine
            # (percent-encodée) : on peut reconstruire la source sans perte,
            # et donc détecter une collision dès le premier ajout d'un run,
            # pas seulement pour les entrées déjà signalées par le passé.
            try:
                self._dir_source.setdefault(relpath, pathmap.location_to_url(relpath).rstrip("/"))
            except pathmap.PathMappingError:
                pass
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line == config.SELF_LINE:
                    entry.is_page = True
                elif line.startswith(config.DEAD_SIGIL):
                    entry.slugs[line[len(config.DEAD_SIGIL) :]] = True
                else:
                    entry.slugs[line] = False
        self._load_anomalies()
        return self

    def _load_anomalies(self) -> None:
        """Recharge le journal d'anomalies, en ne gardant que celles encore
        valables.

        Sans ça, une anomalie résolue (une collision qui ne collisionne plus,
        par exemple depuis que la casse est préservée) ou d'un type retiré du
        code (l'ancienne détection de majuscule) s'éterniserait dans
        ``_anomalies.tsv`` indéfiniment, un ``build`` se contentant de la
        recopier sans jamais la revérifier.
        """
        path = self.data_dir / config.ANOMALIES_FILE
        if not path.is_file():
            return
        try:
            content = fsutil.read_text(path)
        except (PermissionError, UnicodeDecodeError) as exc:
            log.warning("%s illisible (%s), journal d'anomalies ignoré", path, exc)
            return
        for line in content.splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            kind, url, detail = fields
            if self._anomaly_still_applies(kind, url):
                self.anomalies.add((kind, url, detail))

    def _anomaly_still_applies(self, kind: str, url: str) -> bool:
        if kind not in ("collision", "trop_long"):
            return False  # type retiré du code (ex. l'ancienne "majuscule")
        try:
            relpath, leaf = pathmap.url_to_location(url)
        except pathmap.PathMappingError:
            return kind == "trop_long"
        if kind == "trop_long":
            return False  # n'est plus trop long (ex. seuil relevé depuis)
        source = url.rstrip("/") if leaf is None else url.rsplit("/", 1)[0]
        return relpath in self._dir_source and self._dir_source[relpath] != source

    # -- écriture ------------------------------------------------------------

    def write(self) -> dict[str, int]:
        """Réécrit intégralement ``/data`` et retourne les statistiques."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        written: set[Path] = set()

        for relpath, entry in sorted(self.dirs.items()):
            if not entry.slugs and not entry.is_page:
                continue
            directory = self.data_dir / Path(*relpath.split("/"))
            directory.mkdir(parents=True, exist_ok=True)
            written |= self._write_dir(directory, entry)

        self._prune(written)
        self._write_anomalies()
        return self._write_stats()

    def _write_dir(self, directory: Path, entry: DirIndex) -> set[Path]:
        """Écrit le ou les fichiers d'index d'un dossier."""
        files: dict[str, list[str]] = {}

        if len(entry) > config.SHARD_THRESHOLD:
            # Le marqueur `./` reste toujours dans _index.txt, qui fait office
            # d'en-tête ; les slugs partent dans _index.<caractère>.txt.
            if entry.is_page:
                files[""] = [config.SELF_LINE]
            for slug in sorted(entry.slugs):
                files.setdefault(f".{pathmap.shard_key(slug)}", []).append(_render(slug, entry))
        else:
            lines = [config.SELF_LINE] if entry.is_page else []
            lines += [_render(slug, entry) for slug in sorted(entry.slugs)]
            files[""] = lines

        # Purge des index précédents : le sharding peut apparaître ou disparaître
        # d'un run à l'autre, on ne veut pas de fichier orphelin.
        for stale in directory.glob(f"{config.INDEX_BASENAME}*{config.INDEX_SUFFIX}"):
            fsutil.unlink(stale)

        written = set()
        for infix, lines in files.items():
            path = directory / f"{config.INDEX_BASENAME}{infix}{config.INDEX_SUFFIX}"
            _write_lines(path, lines)
            written.add(path)
        return written

    def _prune(self, written: set[Path]) -> None:
        """Supprime les index devenus obsolètes, puis les dossiers vides."""
        pattern = f"{config.INDEX_BASENAME}*{config.INDEX_SUFFIX}"
        for path in self.data_dir.rglob(pattern):
            if path not in written:
                fsutil.unlink(path)
        for directory in sorted(self.data_dir.rglob("*"), key=lambda p: -len(p.parts)):
            if directory.is_dir() and not any(directory.iterdir()):
                fsutil.rmdir(directory)

    def _write_anomalies(self) -> None:
        path = self.data_dir / config.ANOMALIES_FILE
        if not self.anomalies:
            fsutil.unlink(path)
            return
        rows = ["type\turl\tdetail"]
        rows += ["\t".join(row) for row in sorted(self.anomalies)]
        _write_lines(path, rows)

    def _write_stats(self) -> dict[str, int]:
        stats = self.stats()
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **stats,
            "par_hote": self._counts_by_host(),
        }
        path = self.data_dir / config.STATS_FILE
        fsutil.write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        return stats

    # -- statistiques --------------------------------------------------------

    def stats(self) -> dict[str, int]:
        total = sum(len(e) + e.is_page for e in self.dirs.values())
        dead = sum(sum(e.slugs.values()) for e in self.dirs.values())
        return {
            "urls": total,
            "vivantes_ou_non_verifiees": total - dead,
            "mortes": dead,
            "dossiers": len(self.dirs),
            "anomalies": len(self.anomalies),
        }

    def _counts_by_host(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for relpath, entry in self.dirs.items():
            host = relpath.split("/", 1)[0]
            counts[host] = counts.get(host, 0) + len(entry) + entry.is_page
        return dict(sorted(counts.items()))

    def urls(self):
        """Itère sur toutes les URLs indexées, sous forme ``(url, morte)``."""
        for relpath, entry in sorted(self.dirs.items()):
            if entry.is_page:
                yield pathmap.location_to_url(relpath), False
            for slug in sorted(entry.slugs):
                yield pathmap.location_to_url(relpath, slug), entry.slugs[slug]


def _render(slug: str, entry: DirIndex) -> str:
    return f"{config.DEAD_SIGIL}{slug}" if entry.slugs[slug] else slug


def _write_lines(path: Path, lines: list[str]) -> None:
    """Écrit en LF explicite : sous Windows le mode texte produirait du CRLF et
    ferait diverger le dépôt à chaque run."""
    fsutil.write_text(path, "\n".join(lines) + "\n" if lines else "")
