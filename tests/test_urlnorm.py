"""Tests de normalisation, bâtis sur des URLs réellement observées sur rts.ch."""

import pytest

from rts_indexer.urlnorm import normalize, normalize_many

ARTICLE = "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
LEGACY = "https://www.rts.ch/info/suisse/7422738-la-rts-participe.html"
RUBRIQUE = "https://www.rts.ch/info/culture/dossiers/2025/bis-bale/"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Formes déjà canoniques : inchangées.
        (ARTICLE, ARTICLE),
        (LEGACY, LEGACY),
        (RUBRIQUE, RUBRIQUE),
        # http -> https, port par défaut retiré, hôte replié sur sa forme www.
        ("http://www.rts.ch/info/", "https://www.rts.ch/info/"),
        ("http://www.rts.ch:80/info/", "https://www.rts.ch/info/"),
        ("https://rts.ch/info/", "https://www.rts.ch/info/"),
        ("https://WWW.RTS.CH/info/", "https://www.rts.ch/info/"),
        # Slash final ajouté aux rubriques, fragment et query supprimés.
        ("https://www.rts.ch/info/suisse", "https://www.rts.ch/info/suisse/"),
        ("https://www.rts.ch/info/#top", "https://www.rts.ch/info/"),
        ("https://www.rts.ch/info/?page=3", "https://www.rts.ch/info/"),
        # Slashes doublés et segments relatifs résolus.
        ("https://www.rts.ch//info//suisse//", "https://www.rts.ch/info/suisse/"),
        ("https://www.rts.ch/info/culture/../suisse/", "https://www.rts.ch/info/suisse/"),
        # Encodage superflu d'un caractère non réservé défait.
        ("https://www.rts.ch/info/su%69sse/", "https://www.rts.ch/info/suisse/"),
        # Racine.
        ("https://www.rts.ch", "https://www.rts.ch/"),
    ],
)
def test_formes_canoniques(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Hors périmètre : sous-domaine non déclaré, domaine tiers.
        "https://img.rts.ch/articles/2026/image/2kaxbk-29202042.image",
        "https://www.srf.ch/news/",
        "https://avecvous.rts.ch/evenements/ateliers",
        # Non-HTML.
        "https://www.rts.ch/2012/02/20/09/34/3466944.image",
        "https://www.rts.ch/sitemaps/pages.xml",
        "https://www.rts.ch/default.webmanifest.json",
        "https://www.rts.ch/assets/app.js",
        # Artefacts de la CDX Wayback.
        'https://www.rts.ch/"http://www.rts.ch/2012/02/20/09/34/3466944.image?w=100"',
        "https://www.rts.ch/%22http://www.rts.ch/x.html",
        "https://www.rts.ch/info/ suisse/",
        # Boutons de partage social : le `&` non échappé après l'URL avale la
        # suite (`title=...`, `text=...`) et doit être rejeté sans ambiguïté,
        # rts.ch n'utilisant jamais `&` dans un chemin.
        "https://www.rts.ch/education/l-ecole/&amp;title=L%27%C3%A9cole/",
        "https://www.rts.ch/info/suisse/&text=un+article/",
        # Schémas non pertinents.
        "mailto:info@rts.ch",
        "javascript:void(0)",
        "#ancre",
        "",
    ],
)
def test_rejets(raw):
    assert normalize(raw) is None


def test_url_absurdement_longue_rejetee():
    """Incident réel : un candidat de plusieurs centaines de caractères (page
    Play au slug de phrase entière) a fini par faire planter tout un crawl
    faute d'être filtré avant d'atteindre le mapping disque. Rejeter tôt les
    candidats déraisonnablement longs coûte moins cher que de les traiter."""
    trop_long = "https://www.rts.ch/play/tv/x/" + ("mot-" * 600) + "/"
    assert normalize(trop_long) is None


def test_octet_utf8_percent_encode_decode_en_caractere_reel():
    """Incident réel : une URL Wayback contenait `%E2%80%A6` (l'ellipse « … »
    percent-encodée en UTF-8). Laissée telle quelle, la suite de triplets
    pourcent survivait dans le chemin canonique, puis pathmap.escape_segment
    échappait *chaque lettre hexadécimale majuscule* comme une vraie
    majuscule à préserver — un nom de dossier doublement échappé et
    illisible (%25%452%2580%25%416 sur disque). Décoder l'octet non-ASCII en
    son caractère réel évite ce doublon d'échappement à la racine."""
    url = "http://www.rts.ch/%E2%80%A6/mi%E2%80%A6/8964162-x.html"
    assert normalize(url) == "https://www.rts.ch/…/mi…/8964162-x.html"


def test_octet_ascii_reste_encode_meme_apres_percent_utf8():
    """Un octet ASCII percent-encodé (`%2F` pour `/`) est structurel : le
    décoder changerait le nombre de segments du chemin. Seuls les octets
    non-ASCII doivent être décodés en caractère réel."""
    assert normalize("https://www.rts.ch/info/x%2Fy.html") == (
        "https://www.rts.ch/info/x%2Fy.html"
    )


def test_sequence_utf8_invalide_laissee_intacte():
    """Une suite d'octets non-ASCII qui ne forme pas de l'UTF-8 valide ne doit
    pas faire planter la normalisation ; elle reste telle quelle par prudence."""
    assert normalize("https://www.rts.ch/info/%FF%FE-x.html") == (
        "https://www.rts.ch/info/%FF%FE-x.html"
    )


def test_resolution_relative_pendant_le_crawl():
    base = "https://www.rts.ch/info/suisse/"
    assert normalize("2026/article/x-1.html", base) == (
        "https://www.rts.ch/info/suisse/2026/article/x-1.html"
    )
    assert normalize("/info/culture/", base) == "https://www.rts.ch/info/culture/"
    assert normalize("//www.rts.ch/info/", base) == "https://www.rts.ch/info/"


def test_play_hors_liste_blanche_rejete():
    """RTS Play route son contenu sur un paramètre (?urn=/?id=) toujours
    retiré par normalize() ; sans liste blanche, ces URLs seraient indexées
    cassées (redirection vers /play/not-found)."""
    assert normalize("https://www.rts.ch/play/tv/emission/1-jour-1-question/") is None
    assert normalize("https://www.rts.ch/play/tv/rts-education/video/x") is None
    assert normalize("https://www.rts.ch/play/") is None
    assert normalize("https://www.rts.ch/play/radio/emission/cqfd/") is None


def test_play_avec_id_ou_urn_reste_rejete():
    """Incident réel évité : convertir ces URLs en https://www.rts.ch/a/<id>/
    semblait capturer du contenu réel, mais RTS redirige /a/<id> vers cette
    même URL paramétrée — normalize() la reconvertirait donc vers le même
    /a/<id>/, un doublon d'elle-même que resolve_doublons() supprimerait
    sans rien y substituer (cible == url). La query string ne change donc
    rien : ces URLs suivent la même règle que les autres (retirée, puis
    chemin nu jugé sur la liste blanche)."""
    assert normalize(
        "https://www.rts.ch/play/tv/rts-education/video/les-plantes-communiquent-elles"
        "?urn=urn:rts:video:2045231"
    ) is None
    assert normalize("https://www.rts.ch/play/tv/emission/b-r-i-c-o--club?id=4707419") is None


def test_play_liste_blanche_acceptee():
    assert normalize("https://www.rts.ch/play/tv") == "https://www.rts.ch/play/tv/"
    assert normalize("https://www.rts.ch/play/tv/aide") == "https://www.rts.ch/play/tv/aide/"
    assert normalize("https://www.rts.ch/play/recherche/") == "https://www.rts.ch/play/recherche/"


def test_normalize_many_deduplique_en_preservant_l_ordre():
    urls = normalize_many(
        [
            "https://www.rts.ch/info/suisse",
            "http://rts.ch/info/suisse/",  # même URL après normalisation
            "https://www.rts.ch/info/culture/",
            "https://img.rts.ch/x.image",  # rejetée
        ]
    )
    assert urls == ["https://www.rts.ch/info/suisse/", "https://www.rts.ch/info/culture/"]
