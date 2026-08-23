"""Lecture et écriture de l'arborescence ``/data``.

``/data`` *est* la base de données : chaque source la charge, y ajoute ses
trouvailles, puis la réécrit. Le contenu d'un dossier est toujours **recalculé
en entier** — aucune logique incrémentale de split/merge de shard — ce qui rend
le sharding trivial et le résultat déterministe : un second run sans nouveauté
amont doit produire un ``git diff`` vide.

En revanche, un dossier dont le contenu n'a pas changé depuis le chargement
n'est plus réécrit du tout (voir :meth:`Store.write`). Réécrire 138'000
fichiers avec un contenu identique pour en modifier trois coûtait plusieurs
minutes à chaque commande.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config, fsutil, pathmap, urlnorm

log = logging.getLogger(__name__)

#: Nombre de paliers de progression affichés sur un parcours complet (load,
#: write). Un index de 656'000 URLs prend plusieurs minutes sans le moindre
#: signe de vie sinon — c'est ce qui, en pratique, ressemble à un blocage.
_PROGRESS_STEPS = 20


def _progress(label: str, done: int, total: int) -> None:
    step = max(1, total // _PROGRESS_STEPS)
    if done == total or done % step == 0:
        log.info("%s: %d/%d (%.0f%%)", label, done, total, 100 * done / total)


@dataclass
class DirIndex:
    """Contenu indexé d'un dossier."""

    #: slug -> mort ?
    slugs: dict[str, bool] = field(default_factory=dict)
    #: ``None`` si le dossier n'est pas lui-même une URL ; sinon, morte ou non.
    #: Un simple booléen ne suffisait pas : il confondait « ce dossier n'est
    #: pas une page » et « cette page est vivante », ce qui rendait toute
    #: rubrique impossible à marquer morte.
    page_dead: bool | None = None

    @property
    def is_page(self) -> bool:
        return self.page_dead is not None

    def __len__(self) -> int:
        return len(self.slugs)


class Store:
    """Index complet en mémoire, adossé à l'arborescence ``/data``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR
        self.dirs: dict[str, DirIndex] = {}
        #: chemin disque -> chemin d'URL d'origine, pour détecter les collisions
        self._dir_source: dict[str, str] = {}
        #: relpath -> fichiers d'index réellement **observés** sur disque pour ce
        #: dossier (au chargement, puis après chaque écriture). C'est la clé de
        #: l'écriture sélective : pour un dossier inchangé, on réutilise ces
        #: chemins constatés plutôt que de recalculer ceux qui *devraient*
        #: exister. Un tel recalcul, s'il divergeait de la réalité ne serait-ce
        #: que sur un cas de sharding, ferait supprimer les fichiers concernés
        #: par :meth:`_prune` — une perte de données, pas une simple lenteur.
        self._disk_files: dict[str, set[Path]] = {}
        #: relpaths dont le contenu a changé depuis le dernier chargement ou la
        #: dernière écriture. Seuls ceux-là sont réécrits.
        self._dirty: set[str] = set()
        #: ``load()`` a-t-il été appelé ? La purge ciblée repose entièrement sur
        #: la connaissance du disque accumulée au chargement : sans lui,
        #: ``_disk_files`` est vide et *tout* fichier présent paraîtrait
        #: légitime alors qu'aucun ne l'est. On retombe alors sur le balayage
        #: complet, seul régime correct en l'absence de cette connaissance.
        self._loaded = False
        #: relpaths dont au moins un fichier n'a pas pu être lu au chargement.
        #: Leur contenu est perdu pour cette session : le fichier n'a nourri ni
        #: ``dirs`` ni ``_disk_files``, il est donc devenu orphelin et doit être
        #: purgé. C'est la seule source d'orphelin que le code produise lui-même,
        #: et la raison pour laquelle une purge ciblée suffit en temps normal.
        self._unreadable: set[str] = set()
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
        # `avant` sert à distinguer une URL *réaffirmée* (cas de très loin le
        # plus fréquent : un crawl repasse sur des milliers de liens déjà
        # connus) d'un changement réel. Seul le second salit le dossier et
        # justifie de le réécrire.
        if leaf is None:
            avant = entry.page_dead
            is_new = avant is None
            entry.page_dead = bool(dead) if dead is not None else (entry.page_dead or False)
            modifie = entry.page_dead != avant
        else:
            avant = entry.slugs.get(leaf)
            is_new = leaf not in entry.slugs
            entry.slugs[leaf] = bool(dead) if dead is not None else entry.slugs.get(leaf, False)
            modifie = entry.slugs[leaf] != avant

        if modifie:
            self._dirty.add(relpath)
        self.added += is_new
        return is_new

    def add_many(self, urls: object) -> int:
        """Ajoute un itérable d'URLs, retourne le nombre de nouveautés."""
        return sum(self.add(url) for url in urls)  # type: ignore[union-attr]

    def remove(self, url: str) -> bool:
        """Retire une URL de l'index. Retourne ``True`` si elle existait.

        Contrairement à ``add()``, qui ne fait qu'ajouter : c'est la seule
        façon de faire disparaître une URL, utilisée par :meth:`resolve_doublons`
        (doublon confirmé, une autre URL déjà indexée fait double emploi) et
        par :meth:`purge_dead` (URL confirmée morte, à la demande explicite —
        par défaut le sigil ``!`` suffit et rien n'est jamais supprimé).
        Marque le dossier sale :
        l'invariant de l'écriture sélective (« un dossier inchangé a les
        bons fichiers sur disque ») reste vrai puisque ce dossier n'est
        justement plus inchangé. Si le dossier devient entièrement vide,
        ``write()`` s'en aperçoit déjà de lui-même (``entry.slugs`` et
        ``entry.is_page`` vides) et le fait purger.
        """
        try:
            relpath, leaf = pathmap.url_to_location(url)
        except pathmap.PathMappingError:
            return False
        entry = self.dirs.get(relpath)
        if entry is None:
            return False
        if leaf is None:
            if entry.page_dead is None:
                return False
            entry.page_dead = None
        else:
            if leaf not in entry.slugs:
                return False
            del entry.slugs[leaf]
        self._dirty.add(relpath)
        return True

    def resolve_doublons(self) -> tuple[int, int, int]:
        """Supprime les URLs journalisées comme doublons par ``verify``.

        La cible (l'URL vers laquelle le serveur redirige) est indexée si
        elle ne l'est pas déjà : ``verify`` a obtenu un vrai 200 dessus au
        moment de constater la redirection, ce n'est pas une supposition.
        Mais on ne lui fait confiance qu'après l'avoir fait passer par
        :func:`urlnorm.normalize`, le même filtre de périmètre que toutes
        les autres sources — sans ça, un identifiant qui redirige vers une
        image sur ``img.rts.ch`` (hors périmètre, hors format HTML)
        entrerait dans l'index sans contrôle, puisque ``add()`` seul ne
        vérifie ni l'hôte ni l'extension.

        Un doublon dont la cible échoue à ce filtre est ignoré, pas
        supprimé : mieux vaut conserver un doublon en trop que perdre une
        URL dont la destination s'avère hors périmètre.

        Retourne ``(supprimés, cibles ajoutées, ignorés)``.
        """
        doublons = [(u, cible) for genre, u, cible in self.anomalies if genre == "doublon"]
        supprimes = ajoutees = ignores = 0
        for url, cible_brute in doublons:
            cible = urlnorm.normalize(cible_brute)
            self.anomalies.discard(("doublon", url, cible_brute))
            if cible is None:
                # Pas un doublon : rien dans l'index ne fait double emploi
                # avec cette URL, elle redirige simplement hors périmètre
                # (image sur img.rts.ch, domaine tiers...). Requalifiée
                # plutôt que laissée sous une étiquette trompeuse — c'est
                # justement la confusion que ça a causée en pratique.
                log.warning("doublon requalifié, hors périmètre : %s -> %s", url, cible_brute)
                self.anomalies.add(("hors_perimetre", url, cible_brute))
                ignores += 1
                continue
            if self.status(cible) is None:
                self.add(cible)
                ajoutees += 1
            if self.remove(url):
                supprimes += 1
        return supprimes, ajoutees, ignores

    def purge_dead(self) -> int:
        """Supprime de l'index toutes les URLs actuellement marquées mortes.

        Matérialise le sigil ``!`` en suppression réelle. Ce n'est pas le
        comportement par défaut du projet : une URL morte reste indexée
        (choix délibéré, pour garder la trace qu'un contenu a existé même
        après sa disparition — l'intérêt d'un index qui couvre aussi
        l'historique, pas seulement ce qui répond aujourd'hui). Cette méthode
        n'existe que pour qui préfère explicitement un index de contenu
        vivant ; l'historique reste de toute façon récupérable via Git.

        Ne recontrôle rien — se fie au sigil tel qu'il est en mémoire au
        moment de l'appel. À utiliser après un ``verify --dead-only``, pas à
        la place.
        """
        a_supprimer = [url for url, dead in self.urls() if dead]
        for url in a_supprimer:
            self.remove(url)
        return len(a_supprimer)

    # -- chargement ----------------------------------------------------------

    def load(self) -> Store:
        """Relit l'arborescence existante. Sans effet si ``/data`` est absent.

        Un fichier isolé illisible (verrou transitoire déjà épuisé, encodage
        corrompu par une écriture interrompue) est journalisé et ignoré plutôt
        que de faire échouer la relecture de tout le dépôt : un seul dossier
        abîmé ne doit pas rendre l'index entier inexploitable.

        Le ``FileNotFoundError`` couvre un cas bien réel : un autre run en
        cours peut supprimer un fichier entre son listage et sa lecture, la
        réécriture purgeant les index devenus obsolètes.
        """
        # Posé avant le retour anticipé : un `data/` absent est une information
        # sur le disque, pas une absence d'information — il n'y a rien à purger.
        self._loaded = True
        if not self.data_dir.is_dir():
            return self
        # `sorted()` matérialise déjà tout le résultat : ce parcours de l'arbre
        # est lui-même une phase à part entière, avant même la première lecture.
        log.info("recherche des fichiers d'index sous %s...", self.data_dir)
        paths = sorted(self.data_dir.rglob(f"{config.INDEX_BASENAME}*{config.INDEX_SUFFIX}"))
        total = len(paths)
        log.info("%d fichiers d'index trouvés, lecture...", total)
        for done, path in enumerate(paths, 1):
            relpath = path.parent.relative_to(self.data_dir).as_posix()
            try:
                content = fsutil.read_text(path)
            except (PermissionError, UnicodeDecodeError, FileNotFoundError) as exc:
                log.warning("%s illisible (%s), dossier ignoré", path, exc)
                self._unreadable.add(relpath)
                continue
            # Après la lecture seulement : un fichier illisible n'a pas nourri
            # `dirs`, il ne doit donc pas être retenu comme légitime — sans quoi
            # `_prune` le conserverait alors que son contenu est perdu.
            self._disk_files.setdefault(relpath, set()).add(path)
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
                    entry.page_dead = False
                elif line == f"{config.DEAD_SIGIL}{config.SELF_LINE}":
                    entry.page_dead = True
                elif line.startswith(config.DEAD_SIGIL):
                    entry.slugs[line[len(config.DEAD_SIGIL) :]] = True
                else:
                    entry.slugs[line] = False
            _progress("chargement", done, total)
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
        except (PermissionError, UnicodeDecodeError, FileNotFoundError) as exc:
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
        if kind in ("doublon", "hors_perimetre", "code_atypique"):
            # Vaut tant que l'URL reste indexée. Contrairement aux deux
            # suivants, ce verdict vient du réseau (une redirection constatée
            # par `verify`) et ne peut pas être rejoué hors ligne : le
            # conserver est le seul moyen de ne pas perdre l'information entre
            # deux runs. `hors_perimetre` et `code_atypique` ne seront jamais
            # résolus par `dedupe` (voir :meth:`resolve_doublons`), mais
            # restent sujets à disparaître si l'URL source elle-même finit
            # par être retirée de l'index.
            return self.status(url) is not None
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

    def write(self, *, force: bool = False) -> dict[str, int]:
        """Écrit ``/data`` et retourne les statistiques.

        Seuls les dossiers dont le contenu a changé depuis le chargement sont
        réécrits ; pour les autres, on réutilise les chemins de fichiers
        observés sur disque (:attr:`_disk_files`), qui sont donc bien comptés
        comme légitimes par :meth:`_prune`. Un dossier inchangé n'est ni
        rouvert, ni ré-écrit, ni même parcouru par un ``glob``.

        ``force`` réécrit tout, sans considération de propreté, **et** déclenche
        un balayage complet de l'arbre à la purge. C'est le rôle de ``build`` :
        un changement de :data:`config.SHARD_THRESHOLD` ou de la projection des
        chemins ne salit aucun dossier — rien en mémoire n'a bougé — et ne
        serait donc jamais appliqué autrement. C'est aussi la seule commande qui
        rattrape une dérive externe (fichier ajouté à la main dans ``data/``,
        reste d'une fusion Git, débris d'une écriture interrompue).
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        written: set[Path] = set()
        reecrits = 0
        # Dossiers à examiner lors d'une purge ciblée. Les dossiers réécrits
        # n'y figurent pas : _write_dir purge déjà les siens au passage.
        a_purger: set[str] = set(self._unreadable)

        items = sorted(self.dirs.items())
        total = len(items)
        log.info("écriture de %d dossiers%s...", total, " (forcée)" if force else "")
        for done, (relpath, entry) in enumerate(items, 1):
            _progress("écriture", done, total)
            if not entry.slugs and not entry.is_page:
                # Rien à écrire ; mais si ce dossier avait des fichiers sur
                # disque, ils viennent de perdre leur raison d'être.
                if relpath in self._disk_files:
                    a_purger.add(relpath)
                continue
            connus = self._disk_files.get(relpath)
            if not force and connus and relpath not in self._dirty:
                written |= connus
                continue
            directory = self.data_dir / Path(*relpath.split("/"))
            directory.mkdir(parents=True, exist_ok=True)
            fichiers = self._write_dir(directory, entry)
            self._disk_files[relpath] = fichiers
            written |= fichiers
            reecrits += 1

        log.info("%d dossiers réécrits, %d inchangés", reecrits, total - reecrits)

        self._prune(written, cibles=a_purger, complet=force or not self._loaded)
        self._write_anomalies()
        stats = self._write_stats()
        # Le disque reflète désormais la mémoire : plus rien n'est en attente.
        # Indispensable pour les checkpoints (crawl, verify), qui appellent
        # write() en boucle et ne doivent réécrire que le delta de chaque
        # tranche, pas tout l'index à chaque fois.
        self._dirty.clear()
        self._unreadable.clear()
        return stats

    def _write_dir(self, directory: Path, entry: DirIndex) -> set[Path]:
        """Écrit le ou les fichiers d'index d'un dossier."""
        files: dict[str, list[str]] = {}

        if len(entry) > config.SHARD_THRESHOLD:
            # Le marqueur `./` reste toujours dans _index.txt, qui fait office
            # d'en-tête ; les slugs partent dans _index.<caractère>.txt.
            if entry.is_page:
                files[""] = [_render_self(entry)]
            for slug in sorted(entry.slugs):
                files.setdefault(f".{pathmap.shard_key(slug)}", []).append(_render(slug, entry))
        else:
            lines = [_render_self(entry)] if entry.is_page else []
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

    def _prune(self, written: set[Path], *, cibles: set[str], complet: bool) -> None:
        """Supprime les index devenus obsolètes, puis les dossiers vides.

        Deux régimes, pour un compromis assumé entre vitesse et garantie :

        * **ciblé** (défaut) — n'examine que ``cibles``, les rares dossiers dont
          on sait qu'ils peuvent porter un orphelin. Les dossiers réécrits se
          purgent déjà eux-mêmes dans :meth:`_write_dir`, et un dossier
          inchangé n'a par construction rien à purger, puisqu'aucune URL n'est
          jamais retirée de l'index.
        * **complet** — balaye tout l'arbre, comme avant. Deux ``rglob`` sur
          138'000 dossiers, soit l'essentiel du coût d'une écriture une fois
          celle-ci devenue sélective. Seul régime capable de rattraper une
          dérive externe (fichier déposé à la main, débris d'un run interrompu),
          d'où sa place dans ``build`` plutôt qu'à chaque commande.
        """
        pattern = f"{config.INDEX_BASENAME}*{config.INDEX_SUFFIX}"

        if complet:
            log.info("purge : balayage complet de l'arbre...")
            a_supprimer = [
                (self._action_unlink(path), str(path))
                for path in self.data_dir.rglob(pattern)
                if path not in written
            ]
            supprimes = self._executer_par_lots(a_supprimer, "fichier")
            log.info("%d fichiers d'index obsolètes supprimés", supprimes)

            log.info("recherche des dossiers vides...")
            candidats = {p for p in self.data_dir.rglob("*") if p.is_dir()}
            vides = self._purger_dossiers_vides(candidats)
            log.info("%d dossiers vides supprimés", vides)
            return

        if not cibles:
            log.info("purge : aucun dossier suspect, rien à balayer")
            return

        log.info("purge ciblée : %d dossiers suspects", len(cibles))
        a_supprimer = []
        dossiers = set()
        for relpath in sorted(cibles):
            directory = self.data_dir / Path(*relpath.split("/"))
            if not directory.is_dir():
                continue
            dossiers.add(directory)
            for path in directory.glob(pattern):
                if path not in written:
                    a_supprimer.append((self._action_unlink(path), str(path)))

        supprimes = self._executer_par_lots(a_supprimer, "fichier")
        vides = self._purger_dossiers_vides(dossiers)
        log.info("%d fichiers supprimés, %d dossiers vides supprimés", supprimes, vides)

    @staticmethod
    def _action_unlink(path: Path) -> Callable[[], None]:
        return lambda: path.unlink(missing_ok=True)

    @staticmethod
    def _executer_par_lots(actions: list[tuple[Callable[[], None], str]], nature: str) -> int:
        """Exécute des suppressions indépendantes par lots (voir
        :func:`fsutil.retry_many`) et journalise clairement ce qui échoue
        encore après tous les tours, plutôt que de l'avaler en silence.

        Retourne le nombre de réussites.
        """
        echecs = fsutil.retry_many(actions)
        for desc in echecs:
            log.warning(
                "%s non supprimé après plusieurs tentatives, laissé en place : %s",
                nature,
                desc,
            )
        return len(actions) - len(echecs)

    def _purger_dossiers_vides(self, depart: set[Path]) -> int:
        """Supprime les dossiers devenus vides, en remontant vers la racine
        par lots plutôt qu'un par un.

        Un dossier qui vient de se vider peut rendre son parent vide à son
        tour, potentiellement sur plusieurs niveaux : chaque tour de la
        boucle correspond à un niveau de profondeur, balayé d'un coup pour
        tous les dossiers candidats plutôt que dossier par dossier — c'est
        ce qui a permis de passer de plusieurs heures (chaque suppression
        épuisant ses propres tentatives avant de passer à la suivante) à
        quelques tours au total, la durée d'attente ne dépendant plus du
        nombre de dossiers mais de la profondeur de l'arbre.
        """
        total = 0
        a_examiner = {d for d in depart if d != self.data_dir and self.data_dir in d.parents}
        while a_examiner:
            candidats = [
                (self._action_rmdir(d), str(d))
                for d in a_examiner
                if d.is_dir() and not any(d.iterdir())
            ]
            if not candidats:
                break
            echecs = set(fsutil.retry_many(candidats))
            for desc in echecs:
                log.warning(
                    "dossier non supprimé après plusieurs tentatives, laissé en place : %s", desc
                )
            reussis = {Path(desc) for _, desc in candidats if desc not in echecs}
            total += len(reussis)
            a_examiner = {
                d.parent for d in reussis if d.parent != self.data_dir and self.data_dir in d.parents
            }
        return total

    @staticmethod
    def _action_rmdir(path: Path) -> Callable[[], None]:
        return path.rmdir

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
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **stats,
            "par_hote": self._counts_by_host(),
        }
        path = self.data_dir / config.STATS_FILE
        fsutil.write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        return stats

    # -- statistiques --------------------------------------------------------

    def stats(self) -> dict[str, int]:
        total = sum(len(e) + e.is_page for e in self.dirs.values())
        dead = sum(sum(e.slugs.values()) + bool(e.page_dead) for e in self.dirs.values())
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

    def status(self, url: str) -> bool | None:
        """``True`` si l'URL est marquée morte, ``False`` si vivante, ``None``
        si elle n'est pas indexée.

        Consultation directe par le chemin, sans parcourir tout l'index : la
        vérification appelle ceci une fois par URL contrôlée.
        """
        try:
            relpath, leaf = pathmap.url_to_location(url)
        except pathmap.PathMappingError:
            return None
        entry = self.dirs.get(relpath)
        if entry is None:
            return None
        if leaf is None:
            return entry.page_dead
        return entry.slugs.get(leaf)

    def urls(self):
        """Itère sur toutes les URLs indexées, sous forme ``(url, morte)``."""
        for relpath, entry in sorted(self.dirs.items()):
            if entry.is_page:
                yield pathmap.location_to_url(relpath), bool(entry.page_dead)
            for slug in sorted(entry.slugs):
                yield pathmap.location_to_url(relpath, slug), entry.slugs[slug]


def _render(slug: str, entry: DirIndex) -> str:
    return f"{config.DEAD_SIGIL}{slug}" if entry.slugs[slug] else slug


def _render_self(entry: DirIndex) -> str:
    prefix = config.DEAD_SIGIL if entry.page_dead else ""
    return f"{prefix}{config.SELF_LINE}"


def _write_lines(path: Path, lines: list[str]) -> None:
    """Écrit en LF explicite : sous Windows le mode texte produirait du CRLF et
    ferait diverger le dépôt à chaque run."""
    fsutil.write_text(path, "\n".join(lines) + "\n" if lines else "")
