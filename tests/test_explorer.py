"""Tests du générateur de page web.

Le rendu visuel se vérifie dans un navigateur ; ces tests portent sur ce qui
peut casser silencieusement : la structure de l'arbre, la restitution de la
casse, les totaux, et l'échappement du JSON embarqué.
"""

import json
import re

from rts_indexer import explorer
from rts_indexer.explorer import KEY_DIRS, KEY_FILES, KEY_PAGE, KEY_TOTAL, build_tree
from rts_indexer.store import Store

RUBRIQUE = "https://www.rts.ch/info/suisse/"
ARTICLE = "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
AUTRE = "https://www.rts.ch/info/suisse/2026/article/paleo-29313279.html"
CASSE = "https://www.rts.ch/sport/dossiers/2012/JO_2012/4195110-good-bye.html"


def _store(tmp_path, urls=(RUBRIQUE, ARTICLE, AUTRE)):
    store = Store(tmp_path)
    store.add_many(urls)
    return store


def test_structure_de_l_arbre(tmp_path):
    tree = build_tree(_store(tmp_path))
    suisse = tree[KEY_DIRS]["www.rts.ch"][KEY_DIRS]["info"][KEY_DIRS]["suisse"]

    assert suisse[KEY_PAGE] == 0  # rubrique vivante
    articles = suisse[KEY_DIRS]["2026"][KEY_DIRS]["article"]
    assert [nom for nom, _ in articles[KEY_FILES]] == [
        "la-suisse-29312521.html",
        "paleo-29313279.html",
    ]


def test_casse_restituee_pour_l_affichage(tmp_path):
    """Le disque stocke %4A%4F_2012 ; l'explorateur doit afficher JO_2012,
    sinon les liens reconstruits mèneraient à une 404."""
    tree = build_tree(_store(tmp_path, urls=[CASSE]))
    dossiers = tree[KEY_DIRS]["www.rts.ch"][KEY_DIRS]["sport"][KEY_DIRS]["dossiers"]
    assert "JO_2012" in dossiers[KEY_DIRS]["2012"][KEY_DIRS]


def test_url_morte_signalee(tmp_path):
    store = _store(tmp_path, urls=[RUBRIQUE, ARTICLE])
    store.add(ARTICLE, dead=True)
    store.add(RUBRIQUE, dead=True)
    tree = build_tree(store)

    suisse = tree[KEY_DIRS]["www.rts.ch"][KEY_DIRS]["info"][KEY_DIRS]["suisse"]
    assert suisse[KEY_PAGE] == 1
    articles = suisse[KEY_DIRS]["2026"][KEY_DIRS]["article"]
    assert articles[KEY_FILES] == [["la-suisse-29312521.html", 1]]


def test_totaux_cumules_par_sous_arbre(tmp_path):
    tree = build_tree(_store(tmp_path))
    racine = tree[KEY_DIRS]["www.rts.ch"]
    assert racine[KEY_TOTAL] == 3  # la rubrique + ses deux articles

    articles = racine[KEY_DIRS]["info"][KEY_DIRS]["suisse"][KEY_DIRS]["2026"][KEY_DIRS]["article"]
    assert articles[KEY_TOTAL] == 2


def test_chevron_echappe_dans_le_json_embarque(tmp_path):
    """Un `<` issu des données ne doit jamais atteindre le HTML tel quel : il
    pourrait refermer la balise `<script>` et casser la page.

    À noter : un `/` étant un séparateur de chemin, un `</script>` complet ne
    peut de toute façon pas tenir dans un seul segment d'URL — ici `x<` devient
    un dossier et `oups.html` la feuille. L'échappement reste la bonne défense,
    il ne dépend pas de cette subtilité.
    """
    store = Store(tmp_path)
    store.add("https://www.rts.ch/info/suisse/x<b>oups.html")

    html = explorer.render(store)
    brut = re.search(r'type="application/json">(.*?)</script>', html, re.DOTALL).group(1)

    assert "<" not in brut  # aucun chevron brut dans le bloc de données
    assert "\\u003c" in brut

    # Le JSON reste valide et la donnée intacte une fois relue.
    payload = json.loads(brut)
    suisse = payload["tree"][KEY_DIRS]["www.rts.ch"][KEY_DIRS]["info"][KEY_DIRS]["suisse"]
    assert suisse[KEY_FILES] == [["x<b>oups.html", 0]]


def test_page_generee_est_autonome(tmp_path):
    """Aucune ressource externe : la page doit s'ouvrir en file:// sans réseau."""
    html = explorer.render(_store(tmp_path))
    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\"" not in html
    assert "http://" not in html.replace("https://www.rts.ch", "")


def test_generate_ecrit_le_fichier(tmp_path):
    chemin = explorer.generate(_store(tmp_path), tmp_path / "site")
    assert chemin.name == "index.html"
    contenu = chemin.read_text(encoding="utf-8")
    assert contenu.startswith("<!doctype html>")
    assert b"\r" not in chemin.read_bytes()  # LF, comme le reste du dépôt


def test_statistiques_embarquees(tmp_path):
    store = _store(tmp_path)
    store.add(ARTICLE, dead=True)
    html = explorer.render(store)
    brut = re.search(r'type="application/json">(.*?)</script>', html, re.DOTALL).group(1)
    payload = json.loads(brut)

    assert payload["stats"]["urls"] == 3
    assert payload["stats"]["mortes"] == 1
    assert "generated_at" in payload
