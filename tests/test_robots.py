"""Tests de robots.py, calés sur le robots.txt réel de rts.ch."""

import pytest

from rts_indexer.robots import parse

# Extrait fidèle de https://www.rts.ch/robots.txt
RTS_ROBOTS = """
User-agent: AhrefsBot
Crawl-delay: 10

User-agent: *
Disallow: /a/
Disallow: /article/
Disallow: /medias/*/
Disallow: /medias/*.html
Disallow: /recherche/
Disallow: /*/recherche/
Disallow: /*/page/
Disallow: /*?*page=

Sitemap: https://www.rts.ch/sitemaps/pages.xml
"""


@pytest.fixture
def rules():
    return parse(RTS_ROBOTS)


@pytest.mark.parametrize(
    "path",
    [
        # L'article vit sous /info/..., pas sous /article/ : le motif est ancré
        # au début du chemin et ne s'applique donc pas.
        "/info/suisse/2026/article/la-suisse-29312521.html",
        "/info/suisse/",
        "/",
        "/archives/cantons/",
        "/medias/",  # /medias/*/ exige un segment intermédiaire
    ],
)
def test_autorise(rules, path):
    assert rules.allowed(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/a/quelque-chose",
        "/article/29312521",
        "/recherche/",
        "/info/recherche/",  # via /*/recherche/
        "/info/suisse/page/2",  # via /*/page/
        "/medias/2026/emission/x/",  # via /medias/*/
        "/medias/2026-truc.html",  # via /medias/*.html
    ],
)
def test_interdit(rules, path):
    assert rules.allowed(path) is False


def test_le_groupe_etoile_est_retenu(rules):
    """Notre agent n'a pas de groupe dédié : le Crawl-delay d'AhrefsBot ne nous
    concerne pas."""
    assert rules.crawl_delay is None
    assert len(rules.disallow) == 8


def test_groupe_specifique_prioritaire():
    rules = parse(
        "User-agent: *\nDisallow: /\n\nUser-agent: RTS-indexer\nDisallow: /prive/\n"
    )
    assert rules.allowed("/info/") is True
    assert rules.allowed("/prive/x") is False


def test_le_motif_le_plus_long_gagne():
    rules = parse("User-agent: *\nDisallow: /info/\nAllow: /info/suisse/\n")
    assert rules.allowed("/info/culture/") is False
    assert rules.allowed("/info/suisse/x.html") is True


def test_allow_gagne_a_longueur_egale():
    rules = parse("User-agent: *\nDisallow: /info/\nAllow: /info/\n")
    assert rules.allowed("/info/x") is True


def test_ancre_de_fin():
    rules = parse("User-agent: *\nDisallow: /*.pdf$\n")
    assert rules.allowed("/doc/rapport.pdf") is False
    assert rules.allowed("/doc/rapport.pdf.html") is True


def test_agents_multiples_partagent_un_groupe():
    rules = parse("User-agent: Googlebot\nUser-agent: *\nDisallow: /prive/\n")
    assert rules.allowed("/prive/x") is False


def test_commentaires_et_lignes_vides_ignores():
    rules = parse("# commentaire\nUser-agent: *  # nous\nDisallow: /prive/ # secret\n\n")
    assert rules.allowed("/prive/x") is False
    assert rules.allowed("/public/x") is True


def test_robots_vide_autorise_tout():
    assert parse("").allowed("/n-importe-quoi") is True


def test_allowed_url():
    rules = parse(RTS_ROBOTS)
    assert rules.allowed_url("https://www.rts.ch/info/suisse/") is True
    assert rules.allowed_url("https://www.rts.ch/recherche/") is False
