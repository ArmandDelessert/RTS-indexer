"""Configuration du moteur : ce qui ne dépend pas du site indexé.

Tout ce qui varie d'un site à l'autre — périmètre, sources, débit poli, seuils
de stockage — vit dans un profil (:mod:`.profiles`, ``profiles/*.toml``). La
frontière est une question et non un rangement : *ce réglage changerait-il si
l'on indexait lemonde.fr au lieu de rts.ch ?* Le format d'une ligne d'index,
non ; le délai entre deux requêtes, oui (heidi.news coupe à ~20 req/min).

Ce qui reste ici se range en trois familles : les emplacements sur disque, la
résilience réseau (retries, backoff, tolérances), et le format de stockage.
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

# --- Archives CDX (Wayback, Common Crawl) -----------------------------------
#
# Les points d'entrée des archives ne dépendent pas du site indexé : c'est le
# même service, interrogé avec un filtre d'hôte différent.

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"

#: Index des crawls Common Crawl. Chaque entrée du JSON est un crawl distinct
#: (« CC-MAIN-2026-05 »...) avec sa propre API CDX.
COMMONCRAWL_INDEXES = "https://index.commoncrawl.org/collinfo.json"

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

REQUEST_TIMEOUT = 30.0
MAX_CONCURRENCY = 4
#: Durée d'attente d'un worker sur une file vide avant de conclure que le
#: parcours est terminé.
CRAWL_IDLE_TIMEOUT = 2.0
#: Écriture de l'index sur disque tous les N pages visitées (0 pour désactiver).
#: Borne la perte en cas d'incident non rattrapable en Python (coupure de
#: courant, kill -9) à ce nombre de pages plutôt qu'à la totalité du run.
CRAWL_CHECKPOINT_PAGES = 200
#: Fichier de curseur pour la rotation des graines (cf. crawl.select_seeds).
CRAWL_SEED_CURSOR_FILE = "crawl_seed_cursor.json"
#: Nombre d'essais avant de faire confiance à un code de VERIFY_RETRY_CODES
#: (404 : peut être transitoire). Même principe que le second avis de
#: Verifier, mais sans sa file différée (le worker abandonnerait la file
#: avant l'échéance) : le second essai a lieu directement dans _visit(),
#: immobilisant un seul worker plutôt que d'attendre en arrière-plan — d'où
#: un délai bien plus court que VERIFY_RETRY_DELAY.
CRAWL_ATTEMPTS = 2
CRAWL_RETRY_DELAY = 5.0

# --- Contrôle de vivacité (verify) ------------------------------------------

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

#: Ligne signalant que le dossier lui-même est une URL valide.
SELF_LINE = "./"
#: Préfixe marquant une URL confirmée morte (404/410).
DEAD_SIGIL = "!"

ANOMALIES_FILE = "_anomalies.tsv"
STATS_FILE = "_stats.json"
