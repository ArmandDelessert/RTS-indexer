"""Tests du stockage : format, déterminisme, sharding, détection de collision."""

import json
from pathlib import Path

from rts_indexer import config, fsutil
from rts_indexer.store import Store

RUBRIQUE = "https://www.rts.ch/info/suisse/"
ARTICLE = "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
AUTRE = "https://www.rts.ch/info/suisse/2026/article/paleo-festival-29313279.html"
DOSSIER = "www.rts.ch/info/suisse/2026/article"


def index_de(data_dir, relpath=DOSSIER, infix=""):
    path = data_dir / relpath / f"{config.INDEX_BASENAME}{infix}{config.INDEX_SUFFIX}"
    return path.read_text(encoding="utf-8")


def test_format_du_fichier(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(AUTRE)
    store.add(RUBRIQUE)
    store.write()

    # Rubrique : le dossier est lui-même une URL -> ligne `./`.
    assert index_de(tmp_path, "www.rts.ch/info/suisse") == "./\n"
    # Articles : triés, un slug par ligne, sans préfixe.
    assert index_de(tmp_path) == (
        "la-suisse-29312521.html\npaleo-festival-29313279.html\n"
    )


def test_lf_force_meme_sous_windows(tmp_path):
    """Un CRLF ferait diverger le dépôt à chaque run sur des milliers de lignes."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.write()
    brut = (tmp_path / DOSSIER / "_index.txt").read_bytes()
    assert b"\r" not in brut


def test_aller_retour_complet(tmp_path):
    urls = [RUBRIQUE, ARTICLE, AUTRE, "https://www.rts.ch/"]
    store = Store(tmp_path)
    store.add_many(urls)
    store.write()

    relu = Store(tmp_path).load()
    assert sorted(url for url, _ in relu.urls()) == sorted(urls)


def test_determinisme_du_second_run(tmp_path):
    """Réécrire sans nouveauté amont doit produire des octets identiques."""
    store = Store(tmp_path)
    store.add_many([RUBRIQUE, ARTICLE, AUTRE])
    store.write()
    avant = index_de(tmp_path)

    Store(tmp_path).load().write()
    assert index_de(tmp_path) == avant


def test_sigil_mort_ne_change_qu_une_ligne(tmp_path):
    """Le tri ignore le sigil : marquer une URL morte doit modifier la ligne sur
    place, pas la déplacer dans le fichier."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.write()
    avant = index_de(tmp_path).splitlines()

    store = Store(tmp_path).load()
    store.add(ARTICLE, dead=True)
    store.write()
    apres = index_de(tmp_path).splitlines()

    assert len(avant) == len(apres)
    differences = [i for i, (a, b) in enumerate(zip(avant, apres)) if a != b]
    assert differences == [0]
    assert apres[0] == f"{config.DEAD_SIGIL}la-suisse-29312521.html"

    # Et le statut survit à une relecture.
    assert dict(Store(tmp_path).load().urls())[ARTICLE] is True


def test_rubrique_peut_etre_marquee_morte(tmp_path):
    """Bug réel : `dead` était ignoré pour les URLs de rubrique, seuls les
    articles pouvaient être marqués morts. Or les rubriques mortes existent
    bel et bien (dossiers/2016/coeur-a-coeur/* renvoient 404)."""
    store = Store(tmp_path)
    store.add(RUBRIQUE, dead=True)
    store.write()

    assert index_de(tmp_path, "www.rts.ch/info/suisse") == f"{config.DEAD_SIGIL}./\n"

    relu = Store(tmp_path).load()
    assert relu.status(RUBRIQUE) is True
    assert dict(relu.urls())[RUBRIQUE] is True
    assert relu.stats()["mortes"] == 1


def test_rubrique_morte_peut_ressusciter(tmp_path):
    store = Store(tmp_path)
    store.add(RUBRIQUE, dead=True)
    store.write()

    store = Store(tmp_path).load()
    store.add(RUBRIQUE, dead=False)
    store.write()

    assert index_de(tmp_path, "www.rts.ch/info/suisse") == "./\n"
    assert Store(tmp_path).load().status(RUBRIQUE) is False


def test_status_distingue_inconnu_de_vivant(tmp_path):
    """`None` (pas indexée) et `False` (indexée et vivante) ne doivent pas se
    confondre — c'est cette confusion qui empêchait de marquer une rubrique."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    assert store.status(ARTICLE) is False
    assert store.status(RUBRIQUE) is None
    assert store.status("https://www.rts.ch/jamais-vue/") is None


def test_source_ne_ressuscite_pas_une_url_morte(tmp_path):
    """Une source qui réaffirme une URL ne doit pas écraser le verdict de verify."""
    store = Store(tmp_path)
    store.add(ARTICLE, dead=True)
    store.write()

    store = Store(tmp_path).load()
    store.add(ARTICLE)  # dead=None : statut inchangé
    store.write()
    assert dict(Store(tmp_path).load().urls())[ARTICLE] is True


def test_sharding_au_dela_du_seuil(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARD_THRESHOLD", 3)
    store = Store(tmp_path)
    store.add(RUBRIQUE)
    for lettre in "abcd":
        store.add(f"https://www.rts.ch/info/suisse/{lettre}-article.html")
    store.write()

    dossier = tmp_path / "www.rts.ch/info/suisse"
    shards = sorted(p.name for p in dossier.glob("_index*.txt"))
    assert shards == ["_index.a.txt", "_index.b.txt", "_index.c.txt", "_index.d.txt", "_index.txt"]
    # `./` reste dans _index.txt, qui fait office d'en-tête.
    assert (dossier / "_index.txt").read_text(encoding="utf-8") == "./\n"
    assert (dossier / "_index.a.txt").read_text(encoding="utf-8") == "a-article.html\n"

    # Le sharding est réversible : repasser sous le seuil regroupe les fichiers.
    # `force` est indispensable ici, et c'est bien pour cela que `build` l'utilise :
    # changer le seuil ne modifie rien *en mémoire*, donc ne salit aucun dossier.
    # Sans forcer, l'écriture sélective conclurait à juste titre qu'il n'y a rien
    # à refaire et conserverait l'ancien découpage.
    monkeypatch.setattr(config, "SHARD_THRESHOLD", 5_000)
    Store(tmp_path).load().write(force=True)
    assert sorted(p.name for p in dossier.glob("_index*.txt")) == ["_index.txt"]


def test_purge_des_dossiers_disparus(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/obsolete/x.html")
    store.write()
    assert (tmp_path / "www.rts.ch/obsolete").is_dir()

    store = Store(tmp_path)  # sans load() : l'index repart de zéro
    store.add(ARTICLE)
    store.write()
    assert not (tmp_path / "www.rts.ch/obsolete").exists()


def test_url_trop_longue_ignoree_sans_faire_echouer_le_run(tmp_path):
    """Incident réel : une page Play au slug de plusieurs centaines de
    caractères (``play/tv/19h30/video/<slug-phrase-entiere>/``) a fait planter
    un crawl de 350 pages, perdant tout le travail déjà accompli faute d'être
    rattrapée. add() doit désormais journaliser et continuer."""
    trop_longue = "https://www.rts.ch/play/tv/19h30/video/" + ("mot-" * 70) + "/"

    store = Store(tmp_path)
    store.add(RUBRIQUE)
    ajoutee = store.add(trop_longue)
    assert ajoutee is False

    stats = store.write()
    assert stats["urls"] == 1  # seule RUBRIQUE a survécu
    assert dict(Store(tmp_path).load().urls()) == {RUBRIQUE: False}

    lignes = (tmp_path / config.ANOMALIES_FILE).read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("trop_long\t" + trop_longue) for l in lignes)


def test_majuscule_n_est_plus_une_anomalie(tmp_path):
    """Incident réel : https://www.rts.ch/360/Paju/SuisseDesCimes/ et sa
    variante .../paju/suissedescimes/ fusionnaient sur le même dossier (la
    casse étant perdue), ce qui était journalisé comme collision. La casse
    étant désormais préservée sans perte, les deux doivent coexister — ce
    n'est plus une anomalie du tout."""
    store = Store(tmp_path)
    store.add("https://www.rts.ch/360/Paju/SuisseDesCimes/")
    store.add("https://www.rts.ch/360/paju/suissedescimes/")
    stats = store.write()

    assert stats["urls"] == 2  # les deux coexistent, aucune fusion
    assert stats["anomalies"] == 0
    assert not (tmp_path / config.ANOMALIES_FILE).exists()

    urls = dict(Store(tmp_path).load().urls())
    assert "https://www.rts.ch/360/Paju/SuisseDesCimes/" in urls
    assert "https://www.rts.ch/360/paju/suissedescimes/" in urls


def test_collision_forcee_est_journalisee_et_ignoree(tmp_path, monkeypatch):
    """La casse ne collisionne plus (cf. test précédent) : ce filet ne devrait
    donc plus jamais se déclencher pour une simple différence de casse en
    pratique. Il reste une protection utile si l'injectivité d'escape_segment
    était un jour cassée par erreur — on force artificiellement la collision
    pour vérifier qu'elle est journalisée et ignorée, jamais fatale."""
    from rts_indexer import pathmap

    # Les deux URLs sont forcées vers le même chemin de dossier : c'est la
    # seule façon de reproduire une collision maintenant que la casse seule
    # n'y suffit plus.
    monkeypatch.setattr(pathmap, "url_to_location", lambda url: ("www.rts.ch/force-collision", None))

    store = Store(tmp_path)
    store.add("https://www.rts.ch/premiere/")
    ajoutee = store.add("https://www.rts.ch/seconde/")
    assert ajoutee is False

    stats = store.write()
    assert stats["urls"] == 1
    lignes = (tmp_path / config.ANOMALIES_FILE).read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("collision\thttps://www.rts.ch/seconde/\t") for l in lignes)


def test_collision_detectee_meme_apres_rechargement(tmp_path, monkeypatch):
    """``_dir_source`` doit se reconstruire depuis le disque au chargement, la
    casse y étant désormais préservée — pas seulement pour les entrées déjà
    signalées par le passé, contrairement à l'ancien comportement basé sur les
    anomalies persistées."""
    from rts_indexer import pathmap

    store = Store(tmp_path)
    store.add("https://www.rts.ch/premiere/")
    store.write()

    original = pathmap.url_to_location
    monkeypatch.setattr(
        pathmap,
        "url_to_location",
        lambda url: ("www.rts.ch/premiere", None) if "seconde" in url else original(url),
    )
    relu = Store(tmp_path).load()
    assert relu.add("https://www.rts.ch/seconde/") is False


def test_anomalie_resolue_disparait_au_rechargement(tmp_path):
    """Incident réel : après le passage à la casse préservée, `_anomalies.tsv`
    continuait d'afficher une collision Paju/paju désormais résolue et une
    anomalie « majuscule » d'un type qui n'existe plus dans le code — le
    fichier était simplement recopié sans être revérifié. Une anomalie
    persistée doit maintenant être revalidée à chaque chargement."""
    path = tmp_path / config.ANOMALIES_FILE
    tmp_path.mkdir(exist_ok=True)
    path.write_text(
        "type\turl\tdetail\n"
        "majuscule\thttps://www.rts.ch/Foo/x.html\twww.rts.ch/foo\n"  # type retiré
        "collision\thttps://www.rts.ch/plus-de-collision/\tqui n'existe plus\n",
        encoding="utf-8",
    )

    relu = Store(tmp_path).load()
    assert relu.anomalies == set()


def test_anomalie_de_collision_toujours_valable_survit(tmp_path, monkeypatch):
    """À l'inverse, une collision réellement encore active doit être
    reconduite d'un run à l'autre, pas silencieusement oubliée."""
    from rts_indexer import pathmap

    store = Store(tmp_path)
    store.add("https://www.rts.ch/premiere/")
    store.write()

    monkeypatch.setattr(pathmap, "url_to_location", lambda url: ("www.rts.ch/premiere", None))
    store = Store(tmp_path).load()
    store.add("https://www.rts.ch/seconde/")
    store.write()

    relu = Store(tmp_path).load()
    assert ("collision", "https://www.rts.ch/seconde/", "https://www.rts.ch/premiere vs https://www.rts.ch/seconde") in relu.anomalies


def test_anomalie_code_atypique_survit_tant_que_l_url_reste_indexee(tmp_path):
    url = "https://www.rts.ch/technik/elektro.htm"
    store = Store(tmp_path)
    store.add(url)
    store.anomalies.add(("code_atypique", url, "HTTP 406"))
    store.write()

    relu = Store(tmp_path).load()
    assert ("code_atypique", url, "HTTP 406") in relu.anomalies

    relu.remove(url)
    relu.write()

    reencore = Store(tmp_path).load()
    assert reencore.anomalies == set()


def test_fichier_index_corrompu_n_empeche_pas_le_chargement(tmp_path):
    """Un fichier abîmé (écriture interrompue, encodage invalide) ne doit pas
    rendre tout le dépôt inexploitable."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(AUTRE)
    store.write()

    corrompu = tmp_path / DOSSIER / "_index.txt"
    corrompu.write_bytes(b"\xff\xfe\x00\xff invalide")

    relu = Store(tmp_path).load()
    assert dict(relu.urls()) == {}  # le dossier abîmé est ignoré, pas planté


def test_fichier_disparu_pendant_le_chargement_est_ignore(tmp_path, monkeypatch):
    """Incident réel : un autre run écrivant dans data/ a purgé un index entre
    son listage et sa lecture, faisant planter la commande `site`."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(RUBRIQUE)
    store.write()

    from rts_indexer import fsutil

    original = fsutil.read_text

    def read_qui_disparait(path):
        if DOSSIER.replace("/", "\\") in str(path) or DOSSIER in str(path).replace("\\", "/"):
            raise FileNotFoundError(str(path))
        return original(path)

    monkeypatch.setattr(fsutil, "read_text", read_qui_disparait)

    relu = Store(tmp_path).load()  # ne doit pas lever
    assert dict(relu.urls()) == {RUBRIQUE: False}


def test_anomalies_corrompues_n_empechent_pas_le_chargement(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.write()

    (tmp_path / config.ANOMALIES_FILE).write_bytes(b"\xff\xfe garbage")

    relu = Store(tmp_path).load()
    assert dict(relu.urls()) == {ARTICLE: False}


def test_stats(tmp_path):
    store = Store(tmp_path)
    store.add_many([RUBRIQUE, ARTICLE, AUTRE])
    store.add(ARTICLE, dead=True)
    stats = store.write()

    assert stats["urls"] == 3
    assert stats["mortes"] == 1
    assert stats["vivantes_ou_non_verifiees"] == 2

    payload = json.loads((tmp_path / config.STATS_FILE).read_text(encoding="utf-8"))
    assert payload["par_hote"] == {"www.rts.ch": 3}
    assert "generated_at" in payload


# -- suppression (doublons) -----------------------------------------------


def test_remove_retire_un_article(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    assert store.remove(ARTICLE) is True
    store.write()

    relu = Store(tmp_path).load()
    assert dict(relu.urls()) == {AUTRE: False}


def test_remove_retire_une_rubrique(tmp_path):
    store = Store(tmp_path)
    store.add(RUBRIQUE)
    assert store.remove(RUBRIQUE) is True
    store.write()

    relu = Store(tmp_path).load()
    assert relu.status(RUBRIQUE) is None


def test_remove_url_inconnue_ne_fait_rien(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    assert store.remove(AUTRE) is False  # jamais ajoutée
    assert store.remove("https://www.rts.ch/dossier-inconnu/x.html") is False
    assert dict(store.urls()) == {ARTICLE: False}


def test_remove_salit_le_dossier_pour_l_ecriture_selective(tmp_path):
    """remove() doit faire réécrire le dossier, sans quoi l'ancien contenu
    (avec l'URL supprimée) resterait sur disque."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.write()

    relu = Store(tmp_path).load()
    relu.remove(ARTICLE)
    relu.write()

    assert index_de(tmp_path) == "paleo-festival-29313279.html\n"


def test_remove_qui_vide_le_dossier_le_fait_purger(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.write()
    assert (tmp_path / DOSSIER).is_dir()

    relu = Store(tmp_path).load()
    relu.remove(ARTICLE)
    relu.write()
    assert not (tmp_path / DOSSIER).exists()


def test_resolve_doublons_supprime_si_la_cible_existe(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])  # AUTRE joue le rôle de la cible canonique
    store.anomalies.add(("doublon", ARTICLE, AUTRE))
    store.write()

    relu = Store(tmp_path).load()
    supprimes, ajoutees, ignores = relu.resolve_doublons()

    assert (supprimes, ajoutees, ignores) == (1, 0, 0)  # cible déjà indexée
    assert ("doublon", ARTICLE, AUTRE) not in relu.anomalies
    relu.write()
    assert dict(Store(tmp_path).load().urls()) == {AUTRE: False}


def test_resolve_doublons_indexe_la_cible_manquante_puis_supprime(tmp_path):
    """Le cas majoritaire en pratique (5'807 cas sur 5'856 lors du premier
    passage réel) : la cible est une vraie URL rts.ch, simplement jamais
    collectée. verify a déjà obtenu un 200 dessus — on peut lui faire
    confiance, une fois passée par le même filtre que les autres sources."""
    cible_jamais_collectee = "https://www.rts.ch/info/suisse/2026/article/nouvelle-3.html"
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("doublon", ARTICLE, cible_jamais_collectee))
    store.write()

    relu = Store(tmp_path).load()
    supprimes, ajoutees, ignores = relu.resolve_doublons()

    assert (supprimes, ajoutees, ignores) == (1, 1, 0)
    relu.write()
    assert dict(Store(tmp_path).load().urls()) == {cible_jamais_collectee: False}


def test_resolve_doublons_requalifie_si_la_cible_est_hors_perimetre(tmp_path):
    """Le critère de sécurité : ne jamais supprimer si la cible échoue au
    filtre de périmètre — un identifiant qui redirige vers une image sur
    img.rts.ch, par exemple, hors hôte connu et hors format HTML.

    Incident réel : ces cas restaient étiquetés `doublon` indéfiniment, alors
    que ce n'en sont pas — rien dans l'index ne fait double emploi avec eux,
    ils redirigent simplement hors périmètre. `resolve_doublons()` les
    requalifie donc sous un type distinct plutôt que de les laisser sous une
    étiquette trompeuse."""
    cible_hors_perimetre = "https://img.rts.ch/articles/2011/image/x-1.image"
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("doublon", ARTICLE, cible_hors_perimetre))
    store.write()

    relu = Store(tmp_path).load()
    supprimes, ajoutees, ignores = relu.resolve_doublons()

    assert (supprimes, ajoutees, ignores) == (0, 0, 1)
    assert ("doublon", ARTICLE, cible_hors_perimetre) not in relu.anomalies
    assert ("hors_perimetre", ARTICLE, cible_hors_perimetre) in relu.anomalies
    assert dict(relu.urls()) == {ARTICLE: False}  # rien supprimé, rien ajouté


def test_hors_perimetre_survit_au_rechargement(tmp_path):
    """Comme `doublon`, ce verdict vient du réseau et ne peut pas être rejoué
    hors ligne : il doit survivre à un load()/write(), pas disparaître comme
    un type d'anomalie retiré du code."""
    cible_hors_perimetre = "https://img.rts.ch/articles/2011/image/x-1.image"
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("hors_perimetre", ARTICLE, cible_hors_perimetre))
    store.write()

    relu = Store(tmp_path).load()
    assert ("hors_perimetre", ARTICLE, cible_hors_perimetre) in relu.anomalies


def test_hors_perimetre_disparait_si_l_url_source_est_retiree(tmp_path):
    cible_hors_perimetre = "https://img.rts.ch/articles/2011/image/x-1.image"
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("hors_perimetre", ARTICLE, cible_hors_perimetre))
    store.write()

    store2 = Store(tmp_path)  # repart de zéro : ARTICLE n'est jamais réaffirmée
    store2.write()

    relu = Store(tmp_path).load()
    assert relu.anomalies == set()


def test_resolve_doublons_ignore_les_anomalies_d_autres_types(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("trop_long", "https://www.rts.ch/x", "raison"))
    store.anomalies.add(("collision", "https://www.rts.ch/y", "raison"))

    supprimes, ajoutees, ignores = store.resolve_doublons()
    assert (supprimes, ajoutees, ignores) == (0, 0, 0)
    assert len(store.anomalies) == 2  # intactes


# -- purge des URLs mortes -------------------------------------------------


def test_purge_dead_supprime_les_articles_morts(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.add(ARTICLE, dead=True)

    assert store.purge_dead() == 1
    assert dict(store.urls()) == {AUTRE: False}


def test_purge_dead_supprime_les_rubriques_mortes(tmp_path):
    store = Store(tmp_path)
    store.add(RUBRIQUE, dead=True)
    store.add(ARTICLE)

    assert store.purge_dead() == 1
    assert dict(store.urls()) == {ARTICLE: False}


def test_purge_dead_laisse_les_urls_vivantes(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])

    assert store.purge_dead() == 0
    assert dict(store.urls()) == {ARTICLE: False, AUTRE: False}


def test_purge_dead_persiste_apres_ecriture_et_rechargement(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.add(ARTICLE, dead=True)
    store.write()

    relu = Store(tmp_path).load()
    supprimees = relu.purge_dead()
    relu.write()

    assert supprimees == 1
    assert dict(Store(tmp_path).load().urls()) == {AUTRE: False}
    assert index_de(tmp_path) == "paleo-festival-29313279.html\n"


# -- écriture sélective --------------------------------------------------
#
# Le risque de cette optimisation n'est pas la lenteur mais la perte : un
# dossier qu'on cesse de réécrire doit continuer d'être reconnu comme légitime
# par _prune(), sans quoi il serait supprimé. Ces tests couvrent d'abord ce
# cas-là.


#: Marque déposée dans un fichier d'index déjà écrit, pour observer s'il est
#: réécrit ou non. Plus fiable qu'une comparaison de `mtime` : sous Windows, la
#: granularité de l'horloge (~15 ms) rendrait le test instable, deux écritures
#: successives pouvant porter la même date.
TEMOIN = "# temoin\n"


def _poser_temoins(data_dir) -> dict:
    """Ajoute un témoin à chaque fichier d'index et retourne leur contenu."""
    marques = {}
    for path in data_dir.rglob("_index*.txt"):
        contenu = path.read_text(encoding="utf-8") + TEMOIN
        path.write_text(contenu, encoding="utf-8", newline="\n")
        marques[path] = contenu
    return marques


def _survivants(marques: dict) -> set:
    """Fichiers dont le témoin est intact : ils n'ont pas été réécrits."""
    return {p for p, contenu in marques.items() if p.read_text(encoding="utf-8") == contenu}


def test_dossier_inchange_n_est_pas_supprime(tmp_path):
    """Le scénario redouté : un dossier non réécrit doit survivre à _prune()."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    relu = Store(tmp_path).load()
    relu.add(AUTRE)  # ne touche que le dossier de ARTICLE
    relu.write()

    # Le dossier sport/, jamais touché par ce run, est toujours là et intact.
    assert index_de(tmp_path, "www.rts.ch/sport/hockey") == "match-1.html\n"
    assert sorted(url for url, _ in Store(tmp_path).load().urls()) == sorted(
        [ARTICLE, AUTRE, "https://www.rts.ch/sport/hockey/match-1.html"]
    )


def test_seuls_les_dossiers_modifies_sont_reecrits(tmp_path):
    """Le gain recherché : un dossier inchangé n'est pas rouvert du tout."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    relu = Store(tmp_path).load()
    marques = _poser_temoins(tmp_path)
    relu.add(AUTRE)
    relu.write()

    touche = tmp_path / DOSSIER / "_index.txt"
    intact = tmp_path / "www.rts.ch/sport/hockey/_index.txt"
    assert _survivants(marques) == {intact}, "seul le dossier modifié devait être réécrit"
    assert touche.read_text(encoding="utf-8") == (
        "la-suisse-29312521.html\npaleo-festival-29313279.html\n"
    )


def test_url_reaffirmee_ne_salit_pas_le_dossier(tmp_path):
    """Cas de très loin le plus fréquent : un crawl repasse sur des milliers de
    liens déjà connus. Réaffirmer ne doit rien réécrire."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.write()

    relu = Store(tmp_path).load()
    marques = _poser_temoins(tmp_path)
    relu.add_many([ARTICLE, AUTRE])  # rien de neuf
    relu.write()

    assert _survivants(marques) == set(marques), "aucun fichier ne devait être réécrit"


def test_changement_de_sigil_seul_declenche_la_reecriture(tmp_path):
    """`verify` ne change qu'un booléen, sans ajouter d'URL : le dossier doit
    tout de même être reconnu comme sale."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.write()

    relu = Store(tmp_path).load()
    relu.add(ARTICLE, dead=True)
    relu.write()

    assert index_de(tmp_path).startswith("!la-suisse-29312521.html\n")
    assert Store(tmp_path).load().status(ARTICLE) is True


def test_rubrique_marquee_morte_declenche_la_reecriture(tmp_path):
    """Même chose pour `page_dead`, qui vit hors de `slugs`."""
    store = Store(tmp_path)
    store.add(RUBRIQUE)
    store.write()

    relu = Store(tmp_path).load()
    relu.add(RUBRIQUE, dead=True)
    relu.write()

    assert index_de(tmp_path, "www.rts.ch/info/suisse") == "!./\n"


def test_ecriture_selective_reste_deterministe(tmp_path):
    """La garantie qui justifiait la réécriture complète doit tenir : un run
    sans nouveauté ne produit aucun changement d'octets."""
    store = Store(tmp_path)
    store.add_many([RUBRIQUE, ARTICLE, AUTRE])
    store.write()
    avant = {p: p.read_bytes() for p in tmp_path.rglob("_index*.txt")}

    Store(tmp_path).load().write()
    assert {p: p.read_bytes() for p in tmp_path.rglob("_index*.txt")} == avant


def test_checkpoints_successifs_n_ecrivent_que_leur_delta(tmp_path):
    """Un crawl appelle write() en boucle : chaque checkpoint ne doit réécrire
    que ce que la tranche a apporté, pas tout l'index."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    intact = tmp_path / "www.rts.ch/sport/hockey/_index.txt"
    marques = _poser_temoins(tmp_path)

    store.add(AUTRE)
    store.write()
    store.add("https://www.rts.ch/info/suisse/2026/article/troisieme-3.html")
    store.write()

    assert intact in _survivants(marques), "le dossier intact a été réécrit par un checkpoint"
    # Sur le store en mémoire : relire le disque compterait le témoin comme un
    # slug, l'instrument de mesure fausserait la mesure.
    assert len(list(store.urls())) == 4
    assert index_de(tmp_path) == (
        "la-suisse-29312521.html\npaleo-festival-29313279.html\ntroisieme-3.html\n"
    )


def test_purge_fonctionne_encore_avec_l_ecriture_selective(tmp_path):
    """_prune() doit toujours faire son travail sur ce qui a réellement
    disparu, sans se laisser abuser par les dossiers simplement inchangés."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/obsolete/x.html")
    store.write()

    store = Store(tmp_path)  # sans load() : l'index repart de zéro
    store.add(ARTICLE)
    store.write()

    assert not (tmp_path / "www.rts.ch/obsolete").exists()
    assert index_de(tmp_path) == "la-suisse-29312521.html\n"


# -- purge ciblée --------------------------------------------------------


def test_purge_ciblee_n_examine_pas_les_dossiers_inchanges(tmp_path):
    """Le gain : en régime normal, aucun balayage de l'arbre. Un fichier
    étranger déposé dans un dossier sain survit donc à une commande ordinaire."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    intrus = tmp_path / "www.rts.ch/sport/hockey/_index.zz.txt"
    intrus.write_text("intrus.html\n", encoding="utf-8")

    relu = Store(tmp_path).load()
    relu.add(AUTRE)
    relu.write()

    assert intrus.is_file(), "une commande ordinaire ne balaye plus tout l'arbre"


def test_build_rattrape_la_derive_externe(tmp_path):
    """La contrepartie : `build` (force) reste le filet qui nettoie tout."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    intrus = tmp_path / "www.rts.ch/sport/hockey/_index.zz.txt"
    intrus.write_text("intrus.html\n", encoding="utf-8")

    Store(tmp_path).load().write(force=True)
    assert not intrus.exists()


def test_fichier_illisible_est_purge_sans_balayage_complet(tmp_path):
    """Un fichier illisible au chargement n'a nourri ni `dirs` ni `_disk_files` :
    son contenu est perdu, il devient orphelin et doit être purgé — c'est la
    seule source d'orphelin que le code produise lui-même."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/sport/hockey/match-1.html")
    store.write()

    illisible = tmp_path / "www.rts.ch/sport/hockey/_index.txt"
    illisible.write_bytes(b"\xff\xfe invalide en utf-8")

    relu = Store(tmp_path).load()
    assert "www.rts.ch/sport/hockey" in relu._unreadable
    relu.write()

    assert not illisible.exists()
    # Le dossier, désormais vide, disparaît aussi.
    assert not (tmp_path / "www.rts.ch/sport/hockey").exists()


def test_purge_ciblee_remonte_les_parents_devenus_vides(tmp_path):
    """Sans balayage descendant global, il faut remonter la chaîne à la main."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/a/b/c/seul.html")
    store.write()

    illisible = tmp_path / "www.rts.ch/a/b/c/_index.txt"
    illisible.write_bytes(b"\xff\xfe invalide")

    Store(tmp_path).load().write()

    # a/, b/ et c/ n'existaient que pour ce fichier : toute la chaîne s'efface.
    assert not (tmp_path / "www.rts.ch/a").exists()
    # ...sans toucher aux branches voisines.
    assert index_de(tmp_path) == "la-suisse-29312521.html\n"


def test_purge_ciblee_ne_remonte_pas_au_dela_de_data_dir(tmp_path):
    """Garde-fou : la remontée s'arrête à la racine de l'index, même si tout
    est vide."""
    data = tmp_path / "data"
    store = Store(data)
    store.add("https://www.rts.ch/x.html")
    store.write()

    (data / "www.rts.ch/_index.txt").write_bytes(b"\xff\xfe invalide")
    Store(data).load().write()

    assert data.is_dir(), "data/ lui-même ne doit jamais être supprimé"


def test_purge_echec_definitif_est_signale_pas_avale(tmp_path, monkeypatch, caplog):
    """Incident réel : un dossier verrouillé en permanence (antivirus) doit
    être journalisé clairement, pas disparaître en silence derrière un
    `except OSError: break` — c'est ce qui a rendu un taux d'échec de 100 %
    indiscernable d'une progression normale pendant près de 10 heures."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/a/b/seul.html")
    store.write()

    illisible = tmp_path / "www.rts.ch/a/b/_index.txt"
    illisible.write_bytes(b"\xff\xfe invalide")

    original = Path.rmdir

    def rmdir_verrouille(self):
        if self.name == "b":
            raise PermissionError("verrouillé")
        return original(self)

    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)
    monkeypatch.setattr(Path, "rmdir", rmdir_verrouille)

    with caplog.at_level("WARNING"):
        Store(tmp_path).load().write()

    assert any("laissé en place" in m for m in caplog.messages)
    assert (tmp_path / "www.rts.ch/a/b").exists()  # pas supprimé...
    assert not (tmp_path / "www.rts.ch/a/b/_index.txt").exists()  # ...mais bien vidé


def test_purge_complete_supprime_aussi_les_dossiers_vides(tmp_path):
    """Le mode complet (build) doit produire le même résultat que le mode
    ciblé, par un chemin de code différent (rglob plutôt que cibles)."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/a/b/c/seul.html")
    store.write()

    illisible = tmp_path / "www.rts.ch/a/b/c/_index.txt"
    illisible.write_bytes(b"\xff\xfe invalide")

    Store(tmp_path).load().write(force=True)

    assert not (tmp_path / "www.rts.ch/a").exists()
    assert index_de(tmp_path) == "la-suisse-29312521.html\n"


def test_store_jamais_charge_balaye_tout(tmp_path):
    """Sans load(), `_disk_files` est vide : la purge ciblée conclurait qu'il
    n'y a rien à faire alors que *rien* sur disque n'est légitime. Le balayage
    complet est alors le seul régime correct."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/obsolete/x.html")
    store.write()

    repart = Store(tmp_path)  # jamais chargé
    repart.add(ARTICLE)
    repart.write()

    assert not (tmp_path / "www.rts.ch/obsolete").exists()


def test_force_reecrit_meme_les_dossiers_inchanges(tmp_path):
    store = Store(tmp_path)
    store.add_many([ARTICLE, "https://www.rts.ch/sport/hockey/match-1.html"])
    store.write()

    relu = Store(tmp_path).load()
    marques = _poser_temoins(tmp_path)
    relu.write(force=True)

    assert _survivants(marques) == set(), "force doit tout réécrire, témoins compris"
