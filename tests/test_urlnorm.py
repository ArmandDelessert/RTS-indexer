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
        # Schémas non pertinents.
        "mailto:info@rts.ch",
        "javascript:void(0)",
        "#ancre",
        "",
    ],
)
def test_rejets(raw):
    assert normalize(raw) is None


def test_resolution_relative_pendant_le_crawl():
    base = "https://www.rts.ch/info/suisse/"
    assert normalize("2026/article/x-1.html", base) == (
        "https://www.rts.ch/info/suisse/2026/article/x-1.html"
    )
    assert normalize("/info/culture/", base) == "https://www.rts.ch/info/culture/"
    assert normalize("//www.rts.ch/info/", base) == "https://www.rts.ch/info/"


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
