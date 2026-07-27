"""Configuration statique de l'indexeur.

Tout ce qui est susceptible d'être ajusté sans toucher à la logique vit ici.
"""

from __future__ import annotations

from pathlib import Path

# --- Emplacements -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / ".cache"

# --- Périmètre --------------------------------------------------------------

#: Hôtes indexés. Ajouter un sous-domaine ici suffit à l'inclure : il devient un
#: dossier racine sous ``data/``.
HOSTS: tuple[str, ...] = ("www.rts.ch",)

#: Hôtes repliés sur leur forme canonique avant filtrage.
HOST_ALIASES: dict[str, str] = {"rts.ch": "www.rts.ch"}

#: Seules ces extensions de fichier sont retenues sur le segment terminal.
#: Un segment terminal sans point est traité comme un dossier (URL de rubrique).
HTML_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm"})

# --- Sources ----------------------------------------------------------------

#: Sitemaps déclarés dans https://www.rts.ch/robots.txt
SITEMAP_INDEXES: tuple[str, ...] = ("https://www.rts.ch/sitemaps/pages.xml",)
SITEMAP_EXTRA: tuple[str, ...] = ("https://www.rts.ch/sport/sitemap-live-sport.xml",)

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"

# --- Réseau -----------------------------------------------------------------

USER_AGENT = (
    "RTS-URL-indexer/0.1 "
    "(+https://github.com/ArmandDelessert/RTS-URL-indexer)"
)
REQUEST_TIMEOUT = 30.0
#: Délai minimal entre deux requêtes séquentielles (secondes).
REQUEST_DELAY = 1.0
MAX_CONCURRENCY = 4
#: Intervalle minimal entre deux départs de requête du crawler, tous workers
#: confondus. robots.txt ne fixe pas de Crawl-delay pour ``*`` ; 0.5 s (2 req/s)
#: est un rythme délibérément modeste pour un site de cette taille.
CRAWL_MIN_INTERVAL = 0.5
#: Durée d'attente d'un worker sur une file vide avant de conclure que le
#: parcours est terminé.
CRAWL_IDLE_TIMEOUT = 2.0

# --- Format de stockage -----------------------------------------------------

INDEX_BASENAME = "_index"
INDEX_SUFFIX = ".txt"
#: Au-delà de ce nombre de slugs, le fichier d'un dossier est éclaté en shards
#: ``_index.<premier caractère>.txt``.
SHARD_THRESHOLD = 5_000

#: Ligne signalant que le dossier lui-même est une URL valide.
SELF_LINE = "./"
#: Préfixe marquant une URL confirmée morte (404/410).
DEAD_SIGIL = "!"

#: Garde-fou MAX_PATH. Ne se déclenche jamais aux profondeurs observées sur
#: rts.ch (5 segments, 40 caractères max), mais protège les clones Windows qui
#: n'ont pas ``git config core.longpaths true``.
MAX_REL_PATH_LEN = 240

ANOMALIES_FILE = "_anomalies.tsv"
STATS_FILE = "_stats.json"
