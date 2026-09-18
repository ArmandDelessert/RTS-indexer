"""Tests du mapping URL <-> disque.

Le test décisif est :func:`test_majuscule_significative_preservee` : rts.ch a
des rubriques historiques sensibles à la casse (``JO_2012`` répond,
``jo_2012`` renvoie une 404 — constaté sur le site réel), donc la casse doit
être préservée sans perte, pas mise en minuscule. :func:`test_aucune_collision_apres_casefold`
vérifie que cette préservation n'introduit pas de collision NTFS malgré tout.
"""

import pytest

from rts_indexer import config, pathmap, profiles
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
    # Rubrique réelle sensible à la casse (JO_2012 répond, jo_2012 est en 404).
    "https://www.rts.ch/sport/dossiers/2012/JO_2012/4195110-good-bye-london.html",
]


def decoupe(url: str, dir_depth: int | None = None):
    """``url_to_location`` à la profondeur naturelle : le dossier est tout le
    chemin sauf la feuille.

    C'est ce que ``Store`` calcule pour une URL sans descendant. Les tests de ce
    module portent sur la projection elle-même, pas sur le choix de la coupure —
    celui-ci dépend de l'ensemble indexé et se teste dans ``test_store``.
    """
    _, segments, trailing = pathmap.url_parts(url)
    if dir_depth is None:
        dir_depth = len(segments) if trailing else len(segments) - 1
    return url_to_location(url, dir_depth)


@pytest.mark.parametrize("url", CORPUS)
def test_aller_retour(url):
    relpath, leaf = decoupe(url)
    assert location_to_url(relpath, leaf) == url


def test_feuille_multi_segments():
    """Une feuille peut porter plusieurs segments : c'est ce qui permet de
    plafonner la profondeur des dossiers sur un site qui partitionne par date.
    L'URL doit rester exactement reconstructible."""
    url = "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
    relpath, leaf = url_to_location(url, 3)
    assert relpath == "www.rts.ch/info/suisse/2026"
    assert leaf == "article/la-suisse-29312521.html"
    assert location_to_url(relpath, leaf) == url


def test_feuille_de_rubrique_garde_son_slash():
    """Une rubrique rangée en ligne (faute de descendant) porte son slash final
    dans la ligne, sans quoi l'URL reconstruite ne serait pas la bonne."""
    url = "https://www.rts.ch/info/suisse/"
    relpath, leaf = url_to_location(url, 1)
    assert (relpath, leaf) == ("www.rts.ch/info", "suisse/")
    assert location_to_url(relpath, leaf) == url


def test_decoupage_dossier_vs_feuille():
    relpath, leaf = decoupe(
        "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
    )
    assert relpath == "www.rts.ch/info/suisse/2026/article"
    assert leaf == "la-suisse-29312521.html"

    relpath, leaf = decoupe("https://www.rts.ch/info/suisse/")
    assert relpath == "www.rts.ch/info/suisse"
    assert leaf is None


def test_racine():
    relpath, leaf = decoupe("https://www.rts.ch/")
    assert (relpath, leaf) == ("www.rts.ch", None)
    assert location_to_url("www.rts.ch") == "https://www.rts.ch/"


def test_majuscule_significative_preservee():
    """Incident réel : mettre en minuscule une majuscule de rubrique casse la
    reconstruction d'une URL par ailleurs fonctionnelle."""
    url = "https://www.rts.ch/sport/dossiers/2012/JO_2012/"
    relpath, leaf = decoupe(url)
    assert "jo_2012" not in relpath  # la casse d'origine doit survivre, encodée
    assert location_to_url(relpath, leaf) == url


def test_deux_variantes_de_casse_ne_collisionnent_pas_sur_le_chemin():
    """https://www.rts.ch/360/Paju/SuisseDesCimes/ vs .../paju/suissedescimes/ —
    deux URLs distinctes rencontrées en conditions réelles. Avant, elles
    fusionnaient sur le même chemin (d'où la collision journalisée) ; la casse
    étant préservée, elles ne doivent plus jamais se confondre."""
    a, _ = decoupe("https://www.rts.ch/360/Paju/SuisseDesCimes/")
    b, _ = decoupe("https://www.rts.ch/360/paju/suissedescimes/")
    assert a != b


def test_casse_de_la_feuille_preservee():
    """La feuille est une ligne dans un fichier texte : aucune raison de la
    dégrader, et cela garde l'URL exactement reconstructible."""
    url = "https://www.rts.ch/info/suisse/Article-42.html"
    relpath, leaf = decoupe(url)
    assert leaf == "Article-42.html"
    assert location_to_url(relpath, leaf) == url


@pytest.mark.parametrize(
    "segment",
    [
        "con", "nul", "com1", "lpt9", "aux.html", "a%b", "fin.", "fin ", "a:b", "a|b",
        # Casse : les variantes de noms réservés ne doivent plus littéralement
        # correspondre après échappement (les majuscules sont encodées).
        "CON", "Con", "JO_2012",
    ],
)
def test_segments_hostiles_reversibles(segment):
    safe = pathmap.escape_segment(segment)
    assert not (set(safe) & pathmap._ILLEGAL_FS)
    assert safe.split(".")[0] not in pathmap._WIN_RESERVED
    assert safe[-1] not in ". "
    assert pathmap.unescape_segment(safe) == segment


def test_aucune_collision_apres_casefold():
    """Deux URLs distinctes ne doivent jamais viser le même chemin.

    C'est le test qui protège du bug silencieux sous Windows/OneDrive : NTFS
    étant insensible à la casse, une collision non détectée fusionnerait deux
    rubriques sans le moindre message d'erreur. La préservation de la casse
    (percent-encodée) doit suffire à elle seule à garantir cette propriété.
    """
    vus: dict[str, str] = {}
    for url in CORPUS:
        relpath, leaf = decoupe(url)
        cle = f"{relpath}/{leaf or ''}".casefold()
        assert cle not in vus, f"collision entre {vus.get(cle)} et {url}"
        vus[cle] = url


def test_longueur_bornee():
    for url in CORPUS:
        relpath, _ = decoupe(url)
        projete = len(f"data/{relpath}/{config.INDEX_BASENAME}{config.INDEX_SUFFIX}")
        assert projete <= profiles.active().max_rel_path_len


def test_chemin_trop_long_rejete():
    profond = "https://www.rts.ch/" + "/".join("segment-tres-long" * 3 for _ in range(10)) + "/"
    with pytest.raises(PathMappingError, match="trop long"):
        decoupe(profond)


@pytest.mark.parametrize(
    "slug, attendu",
    [("article.html", "a"), ("2026-truc.html", "2"), ("-tiret.html", "_"), ("Éveil.html", "_")],
)
def test_shard_key(slug, attendu):
    assert pathmap.shard_key(slug) == attendu
