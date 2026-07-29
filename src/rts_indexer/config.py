"""Configuration statique de l'indexeur.

Tout ce qui est susceptible d'être ajusté sans toucher à la logique vit ici.
"""

from __future__ import annotations

from pathlib import Path

# --- Emplacements -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / ".cache"
#: Page web générée. Artefact dérivé, non versionné (cf. .gitignore) : un bloc
#: JSON réécrit intégralement à chaque run n'a pas sa place dans l'historique.
SITE_DIR = REPO_ROOT / "site"

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

#: Index des crawls Common Crawl. Chaque entrée du JSON est un crawl distinct
#: (« CC-MAIN-2026-05 »...) avec sa propre API CDX.
COMMONCRAWL_INDEXES = "https://index.commoncrawl.org/collinfo.json"

# --- Archives CDX (Wayback, Common Crawl) -----------------------------------

#: Ces requêtes prennent une dizaine de secondes : timeout large.
CDX_TIMEOUT = 180.0
#: Pause entre deux pages. Ces archives sont des services gratuits et
#: mutualisés : on ne les interroge pas en rafale, et jamais en parallèle.
CDX_DELAY = 1.0
CDX_ATTEMPTS = 4
#: Base du délai de reprise, doublé à chaque essai. Wayback et Common Crawl
#: limitent le débit de façon soutenue, pas ponctuelle.
CDX_BACKOFF = 5.0
#: Nombre de pages vides consécutives avant de conclure qu'une tranche est
#: épuisée. Une seule ne suffit pas : les filtres (mimetype, statuscode) sont
#: appliqués *après* le découpage en blocs, si bien qu'une page intermédiaire
#: peut ne rien retourner alors que les suivantes ont des données. S'arrêter à
#: la première tronquerait silencieusement l'archive.
CDX_EMPTY_TOLERANCE = 3

# --- Réseau -----------------------------------------------------------------

USER_AGENT = (
    "RTS-indexer/0.1 "
    "(+https://github.com/ArmandDelessert/RTS-indexer)"
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
#: Écriture de l'index sur disque tous les N pages visitées (0 pour désactiver).
#: Borne la perte en cas d'incident non rattrapable en Python (coupure de
#: courant, kill -9) à ce nombre de pages plutôt qu'à la totalité du run.
CRAWL_CHECKPOINT_PAGES = 200

# --- Contrôle de vivacité (verify) ------------------------------------------

#: Même débit modeste que le crawl : ce sont les mêmes serveurs.
VERIFY_MIN_INTERVAL = 0.5
#: Une URL déjà contrôlée n'est recontrôlée qu'au-delà de cet âge (jours).
#: L'index compte des dizaines de milliers d'URLs pour ~2 requêtes/s : tout
#: revérifier à chaque run prendrait des heures pour rien.
VERIFY_RECHECK_DAYS = 30
VERIFY_CHECKPOINT_URLS = 500

#: Codes concluants. Tout le reste (403, 429, 5xx, timeout) est *non
#: concluant* : ni vivant ni mort, on ne touche pas au sigil et on ne met pas
#: le résultat en cache, pour recontrôler au prochain run. rts.ch renvoie par
#: exemple un 403 sur /360/paju/suissedescimes/, qui n'est pas une page morte.
VERIFY_DEAD_CODES = frozenset({404, 410})

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
