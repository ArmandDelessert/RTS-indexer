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

#: Flux RSS des rubriques éditoriales.
#:
#: Ces flux sont la seule fenêtre continue sur ce qui vient d'être publié. Le
#: crawl, lui, ne voit que la *première* page de chaque rubrique (``robots.txt``
#: interdit la pagination) : un article chassé de cette première page entre deux
#: exécutions ne serait jamais vu. D'où une source dédiée, très bon marché — une
#: vingtaine de requêtes — mais qu'il faut relancer assez souvent.
#:
#: Fenêtre mesurée : 25 items par flux, soit ~24 h pour le plus actif
#: (``info/toute-info``) et bien davantage pour les rubriques de niche. Une
#: exécution quotidienne est donc le strict minimum, sans marge.
#:
#: La liste est curée à dessein : sonder les 135'000 rubriques connues pour y
#: chercher un flux n'aurait aucun sens, et toutes n'en exposent pas (``culture``,
#: ``jeunesse`` ou les rubriques radio répondent 404). Celles-ci ont été vérifiées
#: une à une.
RSS_FEED_TEMPLATE = "https://www.rts.ch/{path}/?format=rss/news"
RSS_FEEDS: tuple[str, ...] = (
    "info",
    "info/toute-info",
    "info/suisse",
    "info/monde",
    "info/economie",
    "info/regions",
    "info/sciences-tech",
    "info/culture",
    "info/environnement",
    "info/sante",
    "info/societe",
    "sport",
    "sport/tout-le-sport",
    "sport/football",
    "sport/hockey",
    "sport/tennis",
    "sport/cyclisme",
    "sport/ski-alpin",
    "sport/basketball",
    "sport/athletisme",
    "meteo",
    "religion",
)

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
#: Fichier de curseur pour la rotation des graines (cf. crawl.select_seeds).
CRAWL_SEED_CURSOR_FILE = "crawl_seed_cursor.json"

# --- Contrôle de vivacité (verify) ------------------------------------------

#: Même débit modeste que le crawl : ce sont les mêmes serveurs.
VERIFY_MIN_INTERVAL = 0.5
#: Une URL déjà contrôlée n'est recontrôlée qu'au-delà de cet âge (jours).
#: L'index compte des dizaines de milliers d'URLs pour ~2 requêtes/s : tout
#: revérifier à chaque run prendrait des heures pour rien.
VERIFY_RECHECK_DAYS = 30
VERIFY_CHECKPOINT_URLS = 500
#: Cadence d'un point d'avancement léger (pas d'écriture disque, juste un log).
#: Un run de plusieurs dizaines de milliers d'URLs peut durer des heures sans
#: le moindre signe de vie sinon entre deux checkpoints.
VERIFY_PROGRESS_STEP = 100

#: Codes concluants. Tout le reste (403, 429, 5xx, timeout) est *non
#: concluant* : ni vivant ni mort, on ne touche pas au sigil et on ne met pas
#: le résultat en cache, pour recontrôler au prochain run. rts.ch renvoie par
#: exemple un 403 sur /360/paju/suissedescimes/, qui n'est pas une page morte.
VERIFY_DEAD_CODES = frozenset({404, 410})

#: Codes justifiant un second avis avant de condamner une URL.
#:
#: Mesuré sur 400 URLs déjà marquées mortes, re-contrôlées à froid : **aucun
#: faux positif**, et 70 % d'entre elles répondaient 410 — un signal explicite
#: et délibéré de suppression, qu'il serait vain de réinterroger. Seul le 404,
#: qui peut aussi traduire un incident passager côté serveur ou CDN, mérite
#: d'être confirmé. Repasser *tous* les codes morts par deux tours coûterait
#: ~38 % de requêtes en plus pour un problème mesuré sous 0,75 % ; se limiter
#: au 404 ramène ce surcoût à ~6 %.
VERIFY_RETRY_CODES = frozenset({404})
#: Nombre total de tentatives pour une URL suspecte (1 = pas de second avis).
VERIFY_ATTEMPTS = 2
#: Délai minimal avant de réessayer une URL suspecte (secondes).
#:
#: Un 404 fugace vient typiquement d'un cache négatif de CDN, dont les TTL
#: usuels vont de quelques dizaines de secondes à quelques minutes : réessayer
#: plus tôt retomberait sur la même réponse en cache, l'essai serait gaspillé.
#: L'attente n'immobilise aucun worker — l'URL est remise dans une file à
#: échéance et le parcours continue pendant ce temps.
VERIFY_RETRY_DELAY = 60.0

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
