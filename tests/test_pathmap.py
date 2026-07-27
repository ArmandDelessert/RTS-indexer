"""Tests du mapping URL <-> disque.

Les deux tests décisifs sont :func:`test_aucune_collision_apres_casefold` (le
filet qui rend acceptable la normalisation en minuscules sur NTFS) et
:func:`test_longueur_bornee` (le garde-fou MAX_PATH pour les clones Windows).
"""

import pytest

from rts_indexer import config, pathmap
from rts_indexer.pathmap import PathMappingError, location_to_url, url_to_location

CORPUS = [
    "https://www.rts.ch/",
    "https://www.rts.ch/info/",
    "https://www.rts.ch/info/suisse/",
    "https://www.rts.ch/info/culture/dossiers/2025/bis-bale/",
    "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html",
    "https://www.rts.ch/info/suisse/7422738-la-rts-participe.html",
    "https://www.rts.ch/audio-podcast/2010/emission/le-12h30-25000623.html",
    "https://www.rts.ch/articles/lien/decouvrir-l-application-rts-29290741.html",
    "https://www.rts.ch/info/vos-questions/2022/minute-par-minute/jurisprudence-27676776.html",
]


@pytest.mark.parametrize("url", CORPUS)
def test_aller_retour(url):
    relpath, leaf, case_changed = url_to_location(url)
    assert not case_changed
    assert location_to_url(relpath, leaf) == url


def test_decoupage_dossier_vs_feuille():
    relpath, leaf, _ = url_to_location(
        "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
    )
    assert relpath == "www.rts.ch/info/suisse/2026/article"
    assert leaf == "la-suisse-29312521.html"

    relpath, leaf, _ = url_to_location("https://www.rts.ch/info/suisse/")
    assert relpath == "www.rts.ch/info/suisse"
    assert leaf is None


def test_racine():
    relpath, leaf, _ = url_to_location("https://www.rts.ch/")
    assert (relpath, leaf) == ("www.rts.ch", None)
    assert location_to_url("www.rts.ch") == "https://www.rts.ch/"


def test_majuscule_signalee_et_normalisee():
    relpath, leaf, case_changed = url_to_location("https://www.rts.ch/Info/Suisse/")
    assert case_changed is True
    assert relpath == "www.rts.ch/info/suisse"


def test_casse_de_la_feuille_preservee():
    """La feuille est une ligne dans un fichier texte : aucune raison de la
    dégrader, et cela garde l'URL exactement reconstructible."""
    url = "https://www.rts.ch/info/suisse/Article-42.html"
    relpath, leaf, case_changed = url_to_location(url)
    assert leaf == "Article-42.html"
    assert case_changed is False
    assert location_to_url(relpath, leaf) == url


@pytest.mark.parametrize(
    "segment",
    ["con", "nul", "com1", "lpt9", "aux.html", "a%b", "fin.", "fin ", "a:b", "a|b"],
)
def test_segments_hostiles_reversibles(segment):
    safe, _ = pathmap.escape_segment(segment)
    assert not (set(safe) & pathmap._ILLEGAL_FS)
    assert safe.split(".")[0] not in pathmap._WIN_RESERVED
    assert safe[-1] not in ". "
    assert pathmap.unescape_segment(safe) == segment


def test_aucune_collision_apres_casefold():
    """Deux URLs distinctes ne doivent jamais viser le même chemin.

    C'est le test qui protège du bug silencieux sous Windows/OneDrive : NTFS
    étant insensible à la casse, une collision non détectée fusionnerait deux
    rubriques sans le moindre message d'erreur.
    """
    vus: dict[str, str] = {}
    for url in CORPUS:
        relpath, leaf, _ = url_to_location(url)
        cle = f"{relpath}/{leaf or ''}".casefold()
        assert cle not in vus, f"collision entre {vus.get(cle)} et {url}"
        vus[cle] = url


def test_longueur_bornee():
    for url in CORPUS:
        relpath, _, _ = url_to_location(url)
        projete = len(f"data/{relpath}/{config.INDEX_BASENAME}{config.INDEX_SUFFIX}")
        assert projete <= config.MAX_REL_PATH_LEN


def test_chemin_trop_long_rejete():
    profond = "https://www.rts.ch/" + "/".join("segment-tres-long" * 3 for _ in range(10)) + "/"
    with pytest.raises(PathMappingError, match="trop long"):
        url_to_location(profond)


@pytest.mark.parametrize(
    "slug, attendu",
    [("article.html", "a"), ("2026-truc.html", "2"), ("-tiret.html", "_"), ("Éveil.html", "_")],
)
def test_shard_key(slug, attendu):
    assert pathmap.shard_key(slug) == attendu
