"""Profil de site : tout ce qui change d'un site indexé à l'autre.

La frontière avec :mod:`.config` est une question, pas une commodité de
rangement : *est-ce que ce réglage dépend du site indexé ?* Le débit poli
en dépend (heidi.news coupe à ~20 requêtes/minute là où letemps.ch encaisse
1 req/s sans broncher), le seuil de sharding aussi (rts.ch répartit ses URLs
sur des milliers de rubriques, letemps.ch en concentre 22 % sous ``/suisse``).
Le format d'une ligne d'index, lui, n'en dépend pas : il reste dans
:mod:`.config`.

Un profil est un fichier TOML sous ``profiles/``. Le passage par un fichier
plutôt que par un module Python est délibéré : à terme le code et les données
vivent dans des dépôts séparés, et c'est le dépôt de données qui porte son
propre profil — il ne peut donc pas être du code importable depuis le moteur.

**Profil actif.** Une exécution indexe un site et un seul : le profil est posé
une fois au démarrage par :func:`activate`, et les modules le lisent via
:func:`active`. C'est un état global, assumé comme tel — le faire circuler en
paramètre à travers les quatorze modules qui en dépendent coûterait un
remaniement massif pour un besoin qui n'existe pas. Les tests, eux, ont besoin
d'en changer : :func:`use` le fait proprement et restaure l'ancien à la sortie.
"""

from __future__ import annotations

import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import config

#: Emplacement par défaut des profils livrés avec le moteur.
PROFILES_DIR = config.REPO_ROOT / "profiles"


class ProfileError(ValueError):
    """Profil absent, illisible ou incomplet."""


@dataclass(frozen=True)
class DenyRule:
    """Une zone du site exclue du périmètre, avec ses exceptions.

    Généralise la liste blanche ``/play/`` de rts.ch : une branche entière est
    écartée parce que son contenu n'est pas routé par le chemin (RTS Play
    passe par ``?urn=``, toujours retiré à la normalisation — indexer ces URLs
    revenait à indexer des redirections vers ``/play/not-found``), sauf une
    poignée de pages statiques qui, elles, fonctionnent.
    """

    prefix: str
    allow: frozenset[str] = frozenset()
    reason: str = ""

    def blocks(self, path: str) -> bool:
        """``path`` est-il exclu ? ``path`` est le chemin sans slash bordant."""
        if path != self.prefix and not path.startswith(f"{self.prefix}/"):
            return False
        return path not in self.allow


@dataclass(frozen=True)
class Profile:
    """Configuration d'un site indexé."""

    name: str

    # -- périmètre -----------------------------------------------------------
    #: Hôtes indexés. Chacun devient un dossier racine sous ``data/``.
    hosts: tuple[str, ...]
    #: Hôtes repliés sur leur forme canonique avant filtrage.
    host_aliases: dict[str, str] = field(default_factory=dict)
    #: Extensions acceptées sur le segment terminal. Ce n'est **pas** ce qui
    #: décide du stockage (cf. :mod:`.layout`) mais ce qui écarte du périmètre
    #: les ``.jpg``, ``.json`` et autres ``.image`` que les archives CDX
    #: ramènent en quantité.
    html_extensions: frozenset[str] = frozenset({".html", ".htm"})
    deny: tuple[DenyRule, ...] = ()

    # -- sources -------------------------------------------------------------
    sitemaps: tuple[str, ...] = ()
    rss_feed_template: str = ""
    rss_feeds: tuple[str, ...] = ()

    # -- réseau --------------------------------------------------------------
    user_agent: str = "url-indexer/0.1"
    #: Délai entre deux requêtes séquentielles (secondes).
    request_delay: float = 1.0
    #: Intervalle minimal entre deux départs de requête du crawler, tous
    #: workers confondus.
    crawl_min_interval: float = 0.5
    verify_min_interval: float = 0.5

    #: Forme canonique d'une URL de rubrique. ``by_extension`` ajoute un slash
    #: final quand le segment terminal n'a pas de point (rts.ch, lemonde.fr) ;
    #: ``never`` ne l'ajoute jamais (letemps.ch et heidi.news : leurs
    #: canonicals sont sans slash, même si le serveur tolère les deux formes).
    #: Ce n'est plus qu'une question de *mise en forme* de l'URL : depuis que
    #: le stockage se décide sur les descendants, le slash ne décide plus si
    #: l'URL devient un dossier ou une ligne.
    trailing_slash: str = "by_extension"

    # -- stockage ------------------------------------------------------------
    #: Au-delà de ce nombre de slugs, le fichier d'un dossier est éclaté en
    #: shards ``_index.<premier caractère>.txt``.
    shard_threshold: int = 5_000
    #: Garde-fou MAX_PATH, pour les clones Windows sans ``core.longpaths``.
    max_rel_path_len: int = 240
    #: Plafond de profondeur des dossiers ; 0 = pas de plafond. Au-delà, les
    #: segments restants passent dans la ligne d'index. Sans lui, un site qui
    #: partitionne ses URLs par date (lemonde.fr : ``/rubrique/article/AAAA/MM/JJ/``)
    #: produit un dossier par jour et par rubrique — ~279'000 dossiers pour
    #: ~3,5 M d'URLs, contre 5'148 pour les 459'011 de rts.ch.
    max_dir_depth: int = 0

    def excluded(self, path: str) -> bool:
        """``path`` tombe-t-il dans une zone exclue ?"""
        return any(rule.blocks(path) for rule in self.deny)

    def ends_with_slash(self, segments: tuple[str, ...]) -> bool:
        """L'URL formée de ces segments se termine-t-elle par un slash ?"""
        if not segments:
            return True  # la racine de l'hôte
        if self.trailing_slash == "never":
            return False
        return "." not in segments[-1]


def _as_deny(raw: object, source: Path) -> tuple[DenyRule, ...]:
    if not isinstance(raw, list):
        raise ProfileError(f"{source}: 'perimetre.deny' doit être une liste de tables")
    rules = []
    for item in raw:
        try:
            rules.append(
                DenyRule(
                    prefix=item["prefix"].strip("/"),
                    allow=frozenset(s.strip("/") for s in item.get("allow", ())),
                    reason=item.get("raison", ""),
                )
            )
        except (TypeError, KeyError) as exc:
            raise ProfileError(f"{source}: règle deny invalide ({exc})") from exc
    return tuple(rules)


def load(spec: str | Path) -> Profile:
    """Charge un profil, par nom (``"rts"``) ou par chemin vers un ``.toml``."""
    path = Path(spec)
    if path.suffix != ".toml":
        path = PROFILES_DIR / f"{spec}.toml"
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ProfileError(f"profil introuvable: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path}: TOML illisible ({exc})") from exc

    perimetre = raw.get("perimetre", {})
    sources = raw.get("sources", {})
    reseau = raw.get("reseau", {})
    stockage = raw.get("stockage", {})

    hosts = perimetre.get("hosts")
    if not hosts:
        raise ProfileError(f"{path}: 'perimetre.hosts' est obligatoire et non vide")

    defaults = Profile(name="", hosts=())
    return Profile(
        name=raw.get("name") or path.stem,
        hosts=tuple(hosts),
        host_aliases=dict(perimetre.get("host_aliases", {})),
        html_extensions=frozenset(
            perimetre.get("html_extensions", defaults.html_extensions)
        ),
        deny=_as_deny(perimetre.get("deny", []), path),
        sitemaps=tuple(sources.get("sitemaps", ())),
        rss_feed_template=sources.get("rss_feed_template", ""),
        rss_feeds=tuple(sources.get("rss_feeds", ())),
        user_agent=reseau.get("user_agent", defaults.user_agent),
        request_delay=float(reseau.get("request_delay", defaults.request_delay)),
        crawl_min_interval=float(
            reseau.get("crawl_min_interval", defaults.crawl_min_interval)
        ),
        verify_min_interval=float(
            reseau.get("verify_min_interval", defaults.verify_min_interval)
        ),
        trailing_slash=reseau.get("trailing_slash")
        or perimetre.get("trailing_slash", defaults.trailing_slash),
        shard_threshold=int(stockage.get("shard_threshold", defaults.shard_threshold)),
        max_rel_path_len=int(
            stockage.get("max_rel_path_len", defaults.max_rel_path_len)
        ),
        max_dir_depth=int(stockage.get("max_dir_depth", defaults.max_dir_depth)),
    )


_active: Profile | None = None


def active() -> Profile:
    """Profil de l'exécution en cours.

    Charge ``DEFAULT_PROFILE`` au premier appel si rien n'a été activé : un
    test ou un script qui n'a pas de raison de s'en soucier n'a pas à le
    faire, et le comportement reste celui du site historique.
    """
    global _active
    if _active is None:
        _active = load(DEFAULT_PROFILE)
    return _active


def activate(profile: Profile | str | Path) -> Profile:
    """Pose le profil de l'exécution. Appelé une fois, au démarrage du CLI."""
    global _active
    _active = profile if isinstance(profile, Profile) else load(profile)
    return _active


@contextmanager
def use(profile: Profile | str | Path):
    """Active un profil le temps d'un bloc, puis restaure le précédent."""
    global _active
    previous = _active
    try:
        yield activate(profile)
    finally:
        _active = previous


#: Profil utilisé quand rien n'est précisé. Le dépôt n'indexe qu'un site à ce
#: stade ; il disparaîtra quand chaque dépôt de données portera le sien.
DEFAULT_PROFILE = "rts"
